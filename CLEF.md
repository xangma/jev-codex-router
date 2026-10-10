# Local Clef profile for macOS

This fork supports [Clef Flash](https://huggingface.co/cloudflare/Clef-Flash), an
Apache-2.0 open-weight decision model, through llama.cpp's native System One
endpoint. Clef selects the execution model and reasoning effort; Codex work still
runs on your signed-in ChatGPT plan. There is no hosted Jev request in this profile.

The model picker exposes three effort preferences, all selecting between **Luna
and Sol**. Astra is blocked in every profile. Every profile allows **low, medium,
high, xhigh and max** reasoning. Clef chooses sufficient effort for the current
work; profiles guide that decision without imposing a floor or a range:

| Profile | Picker ID | Effort preference | Picker default |
|---|---|---|---|
| Auto Cheap (Clef) | `jev/auto-cheap` | Prefer low; increase when difficulty or failures justify more care | low |
| Auto Standard (Clef) | `jev/auto` | Balance care and cost at sufficient effort | medium |
| Auto Deep (Clef) | `jev/auto-deep` | Prefer careful reasoning for difficult decisions; keep trivial work low | high |

Standard retains `jev/auto` for existing conversations. The picker defaults are
UI hints; Clef can select any supported effort in every mode. Deep can select
Luna at low effort for a trivial task, and Cheap can select higher effort when
the work warrants it. Preferences do not establish token budgets or usage savings.

## Build and download

Prerequisites: Apple Silicon macOS, CMake, Ninja, Node.js 22.19+, Python 3.11+,
Codex CLI with a working ChatGPT login, and the Hugging Face `hf` CLI.
The tested machine has an M4 Max and 128 GiB unified memory.

```sh
mkdir -p ~/repos
git clone https://github.com/ggml-org/llama.cpp ~/repos/llama.cpp
cmake -S ~/repos/llama.cpp -B ~/repos/llama.cpp/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DGGML_METAL=ON -DGGML_ACCELERATE=ON \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
  -DOpenMP_libomp_LIBRARY=/opt/homebrew/opt/libomp/lib/libomp.dylib
cmake --build ~/repos/llama.cpp/build --target llama-server -j 8
hf download ggml-org/Clef-Flash-GGUF Clef-Flash-Q8_0.gguf \
  --local-dir ~/.local/share/clef/models
```

Q8_0 occupies 9,657,260,192 bytes. This setup was verified on 2026-10-10 with
llama.cpp commit `aa94f2086147cd4bfbcff818ce998dffce5fcb39` and router upstream
commit `b0a04a5413f0794844585b2d64487affeaf35d78`.

The explicit OpenMP path avoids an older `/usr/local/lib/libomp.dylib` on this
machine. Use your installed runtime's path, or configure `-DGGML_OPENMP=OFF`
if OpenMP is unavailable. Metal and Accelerate remain enabled.

## Install or refresh

From this repository, with a supported Node binary on PATH:

```sh
python3 server/install-clef.py \
  --llama-server ~/repos/llama.cpp/build/bin/llama-server \
  --model ~/.local/share/clef/models/Clef-Flash-Q8_0.gguf \
  --node "$(command -v node)" --sleep-idle-seconds 300 --share-chatgpt --smoke
```

`--share-chatgpt` explicitly authorizes the local router to use Codex's native
session. `--smoke` sends one real request and consumes ChatGPT quota. Omit it for
installation without a live request. The installer validates the running Clef
profile either way; readiness alone does not prove end-to-end routing.

Fully quit and reopen Codex when convenient, then select an Auto profile. The
installer preserves your existing default model, agent preferences, and unrelated
settings. It disables installation of the router's optional helper skill pack and
automatic agent concurrency defaults. Those opt-outs are forwarded to the macOS
router service. Use this profile installer for subsequent refreshes; the upstream
installer and updater retain upstream defaults.

The model and keys stay outside the checkout. The installer backs up Codex's
configuration under `~/.codex/codex-router/clef-install-backups/`. Never publish
that directory, local state, capability URLs, or raw service logs.

## Runtime and verification

| Service | Loopback port | launchd label |
|---|---:|---|
| Clef / llama-server | 8082 | `com.xangma.clef-flash` |
| Decision adapter | 4319 | `com.thibaultsaintjean.jev-router` |
| Codex Router | 4202 | `io.github.codex-router` |

The llama.cpp router listens on port 8082 and loads Clef on demand. Its editable
presets are in `~/.local/share/clef/models.ini`; the installer preserves additional
model sections when refreshed. `--models-max 1` limits the router to one loaded
model at a time. Clef uses Q8_0, Metal offload, one slot, and an 8192-token context.

The shared preset default `sleep-idle-seconds = 300` unloads model weights and
inference memory after five minutes without inference. The router stays available; the next
request reloads the requested model. Health/model/props checks do not wake it or
reset the timer. Change the timeout using the installer's positive
`--sleep-idle-seconds` option; use `--models-preset PATH` for another preset file.
New model sections inherit the idle default unless they override it. For example:

```ini
[another-model]
model = /absolute/path/to/another-model.gguf
n-gpu-layers = 99
ctx-size = 8192
load-on-startup = false
```

Edit the preset and restart the service to publish another model:

```sh
launchctl kickstart -k "gui/$(id -u)/com.xangma.clef-flash"
```
Adding a model to llama.cpp makes it available for local clients; selecting it as
Codex's classifier also requires changing `CLEF_MODEL`, and it must support the
native `/v1/systemone` decision API.

The owner-only generated key is `~/.codex/codex-router/clef-api.key`; only a file path
appears in process arguments. Local inference disables environment proxies and
redirects. Clef uses `/v1/systemone`, not a chat-completions endpoint.

Adapter configuration persisted by the profile:

```text
JEV_DECISION_PROVIDER=clef
CLEF_ENDPOINT=http://127.0.0.1:8082/v1/systemone
CLEF_MODEL=clef-flash
CLEF_API_KEY_FILE=<private local file>
JEV_DECISION_TIMEOUT=30
JEV_BASELINE_MODEL=gpt-6.1-sol
```

Clef execution is restricted to **Luna and Sol; Astra is disabled**. Its classifier
asks only for Luna/Sol capability, reasoning effort and a route lease. Sol handles
architecture and final or risk-focused review. A hard dispatch ceiling also caps
historical Astra decisions and leases, configured baselines, shadow/kill-switch
routes and empty-completion recovery at Sol. Replayed configuration updates cannot
override the selected execution model or the selected adaptive effort. Clef
requests also disable the parent router's cooldown rerouting and provider failover,
so it cannot replace the chosen model or effort. A technical classifier/provider
error uses Sol at medium effort in every mode and never retries through hosted
Jev. Explicit test or check failures inform the next adaptive decision. Quota
fallbacks accept only explicit Luna/Sol IDs; opaque provider aliases are excluded, so exhausted native usage may
return an error instead of selecting another provider. Upstream behavior is
unchanged when `JEV_DECISION_PROVIDER` is omitted.

The local adaptive policy supplies bounded aggregate feedback from prior routes:
token usage, latency, and explicit test or check outcomes when available. These
observations are advisory evidence for future decisions. They do not provide
calibrated success probabilities or establish that a particular effort will save
quota. HTTP status and a `completed` response indicate transport and completion;
correctness evidence comes from explicit checks. Missing check outcomes remain
unknown. Feedback is aggregated locally rather than storing prompt or response
text. The owner-only aggregate file is
`~/.codex/codex-router/adaptive-feedback.json`; keep it private with the other
router state.

Check evidence comes from a recognized test, lint, typecheck or smoke command
matched to its latest tool result. Literal `functions.exec` wrappers are supported;
ambiguous or dynamic commands remain unknown. Route attribution requires a stable
prompt cache key. Replayed results are counted once, failed checks clear the route
lease, and passing checks permit effort to decrease. Scope history stays in memory;
aggregate measurements survive restart. Checks observe outcomes of the work, not
proof that the last selected model authored a correct solution.

Tool-step failures use explicit process exit status or MCP error status. Successful
log and source reads stay clean even when their contents mention errors. Running
commands remain pending; unknown plain results use anchored diagnostics rather
than matching isolated words anywhere in the output.

Textual tool results larger than 16 KiB receive a stable head-and-tail preview.
The complete UTF-8 output is saved in owner-only, content-addressed files under
`~/.codex/codex-router/tool-output/`; the preview gives its exact path for a
targeted read. Original results are checked before previewing. User messages,
instructions, calls, reasoning and structured or image results remain complete.
If archival fails, the original result is forwarded. Files remain available until
you remove them; keep this directory private and retain files needed by active
chats. Previews stay identical across replay, model changes and service restarts.

The classifier relay preserves validated native session UUIDs. When generic
provider transport omits them, Codex's UUID `prompt_cache_key` restores stable
session and thread identity. Cache controls stay intact. This avoids losing local
session affinity; actual provider cache reads are measured from response usage,
and cache hits are not guaranteed.

In the tested llama.cpp build, Clef has no KV prefix cache: each fresh decision
processes its whole prompt. The adapter reuses successful, identical local
decisions for **60 seconds**, with at most **128 entries** in memory. Concurrent
duplicates share one inference. Changes to state, feedback, questions, their
order, mode, endpoint, credentials, or the managed preset/GGUF revision require
fresh inference. Unknown model files and unsuccessful decisions bypass reuse.
Prompt text and credentials are hashed for lookup and are not stored in cache
entries or persisted. Restarting the adapter clears the cache; when replacing
weights under the same alias, restart both the model service and adapter.

`/health` exposes `clef_decision_cache` counters. Ask responses and route logs
report `decision_cache.kind: exact_result` and `hit`; a hit reports zero newly
processed `usage` tokens and the avoided count as `saved_input_tokens`.
The native System One `input_tokens` count alone does not measure prefix-cache
reuse. Changing decisions still run full Clef inference, and cache hits do not
reset llama.cpp's idle timer.

```sh
curl -fsS http://127.0.0.1:4319/health
JEV_DECISION_PROVIDER=clef python3 server/smoke.py --model jev/auto
JEV_DECISION_PROVIDER=clef python3 server/smoke.py --model jev/auto-cheap
JEV_DECISION_PROVIDER=clef python3 server/smoke.py --model jev/auto-deep
```

A successful smoke receipt reports `decision_source: clef`, matching `model` and
`response_model`, `status: completed`, and `visible_model_effort: confirmed`.
This verifies the routed request and visible model/effort signature. With the
installed 300-second timeout on 2026-10-10, Clef reported sleeping at 301 seconds:
combined router/child RSS fell from about 10.1 GiB to 263 MiB. A new native decision request reloaded the model
and answered in 1.1 seconds. These are observations from this Mac, not latency
or memory guarantees for another model or machine.

The local validation also completed a real function-call/tool-result continuation
with a model change from Sol to Luna between calls. Automated checks cover local
credentials, redirects, routing failures, the no-Astra ceiling, and preserved settings.
On 2026-10-10, all 296 server tests passed. The adaptive profile was deployed and
all three live Auto smoke requests selected Luna at low effort for `Reply only OK`,
including Deep. HTTP checks also cover failed/pass outcomes, replay deduplication,
lease reconsideration, rapid tool-result handoffs, and disconnected attempt costs.

Clef logs are in `~/Library/Logs/clef-flash.{out,err}.log`; adapter logs use
`jev-router.{out,err}.log`. To stop Clef temporarily:

```sh
launchctl bootout "gui/$(id -u)/com.xangma.clef-flash"
```

To remove the routing setup, use the repository's managed uninstaller, then unload
and remove `~/Library/LaunchAgents/com.xangma.clef-flash.plist`. Private keys and
downloaded weights are retained until you explicitly remove them.

Routing does not remove Codex's initial instruction/context tokens. Model changes
can affect cache reuse. Local latency and passing smoke checks do not establish
quota savings or routing quality across real tasks; measure those separately.
