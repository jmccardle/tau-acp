"""JSON-RPC 2.0 over newline-delimited streams: framing, dispatch, outbound requests.

Each incoming request or notification is handled in its own task, so ``session/cancel`` and the
client's answers can arrive while a ``session/prompt`` is still running. Writes are serialized.
"""

import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002

Params = dict[str, Any]


class EditorGone(Exception):
    """The peer's end of the stream is closed: nothing more can be sent to it. (It is not a
    ``ConnectionError``: that means a ``tau serve`` went away.)"""


class RpcError(Exception):
    """A JSON-RPC error: raised by a handler to answer with it, or by ``request`` when the
    peer answered a request with one."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def body(self) -> dict[str, Any]:
        """The wire form of the ``error`` member."""
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            body["data"] = self.data
        return body


@dataclass
class Reply:
    """A request's result, and work to do once the response has been written."""

    result: Any
    after: Callable[[], Awaitable[None]] | None = None


RequestHandler = Callable[[str, Params], Awaitable[Any]]
NotificationHandler = Callable[[str, Params], Awaitable[None]]


TOO_LONG = b"line too long"


async def read_line(reader: asyncio.StreamReader) -> bytes | None:
    """The next line of the stream, ``TOO_LONG`` for one over the reader's limit (it is discarded
    through its end, so the next call starts at the next line), None at the end of the stream (or
    when the transport is lost under it)."""
    too_long = False
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as end:  # the end of the stream
            return None if not end.partial or too_long else end.partial
        except ConnectionError as error:
            log(f"the stream broke: {type(error).__name__}: {error}")
            return None
        except asyncio.LimitOverrunError as over:
            await reader.readexactly(over.consumed)  # discard what is over the limit
            too_long = True
            continue
        return TOO_LONG if too_long else line


def log(text: str) -> None:
    """Log to stderr: stdout carries only protocol messages."""
    print(f"tau-acp: {text}", file=sys.stderr, flush=True)


class Connection:
    """One peer on a reader/writer pair."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_request: RequestHandler,
        on_notification: NotificationHandler,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_request = on_request
        self._on_notification = on_notification
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._next_id = 0
        self._gone = False

    async def run(self) -> None:
        """Read messages until the peer closes the stream (or it can no longer be written to),
        then end every task still running.

        A line longer than the reader's limit is discarded through its end, answered with
        ``-32700`` (id null), and reading goes on with the next line."""
        try:
            while (line := await read_line(self._reader)) is not None:
                if line is TOO_LONG:
                    await self._error(None, RpcError(PARSE_ERROR, "Parse error: line too long"))
                else:
                    await self._line(line)
        finally:
            await self._shutdown()

    async def request(self, method: str, params: Params) -> Any:
        """Send a request and wait for its response; ``RpcError`` if the peer answered with one."""
        self._next_id += 1
        ident = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(ident, None)

    async def notify(self, method: str, params: Params) -> None:
        """Send a notification."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            try:
                self._writer.write(data)
                await self._writer.drain()
            except (ConnectionError, RuntimeError) as error:  # a closed pipe or transport
                if not self._gone:
                    self._gone = True
                    log(f"the editor is gone ({type(error).__name__}: {error}): shutting down")
                self._reader.feed_eof()  # stop reading: nobody is there to answer
                raise EditorGone(str(error)) from None

    async def _line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):  # not JSON, or nested too deep to read
            await self._error(None, RpcError(PARSE_ERROR, "Parse error"))
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            await self._error(None, RpcError(INVALID_REQUEST, "Invalid request"))
        elif "method" in message:
            self._dispatch(message)
        elif "id" in message:
            self._response(message)
        else:
            await self._error(None, RpcError(INVALID_REQUEST, "Invalid request"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        method, params = message["method"], message.get("params", {})
        if "id" in message:
            coroutine = self._answer(message["id"], method, params)
        else:
            coroutine = self._notification(method, params)
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _response(self, message: dict[str, Any]) -> None:
        future = self._pending.get(message["id"]) if isinstance(message["id"], int) else None
        if future is None or future.done():
            log(f"response to no pending request: id {message['id']!r}")
        elif "error" in message:
            future.set_exception(_error_of(message["error"]))
        else:
            future.set_result(message.get("result"))

    async def _answer(self, ident: Any, method: Any, params: Any) -> None:
        try:
            if not isinstance(method, str) or not isinstance(params, dict):
                raise RpcError(INVALID_REQUEST, "Invalid request")
            outcome = await self._on_request(method, params)
        except EditorGone:
            return  # run() has noticed, and is shutting down
        except RpcError as error:
            await self._try_error(ident, error)
            return
        except Exception as error:  # the peer must get an answer
            log(f"{method}: {error!r}")
            await self._try_error(
                ident, RpcError(INTERNAL_ERROR, f"{type(error).__name__}: {error}")
            )
            return
        reply = outcome if isinstance(outcome, Reply) else Reply(outcome)
        try:
            await self._send({"jsonrpc": "2.0", "id": ident, "result": reply.result})
            if reply.after is not None:
                await self._after(method, reply.after)
        except EditorGone:
            return

    @staticmethod
    async def _after(method: str, after: Callable[[], Awaitable[None]]) -> None:
        try:
            await after()
        except EditorGone:
            raise
        except Exception as error:  # the response is already out
            log(f"{method}: after the response: {error!r}")

    async def _try_error(self, ident: Any, error: RpcError) -> None:
        with contextlib.suppress(EditorGone):  # no one to tell
            await self._error(ident, error)

    async def _notification(self, method: Any, params: Any) -> None:
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        try:
            await self._on_notification(method, params)
        except EditorGone:
            pass  # run() has noticed, and is shutting down
        except Exception as error:  # a notification has no one to tell
            log(f"{method}: {error!r}")

    async def _error(self, ident: Any, error: RpcError) -> None:
        await self._send({"jsonrpc": "2.0", "id": ident, "error": error.body()})

    async def _shutdown(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("the peer closed the connection"))
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            self._writer.close()


def _error_of(error: Any) -> RpcError:
    """The ``RpcError`` for the ``error`` member of a response. A member that is not
    ``{code: int, message: str}`` is not trusted: the request fails with ``-32603`` saying so."""
    if (
        isinstance(error, dict)
        and isinstance(error.get("code"), int)
        and isinstance(error.get("message"), str)
    ):
        return RpcError(error["code"], error["message"], error.get("data"))
    log(f"malformed error in a response: {error!r}")
    return RpcError(INTERNAL_ERROR, f"the peer answered with a malformed error: {error!r}")
