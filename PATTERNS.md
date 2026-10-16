# Multi-agent patterns

[multi_agent/](multi_agent/) is one pattern: a router, one specialist per caller line, then a voice agent. This note is the map around that pattern. The snippets further down are for reading. The runnable single-agent graph is in [single_agent_langgraph/](single_agent_langgraph/), and that folder is the one that installs LangChain and LangGraph.

Read [multi_agent/README.md](multi_agent/README.md) for the desk as it runs, and [PRODUCTION.md](PRODUCTION.md) for the contracts a live call still lacks.

## What this repo already is

`DeskSession.handle_utterance` does four steps: redact, route, run one specialist, speak.

| Piece | In this repo | Job |
| --- | --- | --- |
| Router | `RuleRouter` or `ModelRouter` | One word from a closed set: `knowledge`, `incident`, `escalation`, `voice` |
| Specialist | `Specialist.run` | The single-agent tool loop, with that role's tools only |
| Handoff | `Handoff` | One JSON user message. The caller line is data |
| Result | `SpecialistResult` | Status, data, tools used. The caller never hears this object |
| Speaker | `ScriptedVoice` or `ModelVoice` | The only text that leaves the session |
| Facts | `CallFacts` on the session | Symptom, agreement, ticket number, article steps. Still true on the next line |

A specialist starts a fresh message list every turn. It does not see the rest of the call. The session copies the facts it must keep into `CallFacts`.

That split exists because the jobs finish differently. A knowledge lookup is finished when an article comes back. Opening a ticket is finished only after the caller agrees. A transfer is finished when the caller leaves the bot.

## Where the state goes

Today it lives on the `DeskSession` object, in the process that answered the call.

| Field | What it holds | Lifetime today |
| --- | --- | --- |
| `facts` | `CallFacts` | This process |
| `messages` | Redacted caller lines and spoken replies | This process |
| `audit` | Redaction, route, tool call, tool result, speech | This process |
| Specialist `messages` | Prompt, handoff, tool turns | Thrown away at the end of `run` |

A second process cannot resume a caller who already said yes. The new ticket in `ServiceNowSandbox` disappears with the process too.

Production splits that into a hot store and a durable store. The conversation is the message list. It is not a vector database. [PRODUCTION.md](PRODUCTION.md) draws the same line.

| Store | What you put there | Why this store |
| --- | --- | --- |
| Valkey or Redis | The hot transcript, `CallFacts`, the last route, and a lock, under the call id. Set a TTL and a size cap | The next utterance needs this within the same call. A lock stops two workers from appending the same line |
| PostgreSQL | Audit rows, idempotency keys for writes, the ticket id once ServiceNow has it | These must survive a restart and a retried "yes" |

Key the hot record by call id, for example `call:{id}`. When the transcript hits the cap, summarize older turns and keep the recent ones plus `CallFacts`. Scrub secrets before either store sees them. `redact` already runs on the caller line. The same scrub belongs on tool arguments and log fields.

`create_incident` needs an idempotency key in PostgreSQL, not only a memory flag. A retried "yes" must open one ticket. The sandbox counter does not give you that.

If the loop later moves to LangGraph, the graph state takes the place of the hot record. You still keep audit rows and idempotency keys in PostgreSQL. A checkpoint is the graph's memory. It is not your audit log, and it should not be the only copy of a write.

| LangGraph piece | What it stores | Where |
| --- | --- | --- |
| Checkpointer | One thread's graph state, after each super-step. Short-term memory for this call | `InMemorySaver` while learning. `PostgresSaver` in production. SQLite is for a local demo |
| `thread_id` | Which call to load. Pass it on every `invoke` | Your call id, kept under 255 characters for `PostgresSaver` |
| Store | Facts that outlive one call: caller preferences, facts shared across threads | A separate table or keyspace. This desk does not need it for one phone call |

`InMemorySaver` dies with the process, same as `DeskSession` today. Resume after a crash means `PostgresSaver` (package `langgraph-checkpoint-postgres`). Redis and Valkey checkpointers exist too. For this desk, Valkey holds the hot transcript if you keep your own loop, and PostgreSQL holds the durable rows either way.

On resume, a node runs again from the start of its function. A write inside that node must be idempotent, or a crash between the write and the checkpoint opens a second ticket.

## The patterns

Anthropic's names and LangChain's names overlap. The table uses both so a doc search hits the page you want.

| Pattern | What happens | Use it when | This desk |
| --- | --- | --- | --- |
| Single agent | One prompt, one tool list, one loop | A few tools and one stop condition | [single_agent/](single_agent/) |
| Pipeline | Fixed steps. Each call reads the previous result | The order never changes | A ticket summary that must always be extract, then redact, then speak |
| Router | One classification, then one or more specialists. The router itself holds no tools | Clear categories, and you want the branch in your code | [multi_agent/](multi_agent/). One specialist per line |
| Supervisor | A main agent calls specialists as tools and can call more than one before it answers | One line needs two jobs, and a model should pick the order | Not built here. The LangChain sketch below is this shape |
| Handoffs | A state field picks the active agent. A tool updates that field | The caller stays with one specialist across turns, such as an incident flow that asks, confirms, then writes | `CallFacts` is the seed of this. The route is recomputed every line |
| Fan-out | Several specialists run on the same input. A later step merges them | The reads are independent | Not built here. Knowledge and incident are sequential, and incident can write |
| Generator then critic | One model drafts, a second model accepts or sends it back | A write or a spoken line must be checked before it leaves | The voice agent is a speaker, not a critic. The guards in code are the check |
| Skills | One agent loads a prompt or a tool pack when the turn needs it | Many domains, one loop, and you do not want a second model call per domain | The single-agent prompt is the whole skill, loaded every turn |

Build the router until one line must run two specialists, or a crash must resume mid-confirmation. Those are the two reasons to leave `handle_utterance`.

### Single agent

The model proposes a tool or a sentence. Your loop allow-lists the tool, runs the handler, and stops. Memory is the message list you send back.

Reading: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) (the "augmented LLM"), and [FLOW.md](FLOW.md) for one live call in this repo.

### Pipeline

Prompt chaining. Step two cannot start a different plan. A gate between steps can reject a bad intermediate result and stop.

```text
utterance -> extract symptom -> search_knowledge -> speak the steps
```

Reading: the prompt-chaining section of [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents), and the chain notebook in the [Anthropic cookbook](https://github.com/anthropics/anthropic-cookbook/blob/main/patterns/agents/README.md).

### Router

A small model, or plain rules, returns a label. Your code starts the specialist. `ModelRouter` asks for one word and falls back to `knowledge`. `RuleRouter` does the same branches with no model call.

LangChain's router can also fan out with `Send` and then synthesize. This repo returns one route and does not merge.

Reading: [Router](https://docs.langchain.com/oss/python/langchain/multi-agent/router). The page states the difference: a router classifies and stops, a supervisor keeps the conversation and decides again.

### Supervisor

The main agent is a tool-calling model. Each specialist is a tool whose body calls that specialist and returns its final text. The main agent can call `search` and then `open_ticket` inside one user turn. Subagents start with a clean message list, which is the same isolation as `Specialist.run`.

`langgraph-supervisor` (`create_supervisor`) is no longer maintained. The current shape is `create_agent` plus a tool per specialist. See [Migrate from langgraph-supervisor](https://docs.langchain.com/oss/python/migrate/langgraph-supervisor).

Reading: [Subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents) and the worked example [Build a personal assistant with subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents-personal-assistant).

### Handoffs

The active role is a field in state, not a fresh classification of the latest sentence. A tool returns an update to that field. The next model call sees a different prompt and a different tool list. Use this when the caller is mid-flow ("I asked you to confirm the ticket") and a router that looks only at the latest line would send them back to knowledge.

LangChain also shows a lighter version: one agent, and middleware that swaps the prompt and tools from `current_step`.

Reading: [Handoffs](https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs) and [Build customer support with handoffs](https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs-customer-support).

### Fan-out, then merge

The same utterance goes to two or more specialists at once. A merge step writes the reply. Do this for independent reads, such as a knowledge search and a status lookup that do not depend on each other. A write stays on one path so two branches cannot both open a ticket.

LangGraph runs every node targeted by a conditional edge in the same super-step. The router docs use `Send` for the same idea.

Reading: the parallelization section of [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents), and [Use the graph API](https://docs.langchain.com/oss/python/langgraph/use-graph-api) for conditional edges that return more than one node.

### Generator, then critic

One model produces a draft. A second call scores it against a checklist and either accepts it or sends it back, with a step cap. The checklist for this desk is already in code: the handler allow-list, `confirmed` overwritten from `caller_agreed`, `normalize_speech`, and `MAX_STEPS`. A critic model is an extra call on top of those, for a case where the code cannot see the mistake, such as a spoken fix that was not in the article.

Reading: the evaluator-optimizer section of [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents), and the cookbook's [evaluator-optimizer notebook](https://github.com/anthropics/anthropic-cookbook/blob/main/patterns/agents/evaluator_optimizer.ipynb).

### Skills

One agent, one loop. A tool loads a longer prompt, or a new tool pack, for this turn. You pay tokens for the loaded text and you do not pay a second model hop. The single-agent `SYSTEM_PROMPT` is the version of this with every skill already in the prompt.

Reading: [Skills](https://docs.langchain.com/oss/python/langchain/multi-agent/skills).

## A LangGraph sketch of this desk

This is `DeskSession.handle_utterance` drawn as a graph. The nodes still call the router, the specialist, and the voice agent. The new part is the checkpointer and the `thread_id`.

The confirmation pause is the reason to compile with a checkpointer. `interrupt` writes the checkpoint and returns control to the caller. The next `invoke` with the same `thread_id` continues. Details and the resume shape are in [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts).

```python
import os
from typing import Any, Literal

from typing_extensions import TypedDict
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import InMemorySaver


class DeskState(TypedDict, total=False):
    utterance: str
    caller_agreed: bool
    route: str
    symptom: str
    ticket_number: str | None
    result: dict[str, Any]
    speech: str


def route_node(state: DeskState) -> dict:
    # RuleRouter or ModelRouter. Same four words as multi_agent/router.py.
    route = router.route(state["utterance"], facts_from(state))
    if route not in ("knowledge", "incident", "escalation", "voice"):
        route = "knowledge"
    return {"route": route}


def pick(state: DeskState) -> Literal["knowledge", "incident", "escalation", "voice"]:
    return state["route"]  # type: ignore[return-value]


def knowledge_node(state: DeskState) -> dict:
    result = specialists["knowledge"].run(handoff_from(state), audit)
    return {"result": result.data, "symptom": state["utterance"]}


def incident_node(state: DeskState) -> dict:
    # Overwrite confirmed from caller_agreed inside the specialist, as session.py does.
    result = specialists["incident"].run(handoff_from(state), audit)
    number = (result.data.get("incident") or {}).get("number")
    return {"result": result.data, "ticket_number": number}


def escalation_node(state: DeskState) -> dict:
    result = specialists["escalation"].run(handoff_from(state), audit)
    return {"result": result.data}


def voice_node(state: DeskState) -> dict:
    return {"speech": voice.speak(state["utterance"], state.get("result"))}


builder = StateGraph(DeskState)
builder.add_node("route", route_node)
builder.add_node("knowledge", knowledge_node)
builder.add_node("incident", incident_node)
builder.add_node("escalation", escalation_node)
builder.add_node("voice", voice_node)

builder.add_edge(START, "route")
builder.add_conditional_edges("route", pick)
builder.add_edge("knowledge", "voice")
builder.add_edge("incident", "voice")
builder.add_edge("escalation", "voice")
builder.add_edge("voice", END)

# Learning: gone when the process exits.
graph = builder.compile(checkpointer=InMemorySaver())

# Production: survives a restart. Call setup() once so the tables exist.
# from langgraph.checkpoint.postgres import PostgresSaver
# with PostgresSaver.from_conn_string(os.environ["DATABASE_URL"]) as checkpointer:
#     checkpointer.setup()
#     graph = builder.compile(checkpointer=checkpointer)

config = {"configurable": {"thread_id": "call-42"}}
out = graph.invoke(
    {"utterance": "my vpn will not connect", "caller_agreed": False},
    config,
)
# A later line on the same call loads the checkpoint, including ticket_number.
out = graph.invoke(
    {"utterance": "yes, open it", "caller_agreed": True},
    config,
)
```

`facts_from`, `handoff_from`, `router`, `specialists`, `voice`, and `audit` stand for the objects already in [multi_agent/](multi_agent/). The graph does not replace them.

Reading: [Graph API overview](https://docs.langchain.com/oss/python/langgraph/graph-api), [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers).

## A LangChain supervisor sketch

Here the router is a model. `search_knowledge_desk` and `open_ticket` are tools. Their bodies call the specialist and return a string. The supervisor can call both before it speaks, which `DeskSession` does not do.

The guards stay in the wrappers. `open_ticket` sets `confirmed` from `caller_agreed`. It does not forward a boolean the model invented.

```python
from langchain.agents import create_agent
from langchain.tools import tool

knowledge = create_agent(
    model,
    tools=[search_knowledge],
    system_prompt=KNOWLEDGE_PROMPT,  # multi_agent/prompts.py
)

incident = create_agent(
    model,
    tools=[get_incident, create_incident],
    system_prompt=INCIDENT_PROMPT,
)


@tool
def search_knowledge_desk(utterance: str) -> str:
    """Look up a known fix for an IT symptom. Read-only."""
    result = knowledge.invoke({"messages": [{"role": "user", "content": utterance}]})
    return result["messages"][-1].text


@tool
def open_ticket(utterance: str, caller_agreed: bool) -> str:
    """Open one incident after the caller has agreed. The harness sets confirmed."""
    # create_incident's wrapper overwrites confirmed from caller_agreed.
    result = incident.invoke({"messages": [{"role": "user", "content": utterance}]})
    return result["messages"][-1].text


supervisor = create_agent(
    model,
    tools=[search_knowledge_desk, open_ticket],
    system_prompt=(
        "You are the IT desk coordinator. "
        "Call a specialist for desk data. Speak one or two short sentences. "
        "Do not invent a ticket id or a fix."
    ),
)
```

`create_agent` can take a checkpointer. Put it on the outer supervisor so one `thread_id` is the call. The tutorial above uses `InMemorySaver` for that.

This sketch hides the allow-list inside the framework. [PRODUCTION.md](PRODUCTION.md) keeps that allow-list visible so a barge-in can cancel one model call. Use the sketch to learn the shape. Keep `Specialist.run` until you need the supervisor to chain two specialists, or until you need `interrupt` on the confirmation question.

Reading: [Build a personal assistant with subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents-personal-assistant). The index of every pattern LangChain documents is [Multi-agent](https://docs.langchain.com/oss/python/langchain/multi-agent).

## Reading list

| Topic | Where |
| --- | --- |
| Patterns, and when to stay simple | [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) |
| Small reference implementations | [Anthropic cookbook, patterns/agents](https://github.com/anthropics/anthropic-cookbook/blob/main/patterns/agents/README.md) |
| LangChain's pattern index | [Multi-agent](https://docs.langchain.com/oss/python/langchain/multi-agent) |
| Supervisor as tools | [Subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents) |
| Worked supervisor | [Personal assistant](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents-personal-assistant) |
| Router, including parallel `Send` | [Router](https://docs.langchain.com/oss/python/langchain/multi-agent/router) |
| Active agent in state | [Handoffs](https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs) |
| Load a prompt on demand | [Skills](https://docs.langchain.com/oss/python/langchain/multi-agent/skills) |
| Graphs, checkpoints, interrupts | [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) |
| Hot session store | [Valkey](https://valkey.io/docs/) or [Redis](https://redis.io/docs/latest/) |
| Durable rows | [PostgreSQL](https://www.postgresql.org/docs/current/) |
| This desk's production seams | [PRODUCTION.md](PRODUCTION.md) |

## What you can skip until a design needs it

A swarm, a debate with several peers, and a hierarchy of supervisors are further topologies. Name them if someone asks. They add hops, and each hop is another model call with its own failure. The router in this folder, plus a session store, covers the desk.
