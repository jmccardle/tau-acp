"""The relay: a session's MCP servers, their tools as τ tools, and the socket τ calls them by.

A private directory (mode 700) holds ``tools.json`` and ``relay.sock``; the ``_extension``
loaded into ``tau serve`` reads the first and connects to the second (DESIGN.md, "The extension
and the relay").
"""

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from tau_acp.jsonrpc import INVALID_PARAMS, RpcError, log
from tau_acp.mcp import (
    McpClient,
    McpError,
    McpExited,
    McpMalformed,
    McpSpec,
    McpStartError,
    McpTool,
    outcome,
)

EXTENSION = Path(__file__).with_name("_extension.py")
NAME_LIMIT = 64
SOCKET_LIMIT = 100  # sun_path holds 107 bytes
LINE_LIMIT = 1 << 26


def tau_name(server: str, tool: str) -> str:
    """The τ tool name for an MCP tool: ``mcp__<server>__<tool>``, each part reduced to
    ``[A-Za-z0-9_-]``."""
    clean = [re.sub(r"[^A-Za-z0-9_-]", "_", part) for part in (server, tool)]
    return f"mcp__{clean[0]}__{clean[1]}"


class Relay:
    """The MCP servers of one session, and the socket that carries τ's calls to them."""

    def __init__(self, directory: Path, clients: dict[str, tuple[McpClient, str]]) -> None:
        self._directory = directory
        self._clients = clients  # τ tool name -> (server, MCP tool name)
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._servers: list[McpClient] = []

    @classmethod
    async def start(cls, specs: list[McpSpec], cwd: Path) -> "Relay":
        """Start the servers, collect their tools and open the socket. Nothing is left running
        when this raises."""
        started: list[McpClient] = []
        try:
            listed: list[tuple[McpClient, list[McpTool]]] = []
            for spec in specs:
                try:
                    client = await McpClient.start(spec, cwd)
                except McpStartError as error:
                    raise RpcError(-32603, str(error)) from None
                started.append(client)
                try:
                    listed.append((client, await client.tools()))
                except McpStartError as error:
                    raise RpcError(-32603, str(error)) from None
            names, entries = _naming(listed)
            relay = cls(_private_dir(), names)
            relay._servers = started
            await relay._open(entries)
            return relay
        except BaseException:
            await _stop_all(started)
            raise

    @property
    def has_tools(self) -> bool:
        """Whether any MCP server lists a tool: if none does, τ needs no extension."""
        return bool(self._clients)

    @property
    def serve_args(self) -> list[str]:
        """What ``tau serve`` needs to load the extension."""
        return ["-e", str(EXTENSION)]

    @property
    def environ(self) -> dict[str, str]:
        """What it needs in its environment."""
        return {"TAU_ACP_RELAY": str(self._directory)}

    async def _open(self, entries: list[dict[str, Any]]) -> None:
        try:
            (self._directory / "tools.json").write_text(json.dumps(entries))
            socket = str(self._directory / "relay.sock")
            if len(socket) > SOCKET_LIMIT:
                raise RpcError(-32603, f"relay socket path is too long: {socket}")
            self._server = await asyncio.start_unix_server(
                self._connection, socket, limit=LINE_LIMIT
            )
        except BaseException:
            _remove(self._directory)
            raise

    async def stop(self) -> None:
        """Close the socket, stop the servers and remove the directory."""
        if self._server is not None:
            self._server.close()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await _stop_all(self._servers)
        _remove(self._directory)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            await self._serve_one(reader, writer)
        except asyncio.CancelledError:
            pass
        finally:
            self._handlers.discard(task)
            writer.close()

    async def _serve_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One call: the request line, the MCP call; if the peer closes first, the cancel."""
        try:
            line = await reader.readline()
        except ValueError:  # over the limit
            writer.write(
                json.dumps({"err": "tau-acp relay: the call is too long"}).encode() + b"\n"
            )
            await writer.drain()
            return
        if not line:
            return
        call = asyncio.ensure_future(self._call(line))
        closed = asyncio.ensure_future(reader.read())  # returns when the peer closes
        try:
            await asyncio.wait({call, closed}, return_when=asyncio.FIRST_COMPLETED)
            if call.done():
                writer.write(json.dumps(call.result()).encode() + b"\n")
                await writer.drain()
        finally:
            for future in (call, closed):
                future.cancel()
            await asyncio.gather(call, closed, return_exceptions=True)

    async def _call(self, line: bytes) -> dict[str, str]:
        try:
            request = json.loads(line)
            server, tool = self._clients[request["tool"]]
            arguments = json.loads(request["args"]) if request["args"].strip() else {}
        except (ValueError, KeyError, TypeError, AttributeError):
            return {"err": "tau-acp relay: malformed call"}
        try:
            return outcome(await server.call(tool, arguments))
        except McpExited as error:
            return {"err": str(error)}
        except McpMalformed as error:  # "sent a malformed message ...", "returned a malformed ..."
            return {"err": f"MCP server {server.name} {error}"}
        except McpError as error:
            return {"err": f"MCP server {server.name}: {error}"}


def _private_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not Path(base).is_dir():
        base = tempfile.gettempdir()
    return Path(tempfile.mkdtemp(prefix="tacp", dir=base))


def _naming(
    listed: list[tuple[McpClient, list[McpTool]]],
) -> tuple[dict[str, tuple[McpClient, str]], list[dict[str, Any]]]:
    """τ names for every tool; ``-32602`` for a name over 64 characters or a collision."""
    names: dict[str, tuple[McpClient, str]] = {}
    entries: list[dict[str, Any]] = []
    for client, tools in listed:
        for tool in tools:
            name = tau_name(client.name, tool.name)
            if len(name) > NAME_LIMIT:
                raise RpcError(
                    INVALID_PARAMS,
                    f"MCP tool {tool.name!r} of server {client.name!r} would be named {name!r}, "
                    f"{len(name)} characters (the limit is {NAME_LIMIT})",
                )
            if name in names:
                other = names[name]
                raise RpcError(
                    INVALID_PARAMS,
                    f"MCP tools {other[1]!r} of server {other[0].name!r} and {tool.name!r} of "
                    f"server {client.name!r} are both named {name!r}",
                )
            names[name] = (client, tool.name)
            entries.append(
                {"name": name, "description": tool.description, "parameters": tool.schema}
            )
    return names, entries


async def _stop_all(clients: list[McpClient]) -> None:
    for client in clients:
        try:
            await client.stop()
        except Exception as error:  # the others must still be stopped
            log(f"MCP server {client.name}: stopping it failed: {error!r}")


def _remove(directory: Path) -> None:
    """Remove the relay directory; what cannot be removed is logged."""
    shutil.rmtree(
        directory, onexc=lambda _function, path, error: log(f"cannot remove {path}: {error!r}")
    )
