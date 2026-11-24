"""The ACP agent: the method handlers and the registry of sessions."""

import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from tau_acp import __version__, serve, translate
from tau_acp.jsonrpc import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    RESOURCE_NOT_FOUND,
    Connection,
    Params,
    Reply,
    RpcError,
    log,
)
from tau_acp.mcp import McpSpec, parse_servers
from tau_acp.relay import Relay
from tau_acp.session import Session

PROTOCOL_VERSION = 1
CAPABILITIES = {
    "loadSession": True,
    "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
    "mcpCapabilities": {"http": False, "sse": False},
}


class Agent:
    """Answers the editor's requests. ``serve_args`` go to every ``tau serve`` it starts."""

    def __init__(self, serve_args: Sequence[str]) -> None:
        self.conn: Connection | None = None
        self._serve_args = list(serve_args)
        self._sessions: dict[str, Session] = {}
        self._initialized = False
        self._methods: dict[str, Callable[[Params], Awaitable[Any]]] = {
            "initialize": self._initialize,
            "session/new": self._new,
            "session/load": self._load,
            "session/prompt": self._prompt,
        }

    async def request(self, method: str, params: Params) -> Any:
        """Handle a request: the result, a ``Reply``, or ``RpcError``."""
        handler = self._methods.get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        if method != "initialize" and not self._initialized:
            raise RpcError(INVALID_REQUEST, "initialize first")
        return await handler(params)

    async def notification(self, method: str, params: Params) -> None:
        """Handle a notification; unknown ones are ignored."""
        if method == "session/cancel":
            session = self._sessions.get(params.get("sessionId", ""))
            if session is not None:
                await session.cancel()

    async def close(self) -> None:
        """Close every session: disconnect, and stop its serve."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await self._release(session)

    # Methods

    async def _initialize(self, _params: Params) -> dict[str, Any]:
        self._initialized = True
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": CAPABILITIES,
            "authMethods": [],
            "agentInfo": {"name": "tau-acp", "version": __version__},
        }

    async def _new(self, params: Params) -> Reply:
        cwd = _cwd(params)
        session = await self._start(cwd, None, parse_servers(params.get("mcpServers")))
        return Reply({"sessionId": session.id}, session.announce_commands)

    async def _load(self, params: Params) -> Reply:
        cwd = _cwd(params)
        servers = parse_servers(params.get("mcpServers"))
        wanted = _string(params, "sessionId")
        held = self._sessions.get(wanted)  # already held: replay it, do not restart its serve
        session = held or await self._start(cwd, wanted, servers)
        if Path(session.cwd) != cwd:
            if held is None:
                await self._release(self._sessions.pop(session.id))
            raise RpcError(
                INVALID_PARAMS, f"session {wanted} was started in {session.cwd}, not in {cwd}"
            )
        await session.replay()
        return Reply({}, session.announce_commands)

    async def _prompt(self, params: Params) -> dict[str, str]:
        session = self._session(params)
        blocks = params.get("prompt")
        if not isinstance(blocks, list):
            raise RpcError(INVALID_PARAMS, "prompt must be an array of content blocks")
        try:
            text = translate.prompt_text(blocks)
        except (ValueError, KeyError, TypeError) as error:
            raise RpcError(INVALID_PARAMS, str(error)) from None
        return {"stopReason": await session.prompt(text)}

    # Sessions

    def _session(self, params: Params) -> Session:
        ident = _string(params, "sessionId")
        session = self._sessions.get(ident)
        if session is None:
            raise RpcError(RESOURCE_NOT_FOUND, f"no session {ident}", {"sessionId": ident})
        return session

    async def _start(self, cwd: Path, wanted: str | None, servers: list[McpSpec]) -> Session:
        """Start the MCP servers, then the serve for a new session (or session ``wanted``), and
        attach to it. Nothing is left running when this raises."""
        relay = await Relay.start(servers, cwd) if servers else None
        try:
            session = await self._start_serve(cwd, wanted, relay)
        except BaseException:
            if relay is not None:
                await relay.stop()
            raise
        session.relay = relay
        self._sessions[session.id] = session
        return session

    async def _start_serve(self, cwd: Path, wanted: str | None, relay: Relay | None) -> Session:
        # The extension is loaded only when there are MCP tools: with none, τ's tool set is
        # exactly what it is without tau-acp.
        args = [*relay.serve_args, *self._serve_args] if relay else self._serve_args
        try:
            record = await serve.start(cwd, args, wanted, relay.environ if relay else None)
        except serve.ServeError as error:
            code = RESOURCE_NOT_FOUND if error.kind == "UnknownSession" else -32603
            data = {"sessionId": wanted} if code == RESOURCE_NOT_FOUND else None
            raise RpcError(code, str(error), data) from None
        if not record.started:
            raise RpcError(
                -32603,
                f"session {record.session} is already served by pid {record.pid}: "
                f"stop it with `tau serve --stop --session {record.session}`",
            )
        assert self.conn is not None
        try:
            return await Session.attach(self.conn, record)
        except BaseException:
            await _stop(record)
            raise

    async def _release(self, session: Session) -> None:
        await session.close()
        await _stop(session.record)
        if session.relay is not None:
            await session.relay.stop()


async def _stop(record: serve.ServeRecord) -> None:
    try:
        await serve.stop(record)
    except serve.ServeError as error:
        log(f"session {record.session}: {error}")


def _string(params: Params, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, f"{key} must be a string")
    return value


def _cwd(params: Params) -> Path:
    cwd = Path(_string(params, "cwd"))
    if not cwd.is_absolute() or not cwd.is_dir():
        raise RpcError(INVALID_PARAMS, f"cwd must be an existing absolute directory: {cwd}")
    return Path(os.path.normpath(cwd))
