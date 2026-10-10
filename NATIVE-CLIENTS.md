# Clef Auto for native clients

These adapters use the local Clef decision service with each client's native
model inventory. They preserve the client's own authentication, conversations,
tools and permissions. They do not convert subscription login credentials into
provider API keys. The installed Clef service must be healthy on loopback port
4319; native adapters require the `clef` decision provider before forwarding a
task for classification.

Cheap, Standard and Deep guide effort rather than limiting models or imposing
effort floors. Every available model/effort pair remains eligible in every mode.
Clef chooses one valid pair jointly, so a model cannot receive unsupported effort.
Haiku, which the installed Claude Code version reports without an effort control,
remains eligible even in Deep. Native model availability is discovered at runtime.

Install launchers from this checkout:

```sh
python3 clients/install.py --node /absolute/path/to/node
```

Node.js 22.19+ is required for the Antigravity bridge. The installer writes only
`~/.local/bin/claude-clef` and `~/.local/bin/antigravity-clef`. It preserves
unrelated launchers and does not edit native client settings or application files.

## Claude Code

```sh
claude-clef --doctor
claude-clef --mode auto
claude-clef --mode auto-cheap --print 'Reply only OK.'
claude-clef --mode auto-deep --resume SESSION_UUID
```

The small interactive controller runs the official CLI with its native JSON
control interface. It asks Clef before each user turn, applies the selected model
and effort, verifies the applied settings, then submits the original prompt.
The native CLI receives the complete conversation. Normal tool permission
requests are shown for approval; a noninteractive run denies requests needing
user approval. Permission bypasses are never enabled.

This is a separate terminal entry point using the native Claude Code engine.
The stock Claude terminal interface does not expose an automatic model selection
hook. Selection remains fixed within a turn's tool loop.
The wrapper provides a simple prompt-and-reply interface; unsupported rich
native control interactions are rejected. On the first resumed turn, Clef has
the new prompt but no prior-task excerpt. Native Claude still resumes the full
conversation; subsequent wrapped turns include bounded previous-task context.

Claude Code 2.1.110 advertises `max` but silently ignores it in runtime settings
updates. The controller handles transitions to `max` by restarting the native CLI
with `--effort max` and resuming the same session. Other changes retain the
persistent process. Initialization and runtime settings are checked before
inference; native model IDs and effort support come from that CLI's inventory.
`--doctor` performs local initialization only. A routed `--print` reply tests
native authentication as well. If Claude reports a revoked OAuth token, run
`claude auth login` in your own terminal before retrying.

## Antigravity existing chats

```sh
antigravity-clef --mode auto
antigravity-clef --status
antigravity-clef --verify
antigravity-clef --stop
```

This experimental bridge attaches to the running Antigravity app's existing
loopback debugging interface. It intercepts native user-message sends before
dispatch, asks local Clef, and changes only the model configuration of that
request. The native conversation, message content, attachments, authentication
headers and tool permissions remain intact. A small status control shows the
selected model and Auto mode. Internal tool-loop invocations keep the turn's
selected model.

On an existing conversation's first attached turn, the bridge reads a bounded
native history tail for Clef: up to two user and two assistant excerpts, without
thinking text. The full conversation stays with Antigravity. If a bounded tail
cannot be obtained, the original native selection is retained.

The adapter discovers enabled native models from the running app; their enum
identifiers differ from `agy models` CLI slugs. Queued requests cancelled during
classification are suppressed. If classification fails, the request retains
its original native model and the status reports the fallback.

This uses an internal transport interface, not an officially supported model
routing hook. Antigravity's documented `PreInvocation` hook can inject steps but
cannot override model selection. App updates can change the transport contract;
verify backend generation metadata after an update. Rewritten outgoing requests
and model labels alone do not establish the model actually used.
The native chat-box workflow was verified on Antigravity 2.21.1, including its
framed `grpc-web+json` requests, a model switch and preserved conversation history.

Keep the bridge running while using Auto. Stop it with Ctrl-C to restore the
native transport and remove its status control, or run `antigravity-clef --stop`
from another terminal. Cancel any turn still waiting for classification before
stopping. Reopen the bridge after quitting and reopening Antigravity.
App files, OAuth credential
stores and persistent native settings are untouched.

`--verify` checks fresh backend execution metadata against the chosen model,
including the actual backend model identifier. It reports unverified when
identity cannot be established; a rewritten request alone is insufficient.

## Cache and private data

Clef receives only a bounded decision dossier. The native clients receive the
original prompt and native conversation history. Model changes still receive
the full history; cache hits do not carry conversation continuity.

Identical classifier requests benefit from the adapter's 60-second in-memory
result cache. Native execution uses the client's own cache behavior. No raw
prompts or credentials are written into adapter diagnostics. Keep any manual
debugging captures private and outside tracked source.
