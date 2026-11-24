"""The τ extension that carries MCP tools into a session. tau-acp loads it with ``-e``; it is
deliberately not an entry point, because that would load it into every τ session.

It runs inside τ's process, so it imports only the standard library and ``tau``, and it is one
self-contained file. It reads ``tools.json`` from the directory named by ``TAU_ACP_RELAY`` at
import (τ reads an extension's tools once, when the driver is built). Each call goes to
tau-acp over ``relay.sock`` in that directory: one connection per call, one JSON line each way.
Closing the connection (the call was cancelled) tells tau-acp to cancel the MCP request.
"""

import asyncio
import json
import os
from pathlib import Path

from tau import Outcome

name = "tau_acp"

LINE_LIMIT = 1 << 27


def _load(directory: str | None) -> tuple[str, list[dict]]:
    if not directory:
        raise RuntimeError(
            "tau-acp relay: TAU_ACP_RELAY is not set. This extension is loaded by tau-acp "
            "alone (it passes -e and the variable together)."
        )
    try:
        tools = json.loads((Path(directory) / "tools.json").read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(
            f"tau-acp relay: cannot read tools.json in {directory}: {error}"
        ) from None
    return str(Path(directory) / "relay.sock"), tools


SOCKET, _TOOLS = _load(os.environ.get("TAU_ACP_RELAY"))


class RelayTool:
    """One MCP tool, as a τ tool."""

    def __init__(self, entry: dict) -> None:
        self.name = entry["name"]
        self.description = entry["description"]
        self.parameters = entry["parameters"]

    async def call(self, args_raw: str, *, progress=None, call_id=None):
        try:
            reader, writer = await asyncio.open_unix_connection(SOCKET, limit=LINE_LIMIT)
        except OSError as error:
            return Outcome.Err(f"tau-acp relay unavailable: {error}")
        try:
            writer.write(json.dumps({"tool": self.name, "args": args_raw}).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
        except OSError as error:
            return Outcome.Err(f"tau-acp relay failed: {error}")
        finally:
            writer.close()  # on cancellation too: tau-acp sees the close as a cancel
        if not line:
            return Outcome.Err("tau-acp relay closed the connection")
        reply = json.loads(line)
        if "ok" in reply:
            return Outcome.Ok(reply["ok"])
        return Outcome.Err(reply.get("err", "tau-acp relay sent no result"))


tools = [RelayTool(entry) for entry in _TOOLS]
