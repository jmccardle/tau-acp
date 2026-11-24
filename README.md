# tau-acp

Lets an editor that speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP v1)
drive a [τ](https://pypi.org/project/ffwf-tau/) session. The editor starts `tau-acp` and talks
to it over stdio. tau-acp starts one `tau serve` per ACP session, on τ's Unix socket, and
translates between the two. [DESIGN.md](DESIGN.md) is the contract.

Everything runs on one machine: tau-acp, the `tau serve` processes it starts, and the files
being edited.

## Status: phase A3

Works now:

- `initialize`, `session/new`, `session/prompt`, `session/cancel`, `session/load` (with replay
  of the conversation), `session/resume` (without it), `session/list` (a project's sessions,
  titled by name or first message, newest first, no paging) and `session/close`.
- A `model` config option, a select of τ's configured models: change it with
  `session/set_config_option`, or with `/model NAME` in a prompt (the editor is told).
- A `tau serve` that dies fails the pending prompt with an error naming the session; the session
  stays dead until `session/load` or `session/resume` starts it again.
- Streamed replies and thinking, tool calls with their results and live output, and τ's
  notices.
- Tool permissions: when τ asks (for example with a `permissions.ask` rule), the editor gets a
  `session/request_permission` with allow once / always allow / reject. The request always shows
  the question's own text, and names the tool call only when the text identifies exactly one.
  Any other question τ asks is declined, with a line in the chat saying so.
- τ's slash commands (`/usage`, `/model NAME`, ...) are advertised to the editor and run when
  typed. An unknown `/word` goes to the model as text.
- Closing stdin (or SIGTERM) stops every `tau serve` it started and exits 0.

- MCP servers, stdio only: the `mcpServers` of `session/new` and `session/load` are started
  (in the session's directory, without a shell) before `tau serve`, and their tools reach the
  model as `mcp__<server>__<tool>`, through a small τ extension tau-acp loads with `-e`. They
  show in the editor like any τ tool and ask permission the same way. Results are text: an
  image or other non-text block becomes a line saying it was omitted. A server that cannot
  start fails `session/new` with an error naming it; one that exits mid-session fails its
  tool calls. `http`/`sse` entries are refused.

Limits, by decision:

- MCP servers are stdio only (`http`/`sse` are refused).
- No `fs/*` or `terminal/*` client methods: τ's own tools work on the local files.
- No image or audio prompts.
- Unstable ACP methods (`session/fork` and others) are not supported.
- Local only: tau-acp, its `tau serve` processes and the files are on one machine, and there is
  no network transport. A session some other process serves is never attached to.
- `session/load` or `resume` of a session this connection already holds, with different
  `mcpServers`, is refused: close it first.

## Setup

```bash
pip install ffwf-tau-acp        # brings ffwf-tau 0.16
```

τ needs a model: set `default_model` in its config (`$TAU_HOME/config.json`, default
`~/.tau/config.json`), because `tau serve` takes no model argument. If there is none, the
editor sees τ's error when it creates a session.

In the editor's agent configuration, the command is `tau-acp` with no arguments. To pass options
to every `tau serve` it starts, put them after `--`:

```
tau-acp -- --set 'permissions.ask=["bash","write","edit"]'
tau-acp -- -e /path/to/extension.py
```

`tau-acp --version` prints the version. Logs go to stderr; stdout carries only ACP messages.

## How it behaves

- An ACP session is a τ session, and its `sessionId` is τ's session id. Its `tau serve` runs
  in the session's `cwd`.
- A `tau serve` stops when tau-acp exits. The session's log stays on disk, so `session/load`
  can come back to it. A session that never received a prompt is discarded by τ when its serve
  stops, so there is nothing to load.
- `session/load` and `session/resume` take a session's full id. (τ accepts a prefix or a name
  on its own command line; the editor's key would then not be the session's, so it is `-32002`.)
- tau-acp only stops a serve that is still the one it started: if a session's serve died and
  something else serves the id now, closing the session leaves that alone.
- If another process already serves a session, `session/load` fails with an error naming its
  pid and the `tau serve --stop --session ID` command. tau-acp never attaches to a serve it did
  not start.
- A turn ends when τ's cursor rests. Stop reasons: `end_turn`, `max_tokens` and `refusal` from the
  last reply, `cancelled` after `session/cancel`. A failed or halted step is a JSON-RPC error
  (-32603) carrying τ's message. A prompt that was cancelled answers `cancelled` whatever
  fails after the cancel.
- Malformed input does not stop tau-acp: a bad response from the editor fails that one request,
  and a bad or over-long line from the editor or an MCP server is skipped and answered or failed
  (see DESIGN.md).
- A running tool's `in_progress` updates carry the last 65,536 characters of its output.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -U pip            # pip >= 25.1 for --group
.venv/bin/pip install -e . --group dev
PATH=$PWD/.venv/bin:$PATH scripts/check.sh   # ruff check, ruff format --check, radon (CC <= 15), pytest
```

The tests run real processes: `python -m tau_acp` spawning real `tau serve` processes on τ's
built-in `mock` provider, each test in its own temporary `TAU_HOME`, driven by the official ACP
Python SDK (`agent-client-protocol`, a dev dependency only) and by raw JSON-RPC lines.
`tests/scripted.py` is a τ extension that makes the mock "call" tools.

## License

MIT; see `LICENSE`.
