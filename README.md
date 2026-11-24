# tau-acp

A bridge that lets editors speaking the [Agent Client Protocol](https://agentclientprotocol.com)
(ACP, JSON-RPC 2.0 over stdio) drive a [τ](https://pypi.org/project/ffwf-tau/) session.

## Status

Scaffold. Nothing works yet beyond `tau-acp --version`; every other invocation exits
with status 2 and a message saying the bridge is not built. Its shape, whether it runs
beside `tau serve` or on the editor's machine, is undecided.

## Planned role

- `tau_acp.bridge`: the ACP agent side over stdio, translating to a τ `RemoteClient`
  that talks to `tau serve`.
- `tau_acp.tools`: exposing MCP tools provided by the editor to τ as tools.

## Requirements

- Python 3.12 or newer
- `ffwf-tau` 0.16.x. The pin is deliberate: τ's wire protocol number must match
  exactly, so each τ minor series needs a matching tau-acp release.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -U pip            # pip >= 25.1 for --group
.venv/bin/pip install -e . --group dev
PATH=$PWD/.venv/bin:$PATH scripts/check.sh   # ruff check, ruff format --check, pytest
```

## License

MIT; see `LICENSE`.
