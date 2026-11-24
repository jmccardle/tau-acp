# tau-acp design

tau-acp lets an editor that speaks the [Agent Client Protocol](https://agentclientprotocol.com)
(ACP) v1 drive a [τ](https://pypi.org/project/ffwf-tau/) session. The editor spawns `tau-acp`
and talks to it over stdio. tau-acp starts and drives `tau serve`, and exposes the editor's MCP
servers to τ as tools.

This document is the contract the code and tests are held to. Where they disagree, fix
whichever one is wrong. Never paper over the difference.

## Scope

**In scope:**
- ACP v1 (`protocolVersion: 1`, stable schema) on the agent side, over stdio.
- One local τ: tau-acp, `tau serve` and the files being edited are all on one machine.
- `tau serve` uses its Unix socket only.
- A session that tau-acp starts has exactly one consumer: tau-acp.

**Out of scope, by decision:**
- Attaching to a session that something else is serving.
- Any network transport: no TCP listener, and not τ's HTTP edge.
- A client-side role that reaches τ across machines.
- Unstable ACP methods (`session/fork`, MCP-over-ACP and others).
- Editor-specific features.

**Not built, by choice (v1 of tau-acp):**
- The client's `fs/*` and `terminal/*` methods. The files are local, so τ's own base tools are
  the right tools, and ACP v2 removes these methods anyway.
- `http`/`sse` MCP servers.
- `image`/`audio` prompts.
- `elicitation/create`.

**τ itself is never changed.** tau-acp uses only τ's public API (`tau`, `tau.cli`). If a
behaviour seems to need a τ change, that is a stop-and-ask, not a workaround.

## Processes

```
editor ──stdio (ACP JSON-RPC)──> tau-acp ──Unix socket (τ wire, RemoteClient)──> tau serve (one per ACP session)
                                  │  ▲                                              │ loads tau_acp/_extension.py via -e PATH
                                  │  └──────── relay socket (one per session) ──────┘ (MCP tool calls)
                                  └──stdio (MCP JSON-RPC)──> each editor-requested MCP server
```

- **Each ACP session is one `tau serve` process:**
  - `python -m tau serve -d --json -e <path of tau_acp/_extension.py> [user serve args]`;
  - its `cwd` is the session's `cwd` (a new session) or comes from τ's session record (a loaded
    session);
  - tau-acp reads the one-line JSON record from stdout and connects with
    `tau.RemoteClient.connect(record["socket"], "tau-acp")`.
- **Stopping:** tau-acp stops the serve when the session closes, or when the editor closes
  stdin or tau-acp is signalled (`tau serve --stop --session ID --json`). The session log stays on
  disk, so `session/load` can come back to it. (Exception, found in A1: τ discards a session that
  never received input when its serve stops, so a `session/new` closed before its first prompt
  cannot be loaded.)
  - **Only a serve that is still ours is stopped.** First tau-acp reads the session's record
    file (`$TAU_HOME/serve/<id>.json`): if it is gone, nothing is running; if it names another
    pid, another serve holds the session now (a dead session that was started again by someone
    else), it is left running, and the reason is logged. Only a record naming our pid is passed to
    `--stop`, which itself checks the pid over the socket. tau-acp never signals a pid itself: a
    pid can be reused. A serve that does not stop within 5 seconds is logged and left.
  - **A session that is dead** (its serve exited, or its stream was cut off) is released the same
    way: the check above is what tells a stale record from a successor.
  - **Going away during `session/new` or `session/load`** (stdin closes, a signal) lets the
    `tau serve -d` that is starting finish, then stops the serve it started, so none is orphaned.
    A serve that was already running (`started` false) is never stopped.
  - **`session/close` and shutdown always release everything:** if the serve is already gone
    and the abort fails, that is logged, and the MCP servers and the relay directory are still
    released. Shutdown releases the sessions together, and one session's failure does not
    skip another's.
- **`record["started"] == false`** means another process already serves the session. tau-acp
  replies with an error that names the pid and `tau serve --stop --session ID`. It never attaches,
  and never stops it.
- **User serve args:** everything after `--` on tau-acp's command line goes to every
  `tau serve`. Examples: `--set 'permissions.ask=["bash"]'`, or `-e` for more extensions. The
  editor's agent configuration is where a user puts them.
- **Model:** `tau serve` takes the model from τ's config (`default_model`). τ's error passes
  through unchanged when none is set.

### The extension and the relay (MCP tools inside τ)

- **The extension is loaded only when there is something to relay.** With no MCP servers, or
  servers that list no tools, tau-acp passes neither `-e` nor `TAU_ACP_RELAY`: τ's tool set is
  then exactly what it is without tau-acp.
- **The extension is never an entry point.** An installed `tau.extensions` entry point would
  load into every τ session in the environment. τ loads `tau_acp/_extension.py` only because
  tau-acp names it with `-e PATH`.
- **Startup handoff:**
  - before starting the serve, tau-acp makes a private directory (mode 700, under
    `$XDG_RUNTIME_DIR` or the temporary directory, with a short name for the socket path limit)
    holding
    `tools.json`, the MCP tools it has collected, and `relay.sock`, which tau-acp listens on;
  - it passes the directory in the environment as `TAU_ACP_RELAY`;
  - the extension reads `tools.json` **at import**. This is necessary, because τ reads an
    extension's tools once, when the driver is built.
- **Failure at import:** if `TAU_ACP_RELAY` is missing or unreadable, the import raises an
  error that says so. τ then refuses to start, and tau-acp reports that error.
- **Tool calls:** a call opens one relay connection and sends one JSON line
  `{"tool": <τ tool name>, "args": <args_raw text>}`. It reads one JSON line back:
  `{"ok": <text>}` or `{"err": <text>}`.
  - If τ cancels the call (the cursor is aborted), the connection is closed.
  - tau-acp then sends MCP `notifications/cancelled` for the request it forwarded.
- **τ tool names:** `mcp__<server>__<tool>`, with each part reduced to `[A-Za-z0-9_-]`. A name
  longer than 64 characters, or two tools mapping to one name, makes `session/new` fail with an
  error that names them.
- **MCP results:** τ tool results are strings, so `content[]` text blocks are joined with
  newlines.
  - A non-text block becomes a line `[<type> content omitted: τ tool results are text]`, so the
    model sees that something was dropped.
  - `structuredContent` with no text blocks is sent as its JSON.
  - `isError: true` becomes `{"err": …}`.

### MCP servers (stdio only)

- **When they start:** at `session/new` and `session/load`, before `tau serve` starts.
  - Each server is exec'd without a shell, using its `command`, `args` and `env` (added to
    tau-acp's environment), in the session's `cwd`.
- **Handshake:** the 2025-11-25 `initialize` handshake, then `notifications/initialized`, then
  `tools/list`, following `nextCursor` until it is exhausted.
- **Failures:** any failure (spawn, handshake, list, a 60-second timeout of either) ends the ACP
  request with `-32603` and an error naming the server. Servers already started are stopped.
  `tools/list` is only sent when the server declared `capabilities.tools`. `env` values never appear in errors or logs.
- **What a server sends that is wrong:**
  - A line that is not a JSON object, or is over 64 MiB (it is discarded through its newline),
    fails every request waiting on that server with `MCP server <name> sent a malformed message
    (...)`, because it cannot be told which request it answered. The server is *not* given up
    on: its later requests go through.
  - A response whose `error` or `result` is not an object, and a tool result whose `content` is
    not a list of objects, fails that one call with `MCP server <name> returned a malformed
    result`.
  - A request from the server is answered `-32601`, except `ping`, which is answered with `{}`.
  - "MCP server <name> exited" is said only when the process's stream really ended.
- **Lifetime:** the servers live as long as the ACP session. They are stopped (stdin closed,
  then SIGTERM, then SIGKILL after a grace period) on session close or bridge exit. Each runs in
  its own process group, and the signals go to the group, so what the server started stops with it.
- **Server names** must be unique within one request (`-32602`).
- **`http`/`sse` entries:** these get a `-32602` error ("tau-acp supports stdio MCP servers only"). tau-acp advertises
  `mcpCapabilities: {http: false, sse: false}`.

## ACP methods

| method | phase | behaviour |
|---|---|---|
| `initialize` | A1 | answers `protocolVersion: 1` (it echoes 1; for any other version it still answers 1), `agentCapabilities {loadSession: true, promptCapabilities {image: false, audio: false, embeddedContext: true}, mcpCapabilities {http: false, sse: false}, sessionCapabilities {list: {}, resume: {}, close: {}}}`, `authMethods: []` and `agentInfo {name: "tau-acp", version}`. |
| `session/new` | A1 (MCP in A2) | starts the MCP servers, then the serve; connects; reads the snapshot until `Labels`. Uses the lowest open cursor (a new session has cursor 0), calls `watch([cursor])` and sends `available_commands_update`. Returns `sessionId` = τ's session id, and `configOptions`. |
| `session/prompt` | A1 | see "A turn" |
| `session/cancel` | A1 | `abort(cursor)`; the pending prompt answers `cancelled` |
| `session/load` | A1 | starts the serve with `--session ID`, then replays the history ("Replay") before answering. An unknown id gets `-32002`, and so does anything but a session's full id: τ's `--session` also takes an id prefix or a name, but then the serve's session is not the key the editor used, so the serve (if tau-acp started it) is stopped and the answer is `-32002` with `data.sessionId`. A `cwd` that differs from τ's record gets `-32602` (checked once the serve is up, because the record is only known then; a serve started for the check is stopped). A session this connection already holds is replayed, not restarted, if its `mcpServers` are the same (name, command, args, env); if they differ the answer is `-32602` ("session X is already open with different MCP servers; close it first"), because keeping the old tools silently would be wrong. A held session whose serve died is released and started again. Results carry `configOptions`. |
| `session/list` | A3 | `tau.list_sessions($TAU_HOME/sessions, cwd=…)`. Each session is `{sessionId, cwd, title?, updatedAt}`: `title` is τ's name, else the first message cut to one line of at most 80 characters, else omitted; `updatedAt` is the log's modification time, ISO 8601 UTC. `cwd` (when given) must be an absolute directory and filters exactly. No paging: no `nextCursor`, and any `cursor` that is not null gets `-32602` ("tau-acp does not page session/list"). A directory that is not a readable session (τ gives it an `error`) is left out, because ACP requires a `cwd` and the editor could not load it; each one is logged to stderr, so it is never silent. τ has no public function for `TAU_HOME`, so tau-acp reads `$TAU_HOME` (a leading `~` is `$HOME`, relative is an error), else `~/.tau`, as τ documents. |
| `session/resume` | A3 | the same as load, without the replay. It still sends `available_commands_update` and returns `configOptions`. |
| `session/close` | A3 | cancels the prompt in flight (which answers `cancelled`), then stops the serve, the MCP servers and the relay directory. `{}` for an unknown or already closed session. |
| `session/set_config_option` | A3 | the one option, `model`: a `select`, `category: "model"`, `currentValue` = `CursorView.model`, options from `complete(cursor, "model", "")` (each `value` and `name` is τ's model name; if the current model is not among them it is added first, since it is a real value). An unknown `configId`, a non-select `type`, or a value that is not an option gets `-32602`. Otherwise τ's `model` command runs (as a task while events are read) and the answer is the full `configOptions`. A model change that comes from τ's side (a `/model` in a prompt) sends `config_option_update` with the full set; one the editor asked for does not (the answer carries it). If τ names no model (`CursorView.model` is `None`), `configOptions` is left out of the results and the option is not offered. |

- Unknown methods get `-32601`. Unknown notifications are ignored. Any request before
  `initialize` gets `-32600`.
- Only one prompt per session can be in flight. A second one gets `-32602` ("a prompt is already
  running").
- Logs go to stderr. Stdout carries only ACP messages.
- **A peer's mistakes never stop tau-acp.**
  - A response from the editor whose `error` is not `{code: integer, message: string}` fails that one request
    (`-32603` naming the malformed error); it is logged, and reading goes on.
  - A line over the 64 MiB limit is discarded through its end and answered `-32700` (id null);
    reading goes on with the next line. (The stream can be resynchronized at the next newline,
    so nothing else is lost.)
  - When a write to the editor fails (stdout closed), the editor is gone: tau-acp logs it, stops
    reading, closes its sessions and exits 0. It is never reported as a serve that exited.

### A turn (`session/prompt`)

1. **Building the prompt text.** The `prompt` blocks become one text:
   - `text` blocks are used as they are;
   - a `resource_link` becomes `[<name>](<uri>)`;
   - an embedded text `resource` becomes a fenced block headed by its URI;
   - a blob `resource`, `image` or `audio` gets `-32602`.

   Consecutive blocks are joined with a blank line.
2. **A line that starts with `/`** goes to `parse_line(cursor, line)`:
   - `UnknownCommand` sends it as text;
   - `ValueError`/`LookupError` gets `-32602` with τ's message;
   - `missing` arguments get `-32602` naming them;
   - anything else runs as a task (`parsed.run`) while events keep being read. Its result
     string, if any, is sent as an `agent_message_chunk`.

   Everything else goes to `send(cursor, text)`.
3. **Reading events.** τ's events always have a reader. Between prompts a background task reads them: it announces a model change that came from τ's side (`config_option_update`), sends a `Notice` or the nodes another client adds on this cursor's line, and notes the end of the stream (the session is dead from then on: its next request answers `-32603`, and `session/load` starts it again). A prompt pauses that task, hears what is already held, and reads the events itself until rest. It hands the reading back when it ends. So a prompt never starts on stale events.
   - **A node is this line's if its parent is the line's tip** (the cursor's `at`, then each node
     of the line). The stream carries every cursor's nodes: those of another cursor, whose
     parent is not the tip, are logged to stderr and not translated. (A cursor opened at the
     same node as ours, branching from our tip, could not be told apart. tau-acp attaches to
     one cursor, and an extension that opens others at our tip is not supported.)
4. **Waiting for the end.** Events are read until **rest**: the first non-busy `CursorView` of
   the cursor after the call returned. This is τ's own `tau -p` rule. `ending == Done` on a
   view that is still busy is not the end.
5. **Stop reason**, from the rest view:
   - `Ending.Aborted` → `cancelled`.
   - `Ending.Failed(error)` → JSON-RPC error `-32603` with the error text.
   - `Ending.Halted(reason)` → JSON-RPC error `-32603` with "halted: <reason>".
   - `None` → JSON-RPC error `-32603` with "the cursor rested with no recorded ending".
   - `Ending.Done` → use the turn's last Assistant node's `stop`: `End` → `end_turn`;
     `Length` → `max_tokens`; `Refused` → `refusal`; anything else → `-32603` naming it.
     Before that, if pending `items` remain or auto-step is off, the result is `-32603` with
     "stopped before finishing: …" (τ's `tau -p` verdict).
   - After `session/cancel`, the answer is always `cancelled`, as ACP requires, whatever fails
     after the cancel (the stream, a command, the stop reason). If the failure was the serve's
     stream ending, the session is still marked dead. A cancel that comes while the prompt's
     `/` line is still being parsed runs no command.
   - A prompt that ends `Ending.Aborted` without a cancel from the editor means τ aborted it. If
     τ's event stream ends within 10 seconds (τ winds down for up to 3 seconds before it ends
     the streams), the serve exited: `-32603` and the session is dead. If not, `-32603` says τ
     aborted the turn on its own. The pid is not looked at: a serve's process can outlive its
     streams.

### Events to `session/update`

| τ event | ACP update |
|---|---|
| `Live(TextDelta)` | `agent_message_chunk` (text) |
| `Live(ThinkingDelta)` | `agent_thought_chunk` |
| `Appended` Assistant node | the node's text (its text parts joined) not already streamed goes as `agent_message_chunk` (thinking the same way, as `agent_thought_chunk`, without the visible line): if the streamed text is a prefix, only the rest is sent; if not, the final text is sent with a visible `[tau-acp: the streamed text was incomplete; the final text follows]` line in front. Each `Part.Call` → `tool_call`. |
| `Appended` User node from another author (a hook, an extension) | `user_message_chunk` |
| `Appended` Results node | `tool_call_update`, status `completed` (`Ok`) or `failed` (`Err`, `Interrupted`), with the text as `content`, and `rawOutput` |
| `Live(ToolProgress)` | `tool_call_update`, status `in_progress`, with the **last 65,536 characters** of the output so far as `content`. (A running call's output can grow without bound; resending all of it for every chunk would be quadratic. The complete output is in the final `completed` update.) |
| `Notice(text)` | `agent_message_chunk` with the text `\n[τ] <text>\n` |
| an open question | see "Questions" |

- **`toolCallId`** is `"<assistant node id>:<call id>"`. Provider call ids can repeat, so the
  node id is included.
- **`title`** is `<tool>` plus a short argument summary: a path, a command, or `args_raw` cut
  to 80 characters.
- **`kind`:** `read` → read; `write`, `edit` → edit; `grep`, `find` → search; `bash` → execute;
  everything else → other.
- **`locations`:** for path tools, `[{path}]`, resolved against the cwd.
- **`rawInput`:** the parsed arguments, or the raw text if they don't parse.

### Questions

- Each `OpenQuestion` on the cursor's views is handled once. Questions addressed to another
  client are left alone.
- **Permissions questions** (extension `permissions`, enum `yes|always|no`) send
  `session/request_permission`, with options `allow_once`/`yes`, `allow_always`/`always` and
  `reject_once`/`no`. It runs as a task, so events keep flowing.
  - τ's question carries no call id, so tau-acp finds the call by the question's text. τ writes
    `<shown>\n(ask rule: ...)`, where `<shown>` is a line `<tool>: <resolved absolute path>` per
    path for a path tool (`read write edit grep find`; paths resolved against the session cwd
    with symlinks followed, as τ does), `bash: <command>`, or `<tool>: <args_raw>` for any
    other. tau-acp builds that text for every call in the Assistant node at `view.at`. The
    `toolCall.toolCallId` is the call whose text the question starts with, among calls not
    asked about yet; calls with identical text are taken in turn.
  - Nothing is guessed. If no call matches, or the question could be about two calls with
    different texts, it is `"question:<id>"` with the question text as the title.
  - **Either way the question's text is the `content` of the `toolCall`**, so the editor shows
    exactly what τ asks, whatever the match.
  - The answer goes to `answer(id, value)`. `cancelled`, an error response or an unknown option
    is answered with `None` (decline).
- **Any other question** is declined (`answer(id, None)`), and an `agent_message_chunk` says so:
  `[tau-acp] declined a question ACP cannot carry: <text>`. It is never left open, because the
  turn could not end.

### Replay (`session/load`)

The cursor's line, `walk(view.at)`, is sent in order:

- **User** nodes → `user_message_chunk`.
- **Assistant** nodes, part by part:
  - Thinking → `agent_thought_chunk`;
  - Text → `agent_message_chunk`;
  - Call → `tool_call` (status `pending`).
- **Results** nodes → `tool_call_update` (final status).
- **System and Config** nodes are skipped.

The response comes only after the last notification.

## Failure handling

- **The serve exits, or the socket drops** (`ConnectionError` from the client):
  - a pending prompt gets `-32603` ("tau serve for session X exited: …");
  - the session is marked dead, and later requests on it (`session/prompt`,
    `session/set_config_option`) get the same error;
  - `session/load` or `session/resume` releases the dead entry and starts the serve again (with
    the request's `mcpServers`); `session/close` releases it. Until then its MCP servers and
    relay directory stay, like any session's.
  - A serve told to stop (SIGTERM) first aborts its cursor, so a turn can end `Aborted` when
    the editor did not cancel. Nothing else is attached, so tau-acp waits up to 2 seconds
    for the serve to exit: if it does, that is the error above, and if not it is `-32603`
    ("tau serve aborted the turn … on its own"). A cancel is `cancelled` only after
    `session/cancel` or `session/close`.
- **An MCP server dies mid-session:** its tools answer `{"err": "MCP server <name> exited"}`.
  τ records an `Err` result, and the model sees it. Nothing is restarted behind the user's back.
- **The editor closes stdin, or sends SIGTERM/SIGINT:** every session is closed as for
  `session/close`, then tau-acp exits 0.

## Testing

- **Real processes, no mocks.** Each test gets its own `TAU_HOME` and `HOME` (a short temporary
  path, because socket paths are capped at 107 bytes).
- **The model** is τ's built-in `mock` provider, configured in `config.json`.
- **Tool calls** come from a test extension passed through tau-acp's serve args
  (`-- -e tests/scripted.py`). Its `pre_sample` hook drafts an Assistant node holding calls.
  This is documented τ behaviour.
- **The editor side is played by the official ACP Python SDK** (`agent-client-protocol`), a
  dev dependency, as an independent implementation. Methods the SDK predates are exercised with
  raw JSON-RPC lines.
- **MCP servers in tests** are small stdio scripts under `tests/`.
- **The gate** is `scripts/check.sh`: ruff check, ruff format check, and pytest.

## Phases

- **A1 — the bridge:**
  - JSON-RPC over stdio;
  - `initialize`, `session/new` (no MCP), `session/prompt`, `session/cancel`, `session/load`;
  - events to updates, questions and permissions, stop reasons;
  - `available_commands_update`;
  - the CLI (`tau-acp [-- serve args]`);
  - session teardown on EOF.
- **A2 — MCP:**
  - the stdio MCP client and server lifetime;
  - the relay and `_extension.py`;
  - tool naming, result conversion, cancellation;
  - `-32602` for http/sse.
- **A3 — the rest of the stable methods:** `session/list`, `session/resume`, `session/close`,
  the model config option, and failure handling (serve death; MCP death was done in A2).
