"""A stdio MCP client: the 2025-11-25 handshake, ``tools/list`` and ``tools/call``.

Messages are newline-delimited JSON-RPC. We offer no client capabilities, so a request from
the server is answered ``-32601``. ``env`` values are never logged or put in an error.
"""

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tau_acp import __version__
from tau_acp.child import Child, close_all, signal_all, tied
from tau_acp.jsonrpc import INVALID_PARAMS, METHOD_NOT_FOUND, TOO_LONG, RpcError, log, read_line

PROTOCOL_VERSION = "2025-11-25"
START_TIMEOUT = 60.0  # the handshake and each tools/list page
STOP_GRACE = 3.0  # between closing stdin, SIGTERM and SIGKILL
READ_LIMIT = 1 << 26


class McpError(Exception):
    """The server answered a request with a JSON-RPC error."""


class McpMalformed(McpError):
    """The server sent something that is not valid JSON-RPC or MCP. The message completes
    ``MCP server <name> ...``. The server is not given up on: a line that cannot be read is
    skipped, and the requests waiting for an answer fail with this."""


class McpExited(Exception):
    """The server process exited."""

    def __init__(self, name: str) -> None:
        super().__init__(f"MCP server {name} exited")


class McpStartError(Exception):
    """A server could not be started; the message names it."""


@dataclass(frozen=True)
class McpSpec:
    """One stdio entry of ``mcpServers``."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class McpTool:
    """A tool a server lists."""

    name: str
    description: str
    schema: dict[str, Any]


def parse_servers(servers: Any) -> list[McpSpec]:
    """The stdio entries of ``mcpServers``; ``-32602`` for anything else."""
    if not isinstance(servers, list):
        raise RpcError(INVALID_PARAMS, "mcpServers must be an array")
    specs: list[McpSpec] = []
    for entry in servers:
        spec = _spec(entry)
        if any(spec.name == other.name for other in specs):
            raise RpcError(INVALID_PARAMS, f"two MCP servers are named {spec.name!r}")
        specs.append(spec)
    return specs


def _spec(entry: Any) -> McpSpec:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise RpcError(INVALID_PARAMS, "an mcpServers entry needs a string name")
    name = entry["name"]
    kind = entry.get("type", "stdio")
    if kind in ("http", "sse"):
        raise RpcError(
            INVALID_PARAMS,
            f"MCP server {name!r}: tau-acp supports stdio MCP servers only "
            "(mcpCapabilities http/sse are false)",
        )
    if kind != "stdio":
        raise RpcError(INVALID_PARAMS, f"MCP server {name!r}: unknown type {kind!r}")
    command, args, env = entry.get("command"), entry.get("args", []), entry.get("env", [])
    if not isinstance(command, str) or not command:
        raise RpcError(INVALID_PARAMS, f"MCP server {name!r}: command must be a string")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise RpcError(INVALID_PARAMS, f"MCP server {name!r}: args must be strings")
    try:
        pairs = {item["name"]: item["value"] for item in env}
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in pairs.items())
    except (TypeError, KeyError, AssertionError):
        raise RpcError(
            INVALID_PARAMS, f"MCP server {name!r}: env must be {{name, value}} strings"
        ) from None
    return McpSpec(name, command, args, pairs)


class McpClient:
    """One running MCP server."""

    def __init__(self, name: str, process: asyncio.subprocess.Process) -> None:
        self.name = name
        self._process = process
        self._child = Child(process)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._exited = False
        self._has_tools = False
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def start(cls, spec: McpSpec, cwd: Path) -> "McpClient":
        """Spawn the server, shake hands and list its tools (see ``tools``)."""
        try:
            process = await asyncio.create_subprocess_exec(
                spec.command,
                *spec.args,
                cwd=cwd,
                env={**os.environ, **spec.env},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                limit=READ_LIMIT,
                start_new_session=True,  # its own process group, signalled whole when it stops
                preexec_fn=tied(),
            )
        except OSError as error:
            raise McpStartError(
                f"MCP server {spec.name}: cannot start {spec.command}: {error.strerror}"
            ) from None
        client = cls(spec.name, process)
        try:
            async with asyncio.timeout(START_TIMEOUT):
                await client._handshake()
        except (McpError, McpExited, TimeoutError) as error:
            await client.stop()
            reason = str(error) or "timed out"
            raise McpStartError(f"MCP server {spec.name}: {reason}") from None
        except BaseException:
            await client.stop()
            raise
        return client

    async def _handshake(self) -> None:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tau-acp", "version": __version__},
            },
        )
        self._has_tools = "tools" in (result.get("capabilities") or {})
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def tools(self) -> list[McpTool]:
        """The server's tools, following ``nextCursor``."""
        listed: list[McpTool] = []
        cursor: str | None = None
        try:
            async with asyncio.timeout(START_TIMEOUT):
                while self._has_tools:
                    result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
                    for tool in result.get("tools", []):
                        schema = tool.get("inputSchema") or {"type": "object"}
                        listed.append(McpTool(tool["name"], tool.get("description", ""), schema))
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
        except (McpError, McpExited, TimeoutError, KeyError, TypeError, AttributeError) as error:
            raise McpStartError(
                f"MCP server {self.name}: tools/list failed: {error or 'timed out'}"
            ) from None
        return listed

    async def call(self, tool: str, arguments: Any) -> dict[str, Any]:
        """``tools/call``. Cancelling this coroutine sends ``notifications/cancelled``."""
        return await self.request(
            "tools/call", {"name": tool, "arguments": arguments}, cancel_reason="cancelled by τ"
        )

    async def request(
        self, method: str, params: dict[str, Any], cancel_reason: str | None = None
    ) -> Any:
        """Send a request and wait for its result."""
        if self._exited:
            raise McpExited(self.name)
        self._next_id += 1
        ident = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        self._write({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        try:
            return await future
        except asyncio.CancelledError:
            if cancel_reason is not None and not self._exited:
                note = {"requestId": ident, "reason": cancel_reason}
                self._write({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": note})
            raise
        finally:
            self._pending.pop(ident, None)

    def _write(self, message: dict[str, Any]) -> None:
        stdin = self._process.stdin
        assert stdin is not None
        with contextlib.suppress(ConnectionError, RuntimeError):
            stdin.write(json.dumps(message).encode() + b"\n")

    async def _read(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            while (line := await read_line(stdout)) is not None:
                if line is TOO_LONG:
                    self._malformed(f"a line longer than {READ_LIMIT} bytes")
                else:
                    self._dispatch(line)
        finally:
            self._exited = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpExited(self.name))

    def _malformed(self, why: str) -> None:
        """A message that cannot be read answers no request in particular, so every request
        waiting fails with it. The server is still used."""
        log(f"MCP server {self.name}: sent a malformed message ({why}); its pending requests fail")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpMalformed(f"sent a malformed message ({why})"))

    def _dispatch(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            self._malformed("not JSON")
            return
        if not isinstance(message, dict):
            self._malformed("not a JSON object")
            return
        ident, method = message.get("id"), message.get("method")
        if method is None:
            self._resolve(ident, message)
        elif ident is None:
            log(f"MCP server {self.name}: ignored notification {method}")
        elif method == "ping":  # a ping is answered whatever else we offer
            self._write({"jsonrpc": "2.0", "id": ident, "result": {}})
        else:
            error = {"code": METHOD_NOT_FOUND, "message": "tau-acp offers no client capabilities"}
            self._write({"jsonrpc": "2.0", "id": ident, "error": error})

    def _resolve(self, ident: Any, message: dict[str, Any]) -> None:
        future = self._pending.get(ident) if isinstance(ident, int) else None
        if future is None or future.done():
            return
        error, result = message.get("error"), message.get("result")
        if error is not None:
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                future.set_exception(McpError(error["message"]))
            else:
                future.set_exception(McpMalformed(f"returned a malformed error: {error!r:.80}"))
        elif isinstance(result, dict):
            future.set_result(result)
        else:
            future.set_exception(McpMalformed(f"returned a malformed result: {result!r:.80}"))

    async def stop(self) -> None:
        """Close its stdin, wait, SIGTERM, wait, SIGKILL. The signals go to its whole process
        group while it runs; what was in the group when the stop began also gets SIGTERM after
        it exits. A server that exited before the stop began is not signalled at all, nor is
        anything it left: its pid and group id may name someone else's processes by now."""
        process = self._process
        group = self._child.group()
        try:
            if process.returncode is None:
                assert process.stdin is not None
                with contextlib.suppress(ConnectionError, RuntimeError):
                    process.stdin.close()
                if not await self._exits():
                    self._child.send_group(signal.SIGTERM)
                    if not await self._exits():
                        self._child.send_group(signal.SIGKILL)
                        await process.wait()
            signal_all(group, signal.SIGTERM)  # what is left of the group
        finally:
            close_all(group)
            self._child.close()
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)

    async def _exits(self) -> bool:
        try:
            await asyncio.wait_for(self._process.wait(), STOP_GRACE)
        except TimeoutError:
            return False
        return True


def outcome(result: dict[str, Any]) -> dict[str, str]:
    """A ``tools/call`` result as the relay's reply: ``{"ok": text}`` or ``{"err": text}``.
    ``McpMalformed`` if the result is not an MCP tool result."""
    content = result.get("content") or []
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        raise McpMalformed(f"returned a malformed result: content is {content!r:.80}")
    lines: list[str] = []
    texts = 0
    for block in content:
        if block.get("type") == "text":
            texts += 1
            lines.append(str(block.get("text", "")))
        else:
            lines.append(f"[{block.get('type')} content omitted: τ tool results are text]")
    if not texts and result.get("structuredContent") is not None:
        lines.insert(0, json.dumps(result["structuredContent"]))
    return {("err" if result.get("isError") else "ok"): "\n".join(lines)}
