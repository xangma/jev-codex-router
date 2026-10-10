# Local Clef profile for macOS

This fork supports [Clef Flash](https://huggingface.co/cloudflare/Clef-Flash), an
Apache-2.0 open-weight decision model, through llama.cpp's native System One
endpoint. Clef selects the execution model and reasoning effort; Codex work still
runs on your signed-in ChatGPT plan. There is no hosted Jev request in this profile.

The model picker shows **Auto (Clef)**. Its internal ID remains `jev/auto` to retain
the embedded router's native Responses history and tool-continuation handling.

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
  --node "$(command -v node)" --share-chatgpt --smoke
```

`--share-chatgpt` explicitly authorizes the local router to use Codex's native
session. `--smoke` sends one real request and consumes ChatGPT quota. Omit it for
installation without a live request. The installer validates the running Clef
profile either way; readiness alone does not prove end-to-end routing.

Fully quit and reopen Codex when convenient, then select **Auto (Clef)**. The
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

The Clef service uses Q8_0, Metal offload, one slot, and an 8192-token context. Its
owner-only generated key is `~/.codex/codex-router/clef-api.key`; only a file path
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

On Clef errors, routing uses Sol with medium reasoning. It never retries through
hosted Jev. The active mandatory-Astra policy still applies to valid decisions.
The kill switch and shadow mode use the configured baseline. Upstream behavior
is unchanged when `JEV_DECISION_PROVIDER` is omitted.

```sh
curl -fsS http://127.0.0.1:4319/health
JEV_DECISION_PROVIDER=clef python3 server/smoke.py
```

A successful smoke receipt reports `decision_source: clef`, matching `model` and
`response_model`, `status: completed`, and `visible_model_effort: confirmed`.
The local validation also completed a real function-call/tool-result continuation
with a model change from Sol to Luna between calls. Automated checks cover local
credentials, redirects, routing failures, mandatory policy, and preserved settings.

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
