"""One ACP session: a ``tau serve`` process, the client connected to it, and its cursor."""

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any

from tau import Client, CursorGone, CursorView, Labels, RemoteClient

from tau_acp import serve, translate
from tau_acp.jsonrpc import INTERNAL_ERROR, INVALID_PARAMS, Connection, Params, RpcError, log
from tau_acp.mcp import McpSpec
from tau_acp.relay import Relay
from tau_acp.serve import ServeRecord
from tau_acp.turn import Editor, Turn

CLIENT_ID = "tau-acp"
EXIT_WAIT_SECONDS = 2.0  # how long a turn aborted by τ waits to learn whether τ is exiting


class Session:
    """What tau-acp holds for an ACP session id (which is τ's session id)."""

    def __init__(
        self,
        conn: Connection,
        record: ServeRecord,
        client: Client,
        cursor: int,
        view: CursorView,
    ) -> None:
        self.record = record
        self.id = record.session
        self.cwd = record.cwd
        self._conn = conn
        self._client = client
        self._cursor = cursor
        self._view = view
        self._turn: Turn | None = None
        self.relay: Relay | None = None  # set by the agent, which stops it
        self.servers: dict[str, McpSpec] = {}  # the MCP servers it was opened with
        self.dead: str | None = None  # why the serve is gone, once it is
        self._idle = asyncio.Event()  # set while no prompt or command is running
        self._idle.set()

    @classmethod
    async def attach(cls, conn: Connection, record: ServeRecord) -> "Session":
        """Connect to the serve, read its snapshot, take the lowest open cursor and watch it."""
        client = await RemoteClient.connect(record.socket, CLIENT_ID)
        try:
            views = await _snapshot(client)
            cursor = min(views)
            await client.watch([cursor])
        except BaseException:
            await client.close()
            raise
        return cls(conn, record, client, cursor, views[cursor])

    async def emit(self, update: translate.Update) -> None:
        """Send a ``session/update``."""
        await self._conn.notify("session/update", {"sessionId": self.id, "update": update})

    async def ask(self, params: Params) -> Any:
        """Send a ``session/request_permission`` and return its result."""
        return await self._conn.request(
            "session/request_permission", {"sessionId": self.id, **params}
        )

    async def announce_commands(self) -> None:
        """Send the ``available_commands_update``."""
        await self.emit(translate.available_commands(await self._client.commands()))

    async def replay(self) -> None:
        """Send the conversation on the cursor's line, in order."""
        nodes = await self._client.walk(self._view.at)
        for update in translate.replay_updates(nodes, self.cwd):
            await self.emit(update)

    @contextlib.contextmanager
    def alive(self) -> Iterator[None]:
        """Guard work on the serve: ``-32603`` if it is gone, and once it is found gone, always."""
        if self.dead is not None:
            raise RpcError(INTERNAL_ERROR, self.dead)
        try:
            yield
        except ConnectionError as error:
            self.dead = f"tau serve for session {self.id} exited: {error}"
            raise RpcError(INTERNAL_ERROR, self.dead) from None

    def _editor(self, *, announce_model: bool) -> Editor:
        async def model_changed(model: str | None) -> None:
            if announce_model and model is not None:
                options = await self.config_options(model)
                await self.emit({"sessionUpdate": "config_option_update", "configOptions": options})

        return Editor(self.cwd, self.emit, self.ask, model_changed)

    async def config_options(self, model: str | None = None) -> list[dict[str, Any]]:
        """The session's config options: the ``model`` select, or none when τ names no model.
        ``model`` is the current one, when it is known to have changed."""
        current = model or self._view.model
        if current is None:
            return []
        with self.alive():
            names = await self._client.complete(self._cursor, "model", "")
        if current not in names:
            names = [current, *names]
        return [
            {
                "id": "model",
                "name": "Model",
                "category": "model",
                "type": "select",
                "currentValue": current,
                "options": [{"value": name, "name": name} for name in names],
            }
        ]

    async def set_model(self, value: str) -> list[dict[str, Any]]:
        """Run τ's ``model`` command for ``value``; the new config options."""
        options = await self.config_options()
        if not options:
            raise RpcError(INVALID_PARAMS, "this session has no model config option")
        if value not in [o["value"] for o in options[0]["options"]]:
            raise RpcError(INVALID_PARAMS, f"unknown model {value!r}")
        if value != self._view.model:
            turn = self._begin(announce_model=False)
            try:
                with self.alive():
                    await turn.set_model(value)
            finally:
                await self._end(turn)
        return await self.config_options()

    def _begin(self, *, announce_model: bool) -> Turn:
        if self._turn is not None:
            raise RpcError(INVALID_PARAMS, "a prompt is already running")
        self._idle.clear()
        self._turn = Turn(
            self._client, self._cursor, self._view, self._editor(announce_model=announce_model)
        )
        return self._turn

    async def _end(self, turn: Turn) -> None:
        self._view = turn.view
        self._turn = None
        await turn.finish()
        self._idle.set()

    async def prompt(self, text: str) -> str:
        """Run one turn; the ACP stop reason."""
        with self.alive():
            turn = self._begin(announce_model=True)
            try:
                reason = await turn.run(text)
                if reason == "cancelled" and not turn.cancelled:
                    await self._aborted_from_outside()
                return reason
            finally:
                await self._end(turn)

    async def _aborted_from_outside(self) -> None:
        """The cursor was aborted, but not by us. Nothing else is attached, so τ did it: a serve
        told to stop aborts its cursors first. Give it a moment to go; if it does, the turn ended
        because the serve exited, which is an error and not a cancel."""
        deadline = asyncio.get_running_loop().time() + EXIT_WAIT_SECONDS
        while serve.alive(self.record.pid):
            if asyncio.get_running_loop().time() > deadline:
                raise RpcError(
                    INTERNAL_ERROR, f"tau serve aborted the turn of session {self.id} on its own"
                )
            await asyncio.sleep(0.05)
        raise ConnectionError("the process is gone")

    async def cancel(self) -> None:
        """Cancel the turn in flight; nothing when there is none."""
        if self._turn is not None:
            await self._turn.cancel()

    async def cancel_and_wait(self) -> None:
        """Cancel the turn in flight and wait until it has answered."""
        await self.cancel()
        await self._idle.wait()

    async def close(self) -> None:
        """Disconnect from the serve. (Stopping it is the caller's.)"""
        try:
            await self._client.close()
        except Exception as error:  # we are leaving it anyway
            log(f"session {self.id}: disconnecting: {error!r}")


async def _snapshot(client: Client) -> dict[int, CursorView]:
    """The open cursors' views, read from the start of the stream through ``Labels``."""
    views: dict[int, CursorView] = {}
    async for event in client:
        if isinstance(event, CursorView):
            views[event.cursor] = event
        elif isinstance(event, CursorGone):
            views.pop(event.cursor, None)
        elif isinstance(event, Labels):
            if not views:
                raise RuntimeError("the session has no open cursor")
            return views
    raise ConnectionError("the session's stream ended before its snapshot did")
