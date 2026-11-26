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
- τ 0.18 (`ffwf-tau>=0.18,<0.19`): wire protocol 3, which must match τ's exactly.
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
- `audio` prompts.
- Images in tool results as ACP `image` content blocks. τ 0.18's `Client.image(sha256)` can
  fetch the bytes, so this can be built; a result's images are still a line each,
  `[image WxH mime]`, after its text.
- Questions whose answer is not one string, boolean, number or choice of strings (a list, say):
  an ACP form has no field for them.

**What the editor can do is asked, not assumed.** `initialize`'s `clientCapabilities` say whether
the editor takes `elicitation/create` (form mode) and ACP's `notice` update, both of which ACP
marks unstable or gates by capability. Without the capability tau-acp does the plain thing (a
question is declined with a line in the chat, a notice is a chat line); with it, the richer one.

**τ itself is never changed.** tau-acp uses only τ's public API (`tau`, `tau.cli`). If a
behaviour seems to need a τ change, that is a stop-and-ask, not a workaround.

## Processes

```
editor ──stdio (ACP JSON-RPC)──> tau-acp ──Unix socket (τ wire, RemoteClient)──> tau serve (one per ACP session)
                                  │  ▲                                              │ loads tau_acp/_extension.py via -e PATH
                                  │  └──────── relay socket (one per session) ──────┘ (MCP tool calls)
                                  └──stdio (MCP JSON-RPC)──> each editor-requested MCP server
```

- **Each ACP session is one `tau serve` process, tau-acp's child:**
  - `python -m tau serve --json [--session ID] -e <path of tau_acp/_extension.py> [user serve
    args]`, in the foreground (not `-d`), in its own session (`start_new_session`), stdin
    `/dev/null`, stderr shared with tau-acp's (the editor's log);
  - its `cwd` is the session's `cwd` (a new session) or comes from τ's session record (a loaded
    session);
  - tau-acp reads the first line of its stdout, the JSON record (or τ's `{"error": ...}`, and
    then it exits), checks that the record's pid is the child's, and connects with
    `tau.RemoteClient.connect(record["socket"], "tau-acp")`. Whatever it prints later is read
    and logged, so a full pipe never blocks it.
- **Stopping:** tau-acp stops the serve when the session closes, or when the editor closes
  stdin or tau-acp is signalled. The session log stays on disk, so `session/load` can come back
  to it. (Exception, found in A1: τ discards a session that never received input when its serve
  stops, so a `session/new` closed before its first prompt cannot be loaded.)
  - **A serve is stopped by signalling our own child, never a pid read back.** tau-acp holds a
    pidfd for each child it starts (`tau_acp/child.py`), opened when it starts, and sends SIGTERM
    through it; τ aborts what runs, winds down (up to 3 s), removes its record and socket, and
    exits. A serve that has not exited 5 s after SIGTERM gets SIGKILL, and the release logs it (τ
    replaces the stale record and socket it leaves the next time the session is served). A
    child that has exited is never signalled: once reaped, its pid may be someone else's. On a
    platform without pidfds (anywhere but Linux) the signal goes to the pid while asyncio has
    not seen it exit.
  - **A session that is dead** (its serve exited, or its stream was cut off) is released the same
    way. Its successor, if someone else served the session meanwhile, is another process
    and is never signalled: tau-acp only ever signals its own children.
  - **Going away during `session/new` or `session/load`** (stdin closes, a signal) lets the start
    that is under way finish, then stops the serve it started, so none is orphaned.
  - **`session/close` and shutdown always release everything:** if the serve is already gone
    and the abort fails, that is logged, and the MCP servers and the relay directory are still
    released; `session/close` then answers `-32603` (the session is closed all the same). Shutdown releases the sessions together, and one session's failure does not
    skip another's.
  - **Signals:** SIGTERM, SIGINT and SIGHUP (the terminal closed) each start the shutdown. From
    the moment the shutdown starts (a signal, or stdin closing), those three are ignored, so
    a second Ctrl-C can neither cancel the shutdown nor kill tau-acp before it is done. The
    exit status is 0. Children get the default actions back before they exec.
  - **SIGKILL (Linux):** each child is started with `PR_SET_PDEATHSIG` = SIGTERM, so when
    tau-acp dies without stopping it, the kernel sends the serve (and each MCP server) the
    SIGTERM tau-acp would have sent. **Elsewhere there is no such tie: a SIGKILLed tau-acp leaves
    its serves running** (an MCP server, whose stdin then closes, is expected to exit).
- **A session another process serves or holds** (another editor, a TUI) is refused by τ itself:
  the foreground serve fails with τ's `Leased` error ("session ID is in use by another process;
  to work on a copy of it: tau --fork ID"), which tau-acp passes on as `-32603`. tau-acp never
  attaches to a serve it did not start, and never stops one.
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
  its own process group. The signals go to the whole group while the server runs; when the
  stop begins, tau-acp also takes pidfds for the other members of the group (from `/proc`), and
  those still running get SIGTERM once the server has exited. So what the server started stops
  with it, also when the server exits as soon as its stdin closes. A server that exited before the
  stop began (it crashed) is not signalled, and neither is anything it left: its pid and group id
  may name someone else's processes by then. Processes the server starts while it is being
  stopped are not in the snapshot. Off Linux there is no `/proc` snapshot.
- **Server names** must be unique within one request (`-32602`).
- **`http`/`sse` entries:** these get a `-32602` error ("tau-acp supports stdio MCP servers only"). tau-acp advertises
  `mcpCapabilities: {http: false, sse: false}`.

### Plan: τ's `mcp` built-in replaces the relay

τ 0.18.0 is getting an `mcp` built-in (D24 section 12), ported from this relay: it starts MCP
servers inside τ, offers their tools as `mcp__SERVER__TOOL` (the same names), sends
`notifications/cancelled` on an abort, and runs through `permissions` like any tool. When it is
in the τ that tau-acp pins, tau-acp stops starting MCP servers itself:

- the editor's `mcpServers` go to the serve, which starts them: the relay, `_extension.py`,
  `mcp.py` and `relay.py` are deleted, and with them `TAU_ACP_RELAY` and the `-e` of the
  extension;
- the serve is started with the built-in on (it is off by default: `--set load.mcp=true`);
- a server that fails to start fails `session/new` as today: τ's start refuses with every failing
  server named, and tau-acp passes that error on as `-32603`;
- the tests keep their cases (names, errors, cancellation, permissions, lifetime, `ping`, a
  malformed message) and run them against τ's built-in instead of the relay;
- `http` entries become possible (τ's built-in speaks streamable HTTP): `mcpCapabilities.http`
  turns true and the `-32602` goes. `sse` stays refused.

**What it needs from τ, not built yet:** the built-in reads servers only from files
(`$TAU_HOME/mcp.json`, the project's `.mcp.json` and `.tau/mcp.json`). tau-acp must hand τ one
session's servers without writing into the user's files or the project's, so τ needs a route for
a client or the command line to give a serve its servers: `tau serve --mcp-config PATH`, or the
extension's option. Until τ has one, tau-acp keeps its relay. Two differences to watch when
switching: the editor's `env` values are added to τ's environment for that server in both; and
the relay's text-only results become τ's images (D30), which ACP can show.

## ACP methods

| method | phase | behaviour |
|---|---|---|
| `initialize` | A1 | keeps the editor's `clientCapabilities` (`elicitation.form`, `session.notices`); answers `protocolVersion: 1` (it echoes 1; for any other version it still answers 1), `agentCapabilities {loadSession: true, promptCapabilities {image: true, audio: false, embeddedContext: true}, mcpCapabilities {http: false, sse: false}, sessionCapabilities {list: {}, resume: {}, close: {}}}`, `authMethods: []` and `agentInfo {name: "tau-acp", version}`. |
| `session/new` | A1 (MCP in A2) | starts the MCP servers, then the serve; connects; reads the snapshot until `Labels`, keeping its `Commands`. Uses the lowest open cursor (a new session has cursor 0), calls `watch([cursor])` and sends `available_commands_update` (from the `Commands` event; see "Commands"). Returns `sessionId` = τ's session id, and `configOptions`. |
| `session/prompt` | A1 | see "A turn" |
| `session/cancel` | A1 | `abort(cursor)`; the pending prompt answers `cancelled` |
| `session/load` | A1 | starts the serve with `--session ID`, then replays the history ("Replay") before answering. An unknown id gets `-32002`, and so does anything but a session's full id: τ's `--session` also takes an id prefix or a name, but then the serve's session is not the key the editor used. The id is resolved with `tau.find_session` before anything starts, and anything but an exact id is answered `-32002` with `data.sessionId` (naming the session τ would have opened, if any). The record's session is checked again once the serve is up. A `cwd` that differs from τ's record gets `-32602` (checked once the serve is up, because the record is only known then; a serve started for the check is stopped). A session this connection already holds is replayed, not restarted, if its `mcpServers` are the same (name, command, args, env); if they differ the answer is `-32602` ("session X is already open with different MCP servers; close it first"), because keeping the old tools silently would be wrong. A held session whose serve died is released and started again. Results carry `configOptions`. |
| `session/list` | A3 | `tau.list_sessions($TAU_HOME/sessions, cwd=…)`. Each session is `{sessionId, cwd, title?, updatedAt}`: `title` is τ's name, else the first message cut to one line of at most 80 characters, else omitted; `updatedAt` is the log's modification time, ISO 8601 UTC. `cwd` (when given) must be an absolute directory and filters exactly. No paging: no `nextCursor`, and any `cursor` that is not null gets `-32602` ("tau-acp does not page session/list"). A directory that is not a readable session (τ gives it an `error`) is left out, because ACP requires a `cwd` and the editor could not load it; each one is logged to stderr, so it is never silent. τ has no public function for `TAU_HOME`, so tau-acp reads `$TAU_HOME` (a leading `~` is `$HOME`, relative is an error), else `~/.tau`, as τ documents. |
| `session/resume` | A3 | the same as load, without the replay. It still sends `available_commands_update` and returns `configOptions`. |
| `session/close` | A3 | cancels the prompt in flight (which answers `cancelled`), then stops the serve, the MCP servers and the relay directory. `{}`; `{}` too for an unknown or already closed session. If the serve was already gone (found dead, or its process had exited) or dies during the close, everything is released all the same, and the answer is `-32603` naming why, "(the session is closed)", with `data {sessionId}`: what ran in the serve was lost. |
| `session/set_config_option` | A3 | the one option, `model`: a `select`, `category: "model"`, `currentValue` = `CursorView.model`, options from `complete(cursor, "model", "")` (each `value` and `name` is a completion's `value`, τ's model name; if the current model is not among them it is added first, since it is a real value). An unknown `configId`, a non-select `type`, or a value that is not an option gets `-32602`. Otherwise τ's `model` command runs (as a task while events are read) and the answer is the full `configOptions`. A model change that comes from τ's side (a `/model` in a prompt) sends `config_option_update` with the full set; one the editor asked for does not (the answer carries it). If τ names no model (`CursorView.model` is `None`), `configOptions` is left out of the results and the option is not offered. |

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
    reading, closes its sessions and exits 0. It is never reported as a serve that exited. This
    holds also when the write that finds it gone is the answer to a malformed line.

### A turn (`session/prompt`)

1. **Building the prompt.** The `prompt` blocks become one text and some images:
   - `text` blocks are used as they are;
   - a `resource_link` becomes `[<name>](<uri>)`;
   - an embedded text `resource` becomes a fenced block headed by its URI;
   - an `image` block becomes one of τ's `ImageInput`s (its base64 `data`), sent with the text
     (τ puts a message's images after its text; a prompt of images alone has the empty text).
     τ reads the format and size from the bytes: anything but a PNG, JPEG, GIF or WebP of at most
     `tau.MAX_INPUT_BYTES` (20 MiB), or more than `tau.MAX_INPUT_IMAGES` (20), gets `-32602`;
   - a blob `resource` or `audio` gets `-32602`.

   Consecutive text blocks are joined with a blank line. A model that takes no images makes τ
   refuse the input (`Refused`): that is `-32602` with τ's reason, and nothing is queued. The
   editor is told `promptCapabilities.image: true` whatever the model, because the model can
   change in the session.
2. **A line that starts with `/`** goes to `parse_line(cursor, line)`. (A command takes no
   images: a prompt with both is `-32602`.)
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
     Before that, if pending `items` remain, the result is `-32603` with "stopped before
     finishing: …" (τ's `tau -p` verdict). Then **τ's step limit** → `max_turn_requests`: the
     `step_limit` extension turns auto-step off after `limit` steps, each one reply. Its mark is
     auto-step off after a reply that called tools (`Stop.Tools`), with the prompt's reply count
     a multiple of the limit (read with `Client.options(cursor, "step_limit")`; the extension
     counts again after it fires). Any other auto-step off (a hook, a step that did not move,
     or no `step_limit` loaded) is `-32603` "stopped before finishing: auto-step is off". A
     hook that turns auto-step off on exactly such a step cannot be told from the limit.
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
| `Commands(forms)` | `available_commands_update`, once the editor knows the session and whenever the list differs from the last one sent (see "Commands") |
| `Appended` Assistant node | the node's text (its text parts joined) not already streamed goes as `agent_message_chunk` (thinking the same way, as `agent_thought_chunk`, without the visible line): if the streamed text is a prefix, only the rest is sent; if not, the final text is sent with a visible `[tau-acp: the streamed text was incomplete; the final text follows]` line in front. Each `Part.Call` → `tool_call`. Then, if the reply's `meta.usage` has `input` and `output` and the model's config has a `context_window`, a `usage_update` (see "Usage"). |
| `Appended` User node from another author (a hook, an extension) | `user_message_chunk` for its text (left out when empty), then one for each image, as an `image` block (bytes from `Client.image`) |
| `Appended` Results node | `tool_call_update`, status `completed` (`Ok`) or `failed` (`Err`, `Interrupted`), with the text as `content`, and `rawOutput`. An `Ok` with images (τ's protocol 2) adds a line per image after its text, `[image WxH mime]`, as τ's TUI shows it: τ's wire carries an image's reference (mime, sha256, size), not its bytes, so it cannot be an ACP `image` block. |
| `Live(ToolProgress)` | `tool_call_update`, status `in_progress`, with the **last 65,536 characters** of the output so far as `content`. (A running call's output can grow without bound; resending all of it for every chunk would be quadratic. The complete output is in the final `completed` update.) |
| `Notice(text, cursor, extension, level)` | a notice about another cursor is logged and dropped. For an editor that advertised `session.notices`: ACP's `notice` update, `severity` = `level`, `title` = `[<extension or τ>] <first line>` (cut to 80), `description` = the whole text when it has more than that line. Otherwise an `agent_message_chunk` with `\n[<extension or τ>] <text>\n`, the level named (`warning: `, `error: `) when it is not `info`. |
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
- **Any other question** is asked through ACP's `elicitation/create` (form mode), when the editor
  advertised `clientCapabilities.elicitation.form`, and its answer type is one a form has a field
  for: τ's JSON Schema is a string, a boolean, an integer, a number, or an `enum` of strings.
  The form is one field, `answer`, titled "Answer", required, with the question's text as the
  `message`; the request carries the `sessionId`. An `accept` answers the question with the
  field's value. `decline`, `cancel`, an error response, or a malformed result answer it `None`
  (decline). A value τ refuses (`Refused`: the wrong type, or not one of the choices) also
  declines, because a question left open keeps the cursor busy.
- **Otherwise** (no `elicitation.form` capability, or an answer type a form cannot ask: a list, an
  object, a union) the question is declined (`answer(id, None)`), and an `agent_message_chunk`
  says so: `[tau-acp] declined a question ACP cannot carry: <text>`. It is never left open,
  because the turn could not end.
- ACP marks elicitation unstable in the SDK that tau-acp's tests use (`agent-client-protocol`
  1.0.0). It is used only when the editor opts in with its capability, so an editor that does not
  know it is never sent one.

### Commands

τ's stream opens with a `Commands` event (before the `CursorView`s) and sends one again whenever
the command forms change: an extension's start, `/reload`, a skill file, `tree.reassemble()`.
tau-acp keeps the latest, sends the `available_commands_update` right after the response of
`session/new`, `session/load` or `session/resume` (the editor does not know the session id
before), and sends another whenever a `Commands` makes the list differ from the one last sent.
It never calls `commands()`.

**`/new` is not tau-acp's.** A client implements a command only where it can act on it. A new
session in ACP is `session/new`, which the editor sends; tau-acp has no `/new` command and does
not refuse one. A prompt `/new` is τ's to parse: if τ has such a command it runs, and if not it
is text for the model, as any unknown `/word`. (The same holds for `/resume` and `/fork`.)

### Usage

After a reply that reported `meta.usage` (τ's normalized `input` and `output` counts), tau-acp
sends ACP's `usage_update`: `used` = `input + output` of that reply (the prompt τ sent and what
came back: the context now, as τ's own `ctx %` is figured), `size` = the model's `context_window`.
`cost` is not sent.

τ's wire does not carry a model's `context_window`: it is in τ's config. tau-acp reads the
config files with `tau.load_config(cwd, environ, ask=None)` once per session, as the serve does
(without the serve's `--set` overrides). No `context_window` for the model, or a config that
cannot be read from there (an untrusted project, an invalid file; logged once), means no
`usage_update`: `size` is required and unknown. A reply with no `usage`, or one that is not the
documented shape, sends nothing (the latter is logged).

### Replay (`session/load`)

The cursor's line, `walk(view.at)`, is sent in order:

- **User** nodes → `user_message_chunk` for the text (left out when empty and there are images),
  then one per image as an `image` block, its bytes fetched with `Client.image(sha256)`.
- **Assistant** nodes, part by part:
  - Thinking → `agent_thought_chunk`;
  - Text → `agent_message_chunk`;
  - Call → `tool_call` (status `pending`).
- **Results** nodes → `tool_call_update` (final status).
- **System and Config** nodes are skipped.
- Replay sends no `usage_update`.

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
- **The editor closes stdin, or sends SIGTERM, SIGINT or SIGHUP:** every session is closed as
  for `session/close`, then tau-acp exits 0. Further signals are ignored until it has (see
  "Signals" above).

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
- **A4 — τ 0.18 (protocol 3), release candidate 0.1.0:**
  - user images both ways (image prompt blocks, `user_message_chunk` image blocks in replay);
  - `Commands` instead of `commands()`; attributed notices; `Completion` items;
  - questions through `elicitation/create`; `usage_update`;
  - release prep (`publish.yml`); the `mcp` built-in switch-over is planned above, not done.
