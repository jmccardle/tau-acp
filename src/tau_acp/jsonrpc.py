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

    async def run(self) -> None:
        """Read messages until the peer closes the stream, then end every task still running."""
        try:
            while line := await self._reader.readline():
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
            self._writer.write(data)
            await self._writer.drain()

    async def _line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except ValueError:
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
            error = message["error"]
            future.set_exception(RpcError(error["code"], error["message"], error.get("data")))
        else:
            future.set_result(message.get("result"))

    async def _answer(self, ident: Any, method: Any, params: Any) -> None:
        try:
            if not isinstance(method, str) or not isinstance(params, dict):
                raise RpcError(INVALID_REQUEST, "Invalid request")
            outcome = await self._on_request(method, params)
        except RpcError as error:
            await self._error(ident, error)
            return
        except Exception as error:  # the peer must get an answer
            log(f"{method}: {error!r}")
            await self._error(ident, RpcError(INTERNAL_ERROR, f"{type(error).__name__}: {error}"))
            return
        reply = outcome if isinstance(outcome, Reply) else Reply(outcome)
        await self._send({"jsonrpc": "2.0", "id": ident, "result": reply.result})
        if reply.after is not None:
            try:
                await reply.after()
            except Exception as error:  # the response is already out
                log(f"{method}: after the response: {error!r}")

    async def _notification(self, method: Any, params: Any) -> None:
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        try:
            await self._on_notification(method, params)
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
