# tau-acp

Lets an editor that speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP v1)
drive a [τ](https://pypi.org/project/ffwf-tau/) session. The editor starts `tau-acp` and talks
to it over stdio. tau-acp starts one `tau serve` per ACP session, on τ's Unix socket, and
translates between the two. [DESIGN.md](DESIGN.md) is the contract.

Everything runs on one machine: tau-acp, the `tau serve` processes it starts, and the files
being edited.

## Status: phase A2

Works now:

- `initialize`, `session/new`, `session/prompt`, `session/cancel`, `session/load` (with replay
  of the conversation).
- Streamed replies and thinking, tool calls with their results and live output, and τ's
  notices.
- Tool permissions: when τ asks (for example with a `permissions.ask` rule), the editor gets a
  `session/request_permission` with allow once / always allow / reject. Any other question τ
  asks is declined, with a line in the chat saying so.
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

Not yet: `session/list`, `session/resume`, `session/close` and the model option (A3).

Not planned: `fs/*` and `terminal/*` client methods (τ's own tools work on the local files),
`http`/`sse` MCP servers (MCP is stdio only), image and audio prompts, and any network transport.

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
- If another process already serves a session, `session/load` fails with an error naming its
  pid and the `tau serve --stop --session ID` command. tau-acp never attaches to a serve it did
  not start.
- A turn ends when τ's cursor rests. Stop reasons: `end_turn`, `max_tokens` and `refusal` from the
  last reply, `cancelled` after `session/cancel`. A failed or halted step is a JSON-RPC error
  (-32603) carrying τ's message.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -U pip            # pip >= 25.1 for --group
.venv/bin/pip install -e . --group dev
PATH=$PWD/.venv/bin:$PATH scripts/check.sh   # ruff check, ruff format --check, pytest
```

The tests run real processes: `python -m tau_acp` spawning real `tau serve` processes on τ's
built-in `mock` provider, each test in its own temporary `TAU_HOME`, driven by the official ACP
Python SDK (`agent-client-protocol`, a dev dependency only) and by raw JSON-RPC lines.
`tests/scripted.py` is a τ extension that makes the mock "call" tools.

## License

MIT; see `LICENSE`.
