# Auto (Clef Local)

An optional second Codex picker route, `jev/auto-local`, lets Clef choose a
configured open-weight executor or GPT-6.1 Sol. The existing `jev/auto` route
continues choosing Luna or Sol. Neither Clef profile permits Astra.

The effort picker sets Sol's maximum effort, intersected with the configured
Sol floor and ceiling. The local executor keeps its independently configured
effort choices. Clef selects a joint model/effort pair from those permitted
choices and can lease a sufficient route across clean tool continuations.

## Configure an executor

Use a separate OpenAI-compatible server for execution. It can run on the same
computer or another host. Do not put the executor in Clef's one-model llama.cpp
instance: alternating decisions and execution would evict the other model.
The adapter rejects a provider endpoint with the same host and port as Clef.
Distinct URLs behind a proxy still need operator verification that they reach
separate serving processes.

First configure and curate your executor using the embedded router. For example,
replace the example URL and model ID with your server's values:

```sh
bin/jev-codex-router router providers generic add my-llama \
  --name "My executor" --base-url http://127.0.0.1:9000/v1 \
  --adapter openai-chat --allow-private
bin/jev-codex-router router providers generic credential my-llama set
bin/jev-codex-router router curate-models my-llama --models my-model \
  --efforts medium,high --apply
bin/jev-codex-router local-executor set --model my-llama/my-model \
  --name "My coding model" --efforts medium,high --context-window 131072
JEV_DECISION_PROVIDER=clef node server/configure-model.mjs
bin/jev-codex-router router refresh-catalog
```

Skip the credential command for an anonymous server. Prefer `openai-responses`
if your server implements the Responses protocol directly; this avoids a
Chat Completions bridge and its tool-schema conversions. Use HTTPS
for remote endpoints, or an existing trusted private network. Credentials stay
in the router's protected provider store, separate from executor settings.
No server, user path or credential is included in the repository.
When curation prompts for context and input modalities, enter your server's
verified context limit and text support. The executor's configured context
window cannot exceed that curated limit; its efforts must be a subset of the
curated model's supported efforts.

Fully quit and reopen Codex to load **Auto (Clef Local)**. Verify a real request
with `JEV_DECISION_PROVIDER=clef bin/jev-codex-router smoke --model jev/auto-local
--sol-effort-cap high`. A successful smoke proves the route that was actually
selected; it does not prove both models were exercised or establish coding
quality. Qualify your executor on real tool-use and continuation workflows.

Settings live in the resolved router state directory as
`clef-local-executor.json`, with atomic owner-only writes. Set
`CLEF_LOCAL_EXECUTOR_FILE` to use another path; `local-executor --file PATH`
reads or edits that path explicitly. Settings reload on the next request,
including route-lease invalidation. Republish the catalog after changing the
executor's context limit or enabling/disabling the picker.

```sh
bin/jev-codex-router local-executor show
bin/jev-codex-router local-executor disable
JEV_DECISION_PROVIDER=clef node server/configure-model.mjs
bin/jev-codex-router router refresh-catalog
```

Disabling removes the picker on republication and preserves the executor's
provider and credential. Old local-auto requests fail clearly until configured
again. Invalid optional executor settings do not disable native Auto.

## Context, caching and recovery

Every selected executor receives the conversation's portable text and tool
history. Provider-specific reasoning transport is adapted at the gateway;
OpenAI's encrypted reasoning cannot be decoded by an open-weight model.
The local picker advertises text and portable tools. It does not advertise
native hosted web search: Codex attaches that capability to ordinary requests
when enabled, which would force even simple coding turns onto Sol. Use the
native Auto picker for hosted-search workflows.
History is never truncated just to force a local fit. Requests requiring native
hosted search, images or unavailable provider-side history select Sol when
permitted. If neither route can satisfy the request, it fails clearly.

GPT and local KV caches are independent. Switching does not transfer cached
state or guarantee eviction of the previous model's cache. Returning to a
model can reuse a matching retained prefix. Cache affinity uses reported token
reads and prefix compatibility; missing counters remain unknown. Local cache
misses use local compute rather than OpenAI input tokens. A later Sol call must
process context it has not cached, so compare whole-task usage and latency.

Keep prompt formatting and tool definitions stable. Enable prompt reuse on
the executor and size its cache for expected concurrency. llama.cpp idle sleep
unloads its KV cache; choose an executor idle timeout independently from Clef's
decision-server timeout. The installer never changes the executor's model
capacity, idle policy or services.

Local transport failures and empty completions may retry a permitted Sol route
once before assistant text or tool calls escape. Streaming output is never
replayed on a different model after it has reached the caller. Local quota
errors do not mark native Codex allowance exhausted. Sol allowance exhaustion
can fall back to the configured compatible executor.
