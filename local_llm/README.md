# Local Ollama on Kind

Ollama serves the model. Open WebUI is the browser in front of it. Both run as normal Kubernetes workloads, installed with Helm, on a [Kind](https://kind.sigs.k8s.io/) cluster (Kubernetes inside Docker). The Helm values are the part you take to a real cluster. The Kind config is not.

## Do you need KServe?

No.

Ollama is already an inference server. It listens on HTTP, stores weights on a volume, and speaks the OpenAI-compatible API this repo's client already uses (`/v1/chat/completions`). Kubernetes hosts that the same way it hosts any other long-running process:

| Piece | What it is here |
| --- | --- |
| Deployment | The `ollama/ollama` container. One replica. |
| Service | Cluster DNS `ollama.llm.svc.cluster.local:11434`, plus a NodePort so the laptop can reach it. |
| PersistentVolumeClaim | The downloaded weights, so a pod restart does not pull them again. |
| Helm release | `otwld/ollama`, with [`values/ollama.yaml`](values/ollama.yaml). |

Open WebUI is a second release, `open-webui/open-webui`. Its pod calls the Ollama Service. It does not embed the model.

[KServe](https://kserve.github.io/website/) is a control plane for model serving. An `InferenceService` adds a standard predict protocol, scale-to-zero, canary traffic, and GPU autoscaling for runtimes such as Triton, TorchServe, or vLLM. Ollama is not one of those runtimes, and this lab does not need that control plane. Installing it would also mean installing Knative and a gateway in front of a server that already answers HTTP.

The Ollama chart has a `knative.enabled` flag. That is Knative Serving (scale to zero), and it stays off here. It is still not KServe.

Use KServe later only if you replace Ollama with a runtime KServe already knows, and you want one `InferenceService` API, scale-to-zero, or canaries across model versions.

## What stays the same on a real cluster

Install the same two charts with the same values files, then change only the cluster-specific bits:

1. `persistentVolume.storageClass` / `persistence.storageClass`. Kind uses `local-path`. A cloud cluster uses its own class (`gp3`, `standard-rwo`, and so on). Drop the local-path-provisioner; that cluster already has a provisioner.
2. `service.type`. Set both Services back to `ClusterIP`. Put an Ingress (or a Gateway) on Open WebUI, and on Ollama only if clients outside the cluster must call it. In-cluster callers keep using `http://ollama.llm.svc.cluster.local:11434`.
3. GPU. On a node pool that advertises `nvidia.com/gpu`, set `ollama.gpu.enabled: true` in [`values/ollama.yaml`](values/ollama.yaml). Kind on Docker Desktop cannot pass a GPU through, so this file leaves it off.
4. Auth. [`values/open-webui.yaml`](values/open-webui.yaml) sets `WEBUI_AUTH=False` so the local UI opens with no login. Remove that env var on any cluster other people can reach.
5. Delete [`kind-config.yaml`](kind-config.yaml) from the path. Nothing in the charts depends on it. The NodePort numbers exist only so Kind can map them to `localhost`.

## Run

You need Docker, [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation), kubectl, and Helm 3. Give Docker Desktop at least 8 GB of memory. The 3B model plus Open WebUI will sit near that.

Quit the Ollama desktop app first if it is holding port 11434. Quit anything else on port 8080.

```bash
./local_llm/scripts/up.sh
```

The first run pulls the Ollama image, the Open WebUI image, and `llama3.2:3b` (about 2 GB). The Ollama pod stays unready until that pull finishes. Then:

| URL | What it is |
| --- | --- |
| http://localhost:8080 | Open WebUI. Auth is off on this cluster. Pick `llama3.2:3b`. |
| http://localhost:11434 | Ollama's own API. `GET /api/tags` lists pulled models. |

The voice agent uses the same API:

```bash
OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama \
  OPENAI_MODEL=llama3.2:3b python3 single_agent/voice_agent.py --live
```

`llama3.2:3b` is the default because it fits a laptop CPU. The desk walkthroughs name `llama3.1` (the 8B tag). To serve that instead, change `ollama.models.pull` in [`values/ollama.yaml`](values/ollama.yaml) and run `up.sh` again, or pull it into the running pod:

```bash
kubectl -n llm exec deploy/ollama -- ollama pull llama3.1
```

`up.sh` is safe to re-run. It upgrades the two releases in place.

Tear the cluster down (this deletes the downloaded weights):

```bash
./local_llm/scripts/down.sh
```

## Layout

| File | What it is |
| --- | --- |
| `kind-config.yaml` | One-node Kind cluster. Host ports 8080 and 11434. |
| `values/ollama.yaml` | Model tag, PVC, NodePort, CPU-only resources. |
| `values/open-webui.yaml` | UI pointed at the Ollama Service. No bundled Ollama, pipelines, or Redis. |
| `scripts/up.sh` | Kind, local-path storage, then the two Helm releases. |
| `scripts/down.sh` | `kind delete cluster`. |
