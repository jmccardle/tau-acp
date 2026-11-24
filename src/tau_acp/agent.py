"""The ACP agent: the method handlers and the registry of sessions."""

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from tau_acp import __version__, catalog, serve, translate
from tau_acp.jsonrpc import (
    INTERNAL_ERROR,
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
    "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
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
            "session/resume": self._resume,
            "session/list": self._list,
            "session/close": self._close,
            "session/set_config_option": self._set_config_option,
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
        # Together, so that an editor's grace period before it kills us is spent once.
        results = await asyncio.gather(
            *(self._release(session) for session in sessions), return_exceptions=True
        )
        for session, result in zip(sessions, results, strict=True):
            if isinstance(result, BaseException):
                log(f"session {session.id}: releasing it failed: {result!r}")

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
        result = await self._register(session, {"sessionId": session.id})
        return Reply(result, session.announce_commands)

    async def _load(self, params: Params) -> Reply:
        return await self._reopen(params, replay=True)

    async def _resume(self, params: Params) -> Reply:
        return await self._reopen(params, replay=False)

    async def _reopen(self, params: Params, *, replay: bool) -> Reply:
        """``session/load`` and ``session/resume``: they differ only in the replay."""
        cwd = _cwd(params)
        servers = {spec.name: spec for spec in parse_servers(params.get("mcpServers"))}
        wanted = _string(params, "sessionId")
        held = self._sessions.get(wanted)
        if held is not None and held.dead is not None:
            await self._release(self._sessions.pop(wanted))  # its serve is gone: start it again
            held = None
        if held is not None and held.servers != servers:
            raise RpcError(
                INVALID_PARAMS,
                f"session {wanted} is already open with different MCP servers; close it first",
            )
        if held is not None:
            await self._check_cwd(held, wanted, cwd)
            if replay:
                with held.alive():
                    await held.replay()
            return Reply(await _opened(held, {}), held.announce_commands)
        await _exact(wanted)
        session = await self._start(cwd, wanted, list(servers.values()))
        try:
            await self._check_cwd(session, wanted, cwd)
            if replay:
                with session.alive():
                    await session.replay()
        except BaseException:
            await self._release(session)
            raise
        return Reply(await self._register(session, {}), session.announce_commands)

    @staticmethod
    async def _check_cwd(session: Session, wanted: str, cwd: Path) -> None:
        if Path(session.cwd) != cwd:
            raise RpcError(
                INVALID_PARAMS, f"session {wanted} was started in {session.cwd}, not in {cwd}"
            )

    async def _register(self, session: Session, result: dict[str, Any]) -> dict[str, Any]:
        """Add the session's ``configOptions`` to ``result`` and hold the session. The session is
        released when this raises: it is not held, so nothing else would."""
        try:
            result = await _opened(session, result)
        except BaseException:
            await self._release(session)
            raise
        self._sessions[session.id] = session
        return result

    async def _list(self, params: Params) -> dict[str, Any]:
        if params.get("cursor") is not None:
            raise RpcError(INVALID_PARAMS, "tau-acp does not page session/list")
        cwd = _cwd(params) if params.get("cwd") is not None else None
        return {"sessions": await catalog.list_sessions(cwd)}

    async def _close(self, params: Params) -> dict[str, Any]:
        session = self._sessions.pop(_string(params, "sessionId"), None)
        if session is not None:
            try:
                await session.cancel_and_wait()  # the prompt in flight answers `cancelled`
            except Exception as error:  # the serve is gone: closing is what was asked
                log(f"session {session.id}: closing: {error!r}")
            finally:
                await self._release(session)
        return {}

    async def _set_config_option(self, params: Params) -> dict[str, Any]:
        session = self._session(params)
        if params.get("type", "select") != "select":
            raise RpcError(INVALID_PARAMS, "tau-acp has select config options only")
        if params.get("configId") != "model":
            raise RpcError(INVALID_PARAMS, f"unknown config option {params.get('configId')!r}")
        value = params.get("value")
        if not isinstance(value, str):
            raise RpcError(INVALID_PARAMS, "value must be a string")
        return {"configOptions": await session.set_model(value)}

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
        if session.dead is not None:
            raise RpcError(INTERNAL_ERROR, session.dead)
        return session

    async def _start(self, cwd: Path, wanted: str | None, servers: list[McpSpec]) -> Session:
        """Start the MCP servers, then the serve for a new session (or session ``wanted``), and
        attach to it. Nothing is left running when this raises. The session is not held yet."""
        relay = await Relay.start(servers, cwd) if servers else None
        try:
            session = await self._start_serve(cwd, wanted, relay)
        except BaseException:
            if relay is not None:
                await relay.stop()
            raise
        session.relay = relay
        session.servers = {spec.name: spec for spec in servers}
        return session

    async def _start_serve(self, cwd: Path, wanted: str | None, relay: Relay | None) -> Session:
        # The extension is loaded only when there are MCP tools: with none, τ's tool set is
        # exactly what it is without tau-acp.
        args, environ = self._serve_args, None
        if relay is not None and relay.has_tools:
            args, environ = [*relay.serve_args, *args], relay.environ
        try:
            running = await serve.start(cwd, args, wanted, environ)
        except serve.ServeError as error:
            code = RESOURCE_NOT_FOUND if error.kind == "UnknownSession" else -32603
            data = {"sessionId": wanted} if code == RESOURCE_NOT_FOUND else None
            raise RpcError(code, str(error), data) from None
        try:
            _check_record(running.record, wanted)
            assert self.conn is not None
            return await Session.attach(self.conn, running)
        except BaseException:
            await _stop(running)
            raise

    async def _release(self, session: Session) -> None:
        """Disconnect, stop the serve, then the MCP servers and the relay directory. Each step is
        tried whatever the others did; a failure is logged."""
        try:
            await session.close()
            await _stop(session.serve)
        finally:
            if session.relay is not None:
                await session.relay.stop()


def _check_record(record: serve.ServeRecord, wanted: str | None) -> None:
    """``RpcError`` unless the serve ``tau serve`` started is the session asked for."""
    if wanted is not None and record.session != wanted:
        # τ takes an id prefix or a name for --session: the editor's key is the full id only.
        raise RpcError(
            RESOURCE_NOT_FOUND,
            f"no session {wanted}: tau-acp opens sessions by their full id "
            f"(τ resolved it to {record.session})",
            {"sessionId": wanted},
        )


async def _exact(wanted: str) -> None:
    """``RpcError`` unless ``wanted`` is the full id of a session. τ also takes a name or an id
    prefix for ``--session``, but the editor's key for a session is its full id only."""
    found = await catalog.resolve(wanted)
    if found != wanted:
        named = "" if found is None else f" (τ resolves it to {found})"
        raise RpcError(
            RESOURCE_NOT_FOUND,
            f"no session {wanted}: tau-acp opens sessions by their full id{named}",
            {"sessionId": wanted},
        )


async def _opened(session: Session, result: dict[str, Any]) -> dict[str, Any]:
    """``result`` with the session's ``configOptions``, when it has any."""
    if options := await session.config_options():
        result["configOptions"] = options
    return result


async def _stop(running: serve.Serve) -> None:
    try:
        await running.stop()
    except Exception as error:  # the session is closed all the same: say what happened
        log(f"session {running.record.session}: {error}")


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
