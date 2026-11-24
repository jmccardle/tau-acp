"""One ACP session: a ``tau serve`` process, the client connected to it, and its cursor."""

import contextlib
from typing import Any

from tau import Client, CursorGone, CursorView, Labels, RemoteClient

from tau_acp import translate
from tau_acp.jsonrpc import INVALID_PARAMS, Connection, Params, RpcError
from tau_acp.relay import Relay
from tau_acp.serve import ServeRecord
from tau_acp.turn import Editor, Turn

CLIENT_ID = "tau-acp"


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

    async def prompt(self, text: str) -> str:
        """Run one turn; the ACP stop reason."""
        if self._turn is not None:
            raise RpcError(INVALID_PARAMS, "a prompt is already running")
        turn = self._turn = Turn(
            self._client, self._cursor, self._view, Editor(self.cwd, self.emit, self.ask)
        )
        try:
            return await turn.run(text)
        except ConnectionError as error:
            raise RpcError(-32603, f"tau serve for session {self.id} exited: {error}") from None
        finally:
            self._view = turn.view
            self._turn = None
            await turn.finish()

    async def cancel(self) -> None:
        """Cancel the turn in flight; nothing when there is none."""
        if self._turn is not None:
            await self._turn.cancel()

    async def close(self) -> None:
        """Disconnect from the serve. (Stopping it is the caller's.)"""
        with contextlib.suppress(Exception):
            await self._client.close()


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
