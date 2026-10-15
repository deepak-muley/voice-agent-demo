# One live call, function by function

## The short version

At the start we tell the model two things.

1. The system prompt: you are the voice agent on an IT service desk. Here is how to speak, and here is when you may open a ticket.
2. Four tools it may ask for: search the knowledge base, read a ticket, open a ticket, or transfer the caller.

The prompt is saved once, as the first message. The four tool descriptions are sent again with every message.

Then, for each line you type:

1. We hide secrets such as an SSN or a password.
2. We add your line to the conversation and send the whole conversation to the model, with the four tools.
3. The model answers in one of two ways.
   - It returns the sentence to say. We clean that sentence and show it. This is what happened for "help", "can u create an incident", the first "yes", and "thanks".
   - It names a tool. We run that tool, send the result back, and ask the model again. It then returns the sentence. This is what happened for "my vpn is not connecting" (`search_knowledge`) and the second "yes" (`create_incident`).

The model does not always pick a tool. It picks one only when it needs data we did not already give it. After a tool runs, we do send that result back, and the next reply from the model is the sentence you see.

## Which role to use

Every message has a `role`. The role says who is speaking. Pick it from who produced the text, not from what the text is about.

| Role | Who is speaking | Put this here | Do not put this here |
| --- | --- | --- | --- |
| `system` | The developer, once, before the caller talks | Job, speech rules, when a write is allowed | The caller's words, a tool result, a ticket id |
| `user` | The person on the call | The line they just typed, after secrets are hidden | Instructions you wish the model would follow, a tool result |
| `assistant` | The model | The sentence to speak, or a `tool_calls` list when it wants a tool | Text you wrote yourself and want the model to treat as its own earlier reply, unless you are replaying a real prior reply |
| `tool` | Your program, answering one tool request | The JSON result, with the same `tool_call_id` the model used | A new caller line, or a sentence you want spoken aloud |

Decide in this order:

1. Did the person just say it? Use `user`.
2. Did the model just say it, or ask for a tool? Use `assistant`.
3. Did your code just run the tool the model asked for? Use `tool`. The `tool_call_id` must match that request. A tool message with no matching assistant `tool_calls` is not a valid turn.
4. Otherwise it is an instruction for every turn. Use `system`, and send it once at the start.

This repo uses only those four. `system` is set in `VoiceConversationAgent` when the agent is built. `user`, `assistant`, and `tool` are appended in `handle_utterance`.

## Are these roles and keys standard?

They are the standard for the OpenAI Chat Completions API. Azure OpenAI, vLLM, and Ollama copy that same JSON when they claim to be OpenAI-compatible. That is the API `OpenAICompatibleClient` speaks. They are not a rule built into every model. Anthropic and Gemini use different role names and different tool JSON. Point this client at one of those and the keys will not match.

On this API, a model reply is always `role: "assistant"`. The model does not answer as `user`, `system`, or `tool`. A spoken sentence is an assistant message with `content` set. A tool request is still an assistant message: `content` is empty and `tool_calls` is set. The `tool` role is ours. We write it after we run the tool. The model never sends that role back.

The call itself is JSON because that is how this HTTP API is defined. The caller does not type JSON. `type` is `"function"` on both sides: in the tool list we send, and in each item of `tool_calls`. For Chat Completions function tools, that string is the standard value, not a label we invented. `arguments` is a string that itself contains JSON, not a nested object. Our client parses that string into a dict before the desk method runs.

Two nearby names are easy to mix up. `developer` is a newer OpenAI name for the same job as `system`. Not every compatible server accepts it, so this code keeps `system`. `function` is the old role name for what is now `tool`. New messages here stay `system` and `tool`.

## Where this format is written down

`OpenAICompatibleClient` posts to `{base_url}/chat/completions`. The body you see with `--verbose` is the [Create chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create/) request. Roles, `tool_calls`, `type: "function"`, and `arguments` as a JSON string are defined there. The walkthrough of the same flow is [Function calling](https://developers.openai.com/api/docs/guides/function-calling?api-mode=chat), with the chat-completions examples.

Any server that says it is OpenAI-compatible means it accepts that same JSON. This client already points at one by changing `OPENAI_BASE_URL`:

| Server | What it accepts | Docs |
| --- | --- | --- |
| OpenAI | Chat Completions at `/v1/chat/completions` | [Create chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create/) |
| Ollama | A subset of that API at `http://localhost:11434/v1` | [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility) |
| vLLM | Chat Completions for a model you host | [OpenAI-compatible server](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/) |
| Azure OpenAI | The same Chat Completions JSON, on an Azure host and path | The Azure OpenAI REST reference for chat completions |

A compatible server may implement only part of the reference. Tools, in particular, work only on models that were built to emit `tool_calls`. If a model ignores `tools` or returns an error, the JSON shape is still right. That model does not support this part of the API.

OpenAI's newer endpoint is the [Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses), `POST /v1/responses`. It replaces the `messages` list with `input` and `output` items. A tool request there is a `function_call` item, and the result is a `function_call_output` item, not a `role: "tool"` message. Some current OpenAI models expect tool calls on that API rather than on Chat Completions. This client does not speak it. Ollama and vLLM also expose `/v1/responses` as a second, separate path.

Anthropic and Gemini are not drop-in replacements. Each has its own message list and its own tool JSON. Use them with their own client, or with a router such as LiteLLM that translates. Do not point `OPENAI_BASE_URL` at them and expect `type: "function"` to work.

## What a tool request looks like

For "my vpn is not connecting" the model did not return a sentence first. It returned an assistant message whose `tool_calls` list names the tool. `content` is empty. `arguments` is a JSON string.

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "search_knowledge",
        "arguments": "{\"query\": \"vpn not connecting\"}"
      }
    }
  ]
}
```

We read `name` and `arguments`, run `search_knowledge`, and send the result back as a tool message. The `tool_call_id` must match the `id` above, so the model knows which request this answer belongs to. `content` is a JSON string.

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "{\"ok\": true, \"articles\": [{\"id\": \"KB001\", \"title\": \"VPN client will not connect\", \"steps\": \"Quit the VPN client, reconnect to office wifi, then open the client and pick the full-tunnel profile.\"}]}"
}
```

We send the conversation again. This time the model returns a normal sentence, with no `tool_calls`:

```json
{
  "role": "assistant",
  "content": "You can try quitting the VPN client. Then reconnect to your office Wi-Fi. After that, open the client and select the full-tunnel profile. Would you like to try this?"
}
```

`call_abc123` stands in for the id the API assigned. The second "yes" has the same shape, with `name` set to `create_incident` and `arguments` carrying the description, `urgency`, and `confirmed`.

`--verbose` prints these payloads for real, once per model call. The API key stays in the header and is not printed.

```bash
python3 voice_agent.py --demo --verbose
OPENAI_API_KEY=... python3 voice_agent.py --live --verbose
```

Each round prints `sent to model` (the request body), `received from model` (the response body), and, when a tool ran, `tool result` (the message appended before the next request).

## Which functions run a tool call

`handle_utterance` in `agent.py` is the function that handles tool calling. `live` in `voice_agent.py` calls it once for each line you type.

```text
live                          voice_agent.py
  handle_utterance            agent.py
    complete                  model_client.py   sends the conversation
    _parse_completion         model_client.py   reads tool_calls into a ToolCall
    _assistant_tool_message   agent.py          stores the assistant tool message
    _run_tool                 agent.py          runs the desk method
    complete                  model_client.py   sends the tool result, gets the sentence
```

`_run_tool` looks up `call.name` in the handler map and calls that function with the arguments. The map is filled once, at startup, by `build_agent` in `voice_agent.py`:

| Name in the tool message | Method that runs |
| --- | --- |
| `search_knowledge` | `desk.search_knowledge` |
| `get_incident` | `desk.get_incident` |
| `create_incident` | `desk.create_incident` |
| `escalate` | `desk.escalate` |

A name that is not in that map returns `{ok: false, error: unknown_tool}`. No desk method runs.

The rest of this file is the same session, one function at a time.

This is the session from `python3 voice_agent.py --live`. The words that come back are standard because they are copied from the prompt and from the tool result, not invented for that caller.

```text
caller> help
agent > I can help you. What issue are you experiencing?

caller> my vpn is not connecting
agent > You can try quitting the VPN client. Then reconnect to your office Wi-Fi. After that, open the client and select the full-tunnel profile. Would you like to try this?
tools > search_knowledge

caller> can u create an incident
agent > I can create an incident for you. Is this urgent, like a meeting or outage within the hour?

caller> yes
agent > I will create the incident with high urgency. Can you confirm that you want me to proceed?

caller> yes
agent > I have created the incident. The ticket number is I N C, zero zero one, zero zero zero two. You will be updated shortly.
tools > create_incident

caller> thanks
agent > You're welcome! If you need further assistance, just let me know.
```

## What starts before you type

`main` sees `--live` and calls `live` in `voice_agent.py`. `live` builds two objects and then waits:

| Object | Function | What it holds |
| --- | --- | --- |
| `OpenAICompatibleClient` | `model_client.py` | HTTPS address, API key, model name |
| `ServiceNowSandbox` | inside `build_agent` | Two knowledge articles and one existing ticket, `INC0010001` |
| `VoiceConversationAgent` | `build_agent` | The system prompt as the first message, plus a map from tool name to a desk method |

The handler map is the only connection between the loop and the desk:

| Name the model may say | Python method that runs |
| --- | --- |
| `search_knowledge` | `ServiceNowSandbox.search_knowledge` |
| `get_incident` | `ServiceNowSandbox.get_incident` |
| `create_incident` | `ServiceNowSandbox.create_incident` |
| `escalate` | `ServiceNowSandbox.escalate` |

`live` then sits on `input("caller> ")`. Each line is one call to `handle_utterance`. Ctrl+C is caught there and the process exits. The new ticket lived only in that process, so it disappeared with it.

## The same five steps for every line

`handle_utterance` in `agent.py` does this:

1. `redact` looks for an SSN, a card number, or a password. None of these lines had one, so the text was stored as typed.
2. The line is appended to `messages`. That list is the memory. It still holds the system prompt and every earlier turn.
3. `OpenAICompatibleClient.complete` sends the whole list, plus the four tool schemas, to the model.
4. If the model returns a tool call, `_run_tool` finds the name in the handler map and runs that method. The JSON result is appended to `messages`, and the loop goes back to step 3.
5. If the model returns a sentence, `normalize_speech` cleans it and `handle_utterance` returns a `SpeechReply`. `live` prints `agent >` and, when a tool ran, `tools >`.

The tool runs in the middle of the turn. The sentence is printed after it. That is why `agent >` appears above `tools >` even though the desk method ran first.

A line with no `tools >` means step 4 never happened. The model returned a sentence on the first call.

## Each line of this session

**help.** One model call. It returned text and no tool call. The desk was not touched.

**my vpn is not connecting.** Two model calls inside this one line. The first returned a `search_knowledge` call. `_run_tool` ran `search_knowledge("vpn")`, which matched the article whose title contains "vpn". The second model call read that article and wrote the Wi-Fi steps. `live` then printed the sentence and `tools > search_knowledge`.

**can u create an incident.** One model call, no tool. The prompt says not to open a ticket until the caller has agreed, so the model asked a question instead of calling `create_incident`.

**yes.** One model call, no tool. This "yes" answered the urgency question. The prompt also says `confirmed=true` is allowed only after a clear agreement to open the ticket, so the model asked a second time and still did not call the tool.

**yes.** Two model calls. The first returned `create_incident` with `urgency` `high` and `confirmed` `true`. `_run_tool` ran `create_incident`, which advanced the counter from `10001` to `10002` and stored `INC0010002`. The second model call read that JSON and spoke the number.

**thanks.** One model call, no tool. The model could say "you're welcome" because the ticket turn was still in `messages`. The desk was not touched.

## Why the wording was standard

The model did not look up a script of finished replies. Three pieces of text we wrote were in the request, and the model stayed close to them.

**The VPN steps are the knowledge article.** `search_knowledge` returned this string from `ServiceNowSandbox`:

```text
Quit the VPN client, reconnect to office wifi, then open the client and pick the full-tunnel profile.
```

The prompt says a fix may come only from a tool result. The spoken line is that sentence with small wording changes: "office wifi" became "office Wi-Fi", "pick" became "select". A different caller with the same VPN symptom gets the same steps, because the article is the source.

**The urgency question is the tool schema.** The `urgency` field is described as "high when the caller has a meeting or outage within the hour." The model asked, "Is this urgent, like a meeting or outage within the hour?" That phrase was in the schema we sent with every `complete` call. The caller never said "meeting" or "outage".

**The ticket number is spoken the way the prompt shows.** The prompt's example is `I N C, zero zero one, two three four five`. The model copied that pattern for `INC0010002`: "I N C, zero zero one, zero zero zero two." The words "zero" and "two" came from the model. `normalize_speech` would also rewrite a raw `INC0010002` left in the text, into digits with spaces (`I N C, 0 0 1, 0 0 0, 2`). It did not have to, because the model had already spelled it.

**The two confirmation questions are the prompt's write rule.** The prompt says to pass `confirmed=true` only after the caller agreed in the previous turn, and otherwise to ask. The schema repeats that. So "can u create an incident" produced a question, the first "yes" produced another question, and only the second "yes" ran `create_incident`. `create_incident` would also refuse `confirmed` other than `true`, but in this session the model did not try to skip the ask.

**help and thanks had no article to copy.** Nothing in the prompt scripts those two sentences. They are the model filling the role "voice agent on an IT service desk" with no tool result to quote. They are the lines that can vary from one run to the next. The VPN steps, the urgency phrase, and the shape of the ticket number should not.
