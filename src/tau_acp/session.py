"""One ACP session: a ``tau serve`` process, the client connected to it, and its cursor."""

import asyncio
import contextlib
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tau import (
    Client,
    Commands,
    ConfigError,
    CursorGone,
    CursorView,
    ImageInput,
    Labels,
    ModelCatalog,
    RemoteClient,
    TrustError,
    load_config,
)

from tau_acp import translate
from tau_acp.follower import Editor, Follower
from tau_acp.jsonrpc import INTERNAL_ERROR, INVALID_PARAMS, Connection, Params, RpcError, log
from tau_acp.mcp import McpSpec
from tau_acp.relay import Relay
from tau_acp.serve import Serve

CLIENT_ID = "tau-acp"
DEATH_WAIT_SECONDS = 10.0  # for τ's stream to end after it aborted a turn: τ winds down in 3


@dataclass(frozen=True)
class EditorFeatures:
    """What the editor said it can do, in ``initialize``'s ``clientCapabilities``."""

    elicitation: bool = False  # ``elicitation/create`` in form mode
    notices: bool = False  # ACP's ``notice`` session update


class Session:
    """What tau-acp holds for an ACP session id (which is τ's session id)."""

    def __init__(
        self,
        conn: Connection,
        serve: Serve,
        client: Client,
        view: CursorView,
        features: EditorFeatures,
    ) -> None:
        self.serve = serve  # the agent stops it
        self.id = serve.record.session
        self.cwd = serve.record.cwd
        self._conn = conn
        self._client = client
        self._cursor = view.cursor
        self._catalog: ModelCatalog | None = None  # read once, for the context window
        self._follower = Follower(client, view.cursor, view, self._editor(features))
        self._announce = True  # whether a model change heard is sent to the editor
        self._running = False  # a prompt or command is running
        self.relay: Relay | None = None  # set by the agent, which stops it
        self.servers: dict[str, McpSpec] = {}  # the MCP servers it was opened with
        self._dead: str | None = None
        self._idle = asyncio.Event()  # set while no prompt or command is running
        self._idle.set()

    @classmethod
    async def attach(
        cls, conn: Connection, serve: Serve, features: EditorFeatures = EditorFeatures()
    ) -> "Session":
        """Connect to the serve, read its snapshot, take the lowest open cursor and watch it."""
        client = await RemoteClient.connect(serve.record.socket, CLIENT_ID)
        try:
            views, forms = await _snapshot(client)
            cursor = min(views)
            await client.watch([cursor])
        except BaseException:
            await client.close()
            raise
        session = cls(conn, serve, client, views[cursor], features)
        if forms is not None:
            session._follower.hear_commands(forms)
        session._follower.start()
        return session

    @property
    def dead(self) -> str | None:
        """Why the serve is gone, once it is: found when a call fails, or when its stream ends."""
        if self._dead is None and self._follower.lost is not None:
            self._dead = f"tau serve for session {self.id} exited: {self._follower.lost}"
        return self._dead

    def gone(self) -> str | None:
        """``dead``, or, when nothing has found that yet, whether the serve's process has exited."""
        if self.dead is None and (how := self.serve.exited()) is not None:
            self._dead = f"tau serve for session {self.id} {how}"
        return self.dead

    async def emit(self, update: translate.Update) -> None:
        """Send a ``session/update``."""
        await self._conn.notify("session/update", {"sessionId": self.id, "update": update})

    async def ask(self, params: Params) -> Any:
        """Send a ``session/request_permission`` and return its result."""
        return await self._conn.request(
            "session/request_permission", {"sessionId": self.id, **params}
        )

    async def announce_commands(self) -> None:
        """Send the ``available_commands_update`` from the ``Commands`` τ's stream opened with,
        and again whenever τ sends new ones."""
        await self._follower.announce_commands()

    async def replay(self) -> None:
        """Send the conversation on the cursor's line, in order, with its images' bytes."""
        nodes = await self._client.walk(self._follower.view.at)
        images = {h: await self._client.image(h) for h in sorted(translate.image_hashes(nodes))}
        for update in translate.replay_updates(nodes, self.cwd, images):
            await self.emit(update)

    @contextlib.contextmanager
    def alive(self) -> Iterator[None]:
        """Guard work on the serve: ``-32603`` if it is gone, and once it is found gone, always."""
        if (dead := self.dead) is not None:
            raise RpcError(INTERNAL_ERROR, dead)
        try:
            yield
        except ConnectionError as error:
            self._dead = f"tau serve for session {self.id} exited: {error}"
            raise RpcError(INTERNAL_ERROR, self._dead) from None

    async def elicit(self, params: Params) -> Any:
        """Send an ``elicitation/create`` and return its result."""
        return await self._conn.request("elicitation/create", {"sessionId": self.id, **params})

    def _context_window(self, model: str | None) -> int | None:
        """The ``context_window`` of τ's config for ``model``, which is not on τ's wire: read from
        the config files, as the serve reads them (not its ``--set`` overrides). None when the
        model sets none, or the config cannot be read here (said once, in the log)."""
        if model is None:
            return None
        if self._catalog is None:
            try:
                self._catalog = load_config(Path(self.cwd), os.environ, ask=None).catalog
            except (ConfigError, TrustError) as error:
                log(f"session {self.id}: no usage_update, τ's config cannot be read here: {error}")
                self._catalog = ModelCatalog({}, None, ())
        try:
            return self._catalog.resolve(model).context_window
        except ConfigError:
            return None

    def _editor(self, features: EditorFeatures) -> Editor:
        async def model_changed(model: str | None) -> None:
            if self._announce and model is not None:
                options = await self.config_options(model)
                await self.emit({"sessionUpdate": "config_option_update", "configOptions": options})

        return Editor(
            self.cwd,
            self.emit,
            self.ask,
            model_changed,
            self.elicit if features.elicitation else None,
            features.notices,
            self._context_window,
        )

    async def config_options(self, model: str | None = None) -> list[dict[str, Any]]:
        """The session's config options: the ``model`` select, or none when τ names no model.
        ``model`` is the current one, when it is known to have changed."""
        current = model or self._follower.view.model
        if current is None:
            return []
        with self.alive():
            names = [c.value for c in await self._client.complete(self._cursor, "model", "")]
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
        if value != self._follower.view.model:
            with self.alive():
                follower = await self._begin(announce_model=False)
            try:
                with self.alive():
                    await follower.set_model(value)
            finally:
                await self._end()
        return await self.config_options()

    async def _begin(self, *, announce_model: bool) -> Follower:
        """Take the reading of events over from the background, for a prompt."""
        if self._running:
            raise RpcError(INVALID_PARAMS, "a prompt is already running")
        self._running = True
        self._idle.clear()
        self._follower.cancelled = False
        try:
            await self._follower.pause()
        except BaseException:
            await self._end()
            raise
        self._announce = announce_model
        self._follower.begin()
        return self._follower

    async def _end(self) -> None:
        """The prompt is over: end its question tasks and read in the background again."""
        try:
            await self._follower.finish()
        finally:
            self._announce = True
            self._follower.start()
            self._running = False
            self._idle.set()

    async def prompt(self, text: str, images: Sequence[ImageInput] = ()) -> str:
        """Run one turn; the ACP stop reason. A turn the editor cancelled answers ``cancelled``
        whatever went wrong after the cancel, except that a serve found gone stays dead."""
        with self.alive():
            follower = await self._begin(announce_model=True)
        try:
            return await self._run(follower, text, images)
        finally:
            await self._end()

    async def _run(self, follower: Follower, text: str, images: Sequence[ImageInput]) -> str:
        try:
            reason = await follower.run(text, images)
        except ConnectionError as error:
            self._dead = f"tau serve for session {self.id} exited: {error}"
            if not follower.cancelled:
                raise RpcError(INTERNAL_ERROR, self._dead) from None
            return "cancelled"
        except Exception as error:
            if not follower.cancelled:
                raise
            log(f"session {self.id}: after a cancel: {error!r}")
            return "cancelled"
        if reason == "cancelled" and not follower.cancelled:
            await self._aborted_from_outside()
        return reason

    async def _aborted_from_outside(self) -> None:
        """The cursor was aborted, but not by us. Nothing else is attached, so τ did it: a serve
        told to stop aborts its cursors first, and then ends the stream (after up to 3 seconds of
        winding down). If the stream ends within ``DEATH_WAIT_SECONDS`` the turn ended because the
        serve exited, which is an error and not a cancel; the pid is not looked at."""
        if await self._follower.stream_ends(DEATH_WAIT_SECONDS):
            raise RpcError(INTERNAL_ERROR, self.dead or "the serve exited")
        raise RpcError(
            INTERNAL_ERROR, f"tau serve aborted the turn of session {self.id} on its own"
        )

    async def cancel(self) -> None:
        """Cancel the turn in flight; nothing when there is none."""
        if self._running:
            await self._follower.cancel()

    async def cancel_and_wait(self) -> None:
        """Cancel the turn in flight and wait until it has answered."""
        await self.cancel()
        await self._idle.wait()

    async def close(self) -> None:
        """Disconnect from the serve. (Stopping it is the caller's.)"""
        try:
            await self._follower.stop()
            await self._client.close()
        except Exception as error:  # we are leaving it anyway
            log(f"session {self.id}: disconnecting: {error!r}")


async def _snapshot(client: Client) -> tuple[dict[int, CursorView], Commands | None]:
    """The open cursors' views and the ``Commands``, read from the start of the stream through
    ``Labels``."""
    views: dict[int, CursorView] = {}
    forms: Commands | None = None
    async for event in client:
        if isinstance(event, Commands):
            forms = event
        elif isinstance(event, CursorView):
            views[event.cursor] = event
        elif isinstance(event, CursorGone):
            views.pop(event.cursor, None)
        elif isinstance(event, Labels):
            if not views:
                raise RuntimeError("the session has no open cursor")
            return views, forms
    raise ConnectionError("the session's stream ended before its snapshot did")
