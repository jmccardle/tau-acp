"""A session's one reader of τ's events, and the state of the prompt running on it.

τ's events are read by exactly one task at a time (a client has one consumer). Between prompts a
background task reads them, so that nothing waits unread: a model changed from τ's side is
announced at once, a stream that ended marks the session dead at once, and the next prompt does
not start on stale events. A prompt ``pause``\\ s that task, hears what is held, and then reads the
events itself, as τ's own ``tau -p`` does, until the cursor rests.
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
    ConfigError,
    CursorView,
    Ending,
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
ModelChanged = Callable[[str | None], Awaitable[None]]
PERMISSIONS = "permissions"
PERMISSION_VALUES = ["yes", "always", "no"]
PROGRESS_TAIL = 1 << 16  # characters of a running tool's output sent in a progress update


@dataclass(frozen=True)
class Editor:
    """How the follower talks to the editor: the session's cwd, and sending an update and a
    ``session/request_permission`` (whose result it returns) for the session."""

    cwd: str
    emit: Emit
    ask: Ask
    model_changed: ModelChanged


class Follower:
    """Reads one cursor's events and translates them for the editor."""

    def __init__(self, client: Client, cursor: int, view: CursorView, editor: Editor) -> None:
        self.view = view
        self.tip = view.at  # the last node of the cursor's line: only its children are ours
        self.cancelled = False
        self.lost: str | None = None  # why the stream ended, once it has
        self._client = client
        self._cursor = cursor
        self._cwd = editor.cwd
        self._emit = editor.emit
        self._ask = editor.ask
        self._model_changed = editor.model_changed
        self._stepped = False
        self._last_stop: Stop | None = None
        self._replies = 0  # the Assistant nodes of the prompt
        self._text = ""
        self._thought = ""
        self._progress: dict[tuple[int, str], str] = {}
        self._handled: set[int] = set()
        self._asked: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._background: asyncio.Task[None] | None = None
        self._waiting = False  # the background task is blocked on the stream, holding no event
        self._paused = True

    # Between prompts

    def start(self) -> None:
        """Read the events in the background, until ``pause`` or the end of the stream."""
        self._paused = False
        if self.lost is None:
            self._background = asyncio.get_running_loop().create_task(self._follow())

    async def pause(self) -> None:
        """Stop the background reading, and hear the events held at this moment. Nothing that was
        read is lost: the task is only interrupted where it holds no event."""
        self._paused = True
        task, self._background = self._background, None
        if task is not None:
            if self._waiting:
                task.cancel()
            (outcome,) = await asyncio.gather(task, return_exceptions=True)
            if isinstance(outcome, Exception):
                raise outcome
        if self.lost is None:
            await self._drain()

    async def stop(self) -> None:
        """End the background reading and the answering of questions."""
        self._paused = True
        task, self._background = self._background, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.finish()

    async def _follow(self) -> None:
        try:
            while not self._paused:
                self._waiting = True
                try:
                    event = await self._next()
                finally:
                    self._waiting = False
                await self.hear(event)
        except ConnectionError as error:
            log(f"the stream of the session ended: {error}")  # `lost` is set
        except Exception as error:  # `pause` raises it, at the next prompt
            log(f"hearing the session's events failed: {error!r}")
            raise

    async def _drain(self) -> None:
        """Hear every event the client holds now, without waiting for more."""
        while True:
            try:
                async with asyncio.timeout(0):
                    event = await self._next()
            except TimeoutError:
                return
            await self.hear(event)

    async def stream_ends(self, seconds: float) -> bool:
        """Hear events until the stream ends (True) or ``seconds`` pass (False)."""
        try:
            async with asyncio.timeout(seconds):
                while True:
                    await self.hear(await self._next())
        except ConnectionError:
            return True
        except TimeoutError:
            return False

    def begin(self) -> None:
        """Start a prompt. (``cancelled`` is the caller's: a cancel can come before this.)"""
        self._stepped = False
        self._last_stop = None
        self._replies = 0

    # A prompt

    async def run(self, text: str) -> str:
        """Run the prompt text; the ACP stop reason, or ``RpcError``."""
        try:
            parsed = None
            if text.startswith("/"):
                parsed = await self._parse(text)
            if self.cancelled:  # the cancel came while the line was being read: run nothing
                return "cancelled"
            if parsed is not None:
                return await self._command(parsed)
            return await self._message(text)
        except translate.TurnError as error:
            raise RpcError(INTERNAL_ERROR, str(error)) from None

    async def cancel(self) -> None:
        """Abort the cursor; the prompt then rests and answers ``cancelled``."""
        self.cancelled = True
        await self._client.abort(self._cursor)

    async def finish(self) -> None:
        """End the tasks still answering questions."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # What the prompt does

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
        await self._client.send(self._cursor, text)
        if self.cancelled:  # the cancel came while the input was going in
            await self._client.abort(self._cursor)
        view = await self._rest()
        return translate.stop_reason(
            view,
            self._last_stop,
            message=True,
            cancelled=self.cancelled,
            limited=await self._limited(view),
        )

    async def _command(self, parsed: Parsed) -> str:
        if self.view.busy:
            await self._rest()  # loading the command woke the cursor
        self._stepped = False
        result = await self._invoke(parsed.run(self._client, self._cursor))
        view = await self._rest() if self.view.busy else self.view
        if result:
            await self._emit(translate.chunk("agent_message_chunk", result))
        if not self._stepped:
            return "cancelled" if self.cancelled else "end_turn"
        return translate.stop_reason(
            view,
            self._last_stop,
            message=False,
            cancelled=self.cancelled,
            limited=await self._limited(view),
        )

    async def _limited(self, view: CursorView) -> bool:
        """Whether τ's step limit (the ``step_limit`` extension) turned auto-step off at the end of
        the prompt. Its mark: the cursor rested done with nothing pending and auto-step off after
        a reply that called tools, and the prompt's reply count is a multiple of the limit (it
        turns auto-step off after ``limit`` steps, each one reply, then counts again). A session
        without the extension has no step limit. Anything else that turned auto-step off (a hook,
        a step that did not move) is not this."""
        if view.auto or view.items or not isinstance(view.ending, Ending.Done):
            return False
        if self._last_stop != Stop.Tools or self._replies == 0:
            return False
        try:
            form = await self._client.options(self._cursor, "step_limit")
        except ConfigError:  # not loaded: no step limit
            return False
        limit = form.schema["properties"]["limit"]["default"]
        return self._replies % limit == 0

    async def set_model(self, name: str) -> None:
        """Run τ's ``model`` command, hearing events meanwhile; ``RpcError`` if τ refuses."""
        if self.view.busy:
            await self._rest()
        try:
            await self._invoke(self._client.run(self._cursor, "model", {"model": name}))
        except (ValueError, LookupError) as error:
            raise RpcError(INVALID_PARAMS, str(error)) from None
        if self.view.busy:
            await self._rest()

    async def _invoke(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run a client call as a task while events keep being heard; its result."""
        call = asyncio.ensure_future(coroutine)
        try:
            await self._hear_until(call)
        except BaseException:
            call.cancel()
            raise
        return call.result()

    # Hearing events

    async def _rest(self) -> CursorView:
        """Hear events until the cursor's view is not busy; that view."""
        while True:
            event = await self._next()
            await self.hear(event)
            if isinstance(event, CursorView) and event.cursor == self._cursor and not event.busy:
                return event

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
                    await self.hear(event)
                elif call.done():
                    return
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _next(self) -> Event:
        """The next event. The end of the stream is ``ConnectionError``, and is remembered."""
        try:
            return await anext(self._client)
        except StopAsyncIteration:
            self.lost = "the session's stream ended"
            raise ConnectionError(self.lost) from None
        except ConnectionError as error:
            self.lost = str(error) or type(error).__name__
            raise

    async def hear(self, event: Event) -> None:
        """Translate one event: to the editor, or to a question to answer."""
        if isinstance(event, CursorView):
            before = self.view.model
            self._seen(event)
            if event.cursor == self._cursor and event.model != before:
                await self._model_changed(event.model)
        elif isinstance(event, Live) and event.cursor == self._cursor:
            await self._live(event.delta)
        elif isinstance(event, Appended):
            for node in event.nodes:
                await self._appended(node)
        elif isinstance(event, Notice):
            await self._emit(translate.chunk("agent_message_chunk", f"\n[τ] {event.text}\n"))

    def _seen(self, view: CursorView) -> None:
        if view.cursor != self._cursor:
            return
        self.view = view
        self.tip = view.at
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
            tail = (self._progress.get(key, "") + delta.text)[-PROGRESS_TAIL:]
            self._progress[key] = tail
            await self._emit(translate.tool_progress(translate.call_id(*key), tail))

    async def _appended(self, node: Any) -> None:
        if node.parent != self.tip:
            # Another cursor's node (the stream carries every cursor's): not this line's.
            log(f"ignored node {node.id}: its parent {node.parent} is not the tip {self.tip}")
            return
        self.tip = node.id
        body = node.body
        if isinstance(body, Body.Assistant):
            updates = translate.assistant_updates(node, self._cwd, self._text, self._thought)
            self._text = self._thought = ""
            self._last_stop = body.stop
            self._replies += 1
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
