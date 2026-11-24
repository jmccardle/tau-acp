"""One prompt turn: send the text (or run the command), hear τ's events until the cursor rests,
translate them to updates, answer τ's questions, and say how the turn ended.

This is τ's own ``tau -p`` loop: the turn is the only reader of the client's events.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from tau import (
    Appended,
    Body,
    Client,
    CursorView,
    Event,
    Live,
    Notice,
    OpenQuestion,
    Parsed,
    Stop,
    TextDelta,
    ThinkingDelta,
    ToolProgress,
    UnknownCommand,
)

from tau_acp import translate
from tau_acp.jsonrpc import INTERNAL_ERROR, INVALID_PARAMS, Params, RpcError, log

Emit = Callable[[translate.Update], Awaitable[None]]
Ask = Callable[[Params], Awaitable[Any]]
PERMISSIONS = "permissions"
PERMISSION_VALUES = ["yes", "always", "no"]


@dataclass(frozen=True)
class Editor:
    """How a turn talks to the editor: the session's cwd, and sending an update and a
    ``session/request_permission`` (whose result it returns) for the session."""

    cwd: str
    emit: Emit
    ask: Ask


class Turn:
    """The state of one ``session/prompt``."""

    def __init__(self, client: Client, cursor: int, view: CursorView, editor: Editor) -> None:
        self.view = view
        self.cancelled = False
        self._client = client
        self._cursor = cursor
        self._cwd = editor.cwd
        self._emit = editor.emit
        self._ask = editor.ask
        self._stepped = False
        self._last_stop: Stop | None = None
        self._text = ""
        self._thought = ""
        self._progress: dict[tuple[int, str], str] = {}
        self._handled: set[int] = set()
        self._asked: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self, text: str) -> str:
        """Run the prompt text; the ACP stop reason, or ``RpcError``."""
        try:
            if text.startswith("/") and (parsed := await self._parse(text)) is not None:
                return await self._command(parsed)
            return await self._message(text)
        except translate.TurnError as error:
            raise RpcError(INTERNAL_ERROR, str(error)) from None

    async def cancel(self) -> None:
        """Abort the cursor; the turn then rests and answers ``cancelled``."""
        self.cancelled = True
        await self._client.abort(self._cursor)

    async def finish(self) -> None:
        """End the tasks still answering questions."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # What the turn does

    async def _parse(self, line: str) -> Parsed | None:
        try:
            parsed = await self._client.parse_line(self._cursor, line)
        except UnknownCommand:
            return None  # not a command of τ's: the model gets it as text
        except (ValueError, LookupError) as error:
            raise RpcError(INVALID_PARAMS, str(error)) from None
        if parsed.missing:
            name = line.split(maxsplit=1)[0]
            raise RpcError(INVALID_PARAMS, f"{name} needs: {', '.join(parsed.missing)}")
        return parsed

    async def _message(self, text: str) -> str:
        if self.cancelled:
            return "cancelled"
        await self._client.send(self._cursor, text)
        if self.cancelled:  # the cancel came while the input was going in
            await self._client.abort(self._cursor)
        view = await self._rest()
        return translate.stop_reason(view, self._last_stop, message=True, cancelled=self.cancelled)

    async def _command(self, parsed: Parsed) -> str:
        if self.view.busy:
            await self._rest()  # loading the command woke the cursor
        self._stepped = False
        call = asyncio.ensure_future(parsed.run(self._client, self._cursor))
        try:
            await self._hear_until(call)
        except BaseException:
            call.cancel()
            raise
        result = call.result()
        view = await self._rest() if self.view.busy else self.view
        if result:
            await self._emit(translate.chunk("agent_message_chunk", result))
        if not self._stepped:
            return "cancelled" if self.cancelled else "end_turn"
        return translate.stop_reason(view, self._last_stop, message=False, cancelled=self.cancelled)

    # Hearing events

    async def _rest(self) -> CursorView:
        """Hear events until the cursor's view is not busy; that view."""
        async for event in self._client:
            await self._hear(event)
            if isinstance(event, CursorView) and event.cursor == self._cursor and not event.busy:
                return event
        raise ConnectionError("the session's stream ended before the cursor rested")

    async def _hear_until(self, call: "asyncio.Future[Any]") -> None:
        """Hear events while ``call`` runs, then those it published before it returned."""
        pending: asyncio.Task[Event] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(self._next())
                if call.done():
                    await asyncio.sleep(0)
                else:
                    await asyncio.wait({call, pending}, return_when=asyncio.FIRST_COMPLETED)
                if pending.done():
                    event, pending = pending.result(), None
                    await self._hear(event)
                elif call.done():
                    return
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _next(self) -> Event:
        try:
            return await anext(self._client)
        except StopAsyncIteration:
            raise ConnectionError("the session's stream ended during the turn") from None

    async def _hear(self, event: Event) -> None:
        if isinstance(event, CursorView):
            self._view(event)
        elif isinstance(event, Live) and event.cursor == self._cursor:
            await self._live(event.delta)
        elif isinstance(event, Appended):
            for node in event.nodes:
                await self._appended(node)
        elif isinstance(event, Notice):
            await self._emit(translate.chunk("agent_message_chunk", f"\n[τ] {event.text}\n"))

    def _view(self, view: CursorView) -> None:
        if view.cursor != self._cursor:
            return
        self.view = view
        self._stepped = self._stepped or view.busy
        for question in view.questions:
            if question.id in self._handled or question.to not in (None, self._client.id):
                continue
            self._handled.add(question.id)
            self._spawn(self._question(question, view.at))

    async def _live(self, delta: Any) -> None:
        if isinstance(delta, TextDelta):
            self._text += delta.text
            await self._emit(translate.chunk("agent_message_chunk", delta.text))
        elif isinstance(delta, ThinkingDelta):
            self._thought += delta.text
            await self._emit(translate.chunk("agent_thought_chunk", delta.text))
        elif isinstance(delta, ToolProgress):
            key = (delta.node, delta.call_id)
            self._progress[key] = self._progress.get(key, "") + delta.text
            identity = translate.call_id(delta.node, delta.call_id)
            await self._emit(translate.tool_progress(identity, self._progress[key]))

    async def _appended(self, node: Any) -> None:
        body = node.body
        if isinstance(body, Body.Assistant):
            updates = translate.assistant_updates(node, self._cwd, self._text, self._thought)
            self._text = self._thought = ""
            self._last_stop = body.stop
        elif isinstance(body, Body.Results):
            updates = translate.results_updates(node)
        elif isinstance(body, Body.User) and not translate.is_own(node, self._client.id):
            updates = [translate.chunk("user_message_chunk", body.text)]
        else:
            updates = []
        for update in updates:
            await self._emit(update)

    # Questions

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _question(self, question: OpenQuestion, at: int) -> None:
        """Answer a question once; a failure declines it, because a question left open keeps
        the cursor busy."""
        try:
            if (
                question.extension == PERMISSIONS
                and question.schema.get("enum") == PERMISSION_VALUES
            ):
                await self._permission(question, at)
            else:
                text = f"[tau-acp] declined a question ACP cannot carry: {question.text}"
                await self._emit(translate.chunk("agent_message_chunk", text))
                await self._answer(question.id, None)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log(f"question {question.id}: {error!r}")
            with contextlib.suppress(Exception):
                await self._answer(question.id, None)

    async def _permission(self, question: OpenQuestion, at: int) -> None:
        node = await self._client.node(at)
        tool_call = translate.permission_tool_call(question, node, self._asked, self._cwd)
        try:
            result = await self._ask(
                {"toolCall": tool_call, "options": translate.permission_options()}
            )
        except RpcError as error:
            log(f"the client refused session/request_permission: {error.message}")
            result = None
        await self._answer(question.id, translate.permission_answer(result))

    async def _answer(self, question: int, value: object) -> None:
        with contextlib.suppress(LookupError):  # closed already: the first good answer won
            await self._client.answer(question, value)
