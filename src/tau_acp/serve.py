"""Start and stop one ``tau serve`` process per ACP session.

tau-acp only uses τ's public command line. ``tau serve --json`` runs in the foreground as
tau-acp's child: it prints one JSON line (its serve record, or an error) and serves until SIGTERM,
when it aborts what runs, winds down and exits. Being our child, it is stopped by signalling it
through a pidfd (``tau_acp.child``), never by a pid or a session id read back from a file, and
on Linux it gets SIGTERM if tau-acp dies without stopping it.
"""

import asyncio
import json
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tau_acp.child import Child, tied
from tau_acp.jsonrpc import log

STOP_WAIT_SECONDS = 5.0  # τ winds down in 3


class ServeError(Exception):
    """``tau serve`` refused to start, or did not stop; ``kind`` is the error type τ named."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ServeRecord:
    """What ``tau serve --json`` prints for a serve."""

    socket: str
    pid: int
    session: str
    cwd: str


class Serve:
    """A running ``tau serve`` that tau-acp started: its record, and the process."""

    def __init__(self, record: ServeRecord, child: Child) -> None:
        self.record = record
        self._child = child
        self._drain = asyncio.ensure_future(_drain(child.process))

    async def stop(self) -> None:
        """SIGTERM it and wait for it to exit; after ``STOP_WAIT_SECONDS``, SIGKILL it and raise
        ``ServeError``. Nothing is signalled if it has exited already."""
        process = self._child.process
        try:
            self._child.send(signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), STOP_WAIT_SECONDS)
            except TimeoutError:
                self._child.send(signal.SIGKILL)
                await process.wait()
                raise ServeError(
                    "StopFailed",
                    f"tau serve pid {self.record.pid} did not exit within "
                    f"{STOP_WAIT_SECONDS:g} s of SIGTERM: killed",
                ) from None
        finally:
            self._child.close()
            self._drain.cancel()


async def _drain(process: asyncio.subprocess.Process) -> None:
    """Read what the serve prints after its record, so a full pipe never blocks it."""
    assert process.stdout is not None
    while line := await process.stdout.readline():
        log(f"tau serve pid {process.pid}: {line.decode(errors='replace').rstrip()}")


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "tau", "serve", *args]


def _parse(line: bytes, code: int | None) -> dict:
    """The record ``tau serve --json`` printed, or ``ServeError`` with the error it printed."""
    if not line.strip():
        raise ServeError("ServeFailed", f"tau serve exited {code} before it printed its record")
    try:
        record = json.loads(line)
    except ValueError:
        raise ServeError("ServeFailed", f"tau serve printed {line[:200]!r}") from None
    if not isinstance(record, dict):
        raise ServeError("ServeFailed", f"tau serve printed {line[:200]!r}")
    if "error" in record:
        error = record["error"]
        raise ServeError(str(error.get("type", "ServeFailed")), str(error.get("message", error)))
    return record


async def start(
    cwd: Path,
    serve_args: Sequence[str],
    session: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Serve:
    """Start the serve of a new session, or of the existing session ``session``. ``env`` is added
    to tau-acp's environment for it. A session that another process holds fails with τ's error.

    Cancelling this does not leave a serve behind: the start runs to its end (it takes under a
    few seconds), and the serve it started is stopped before the cancellation goes on."""
    task = asyncio.ensure_future(_start(cwd, serve_args, session, env))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            serve = await task
        except Exception as error:  # nothing started: say why
            log(f"a cancelled session start ended with: {error}")
        else:
            await serve.stop()
        raise


async def _start(
    cwd: Path, serve_args: Sequence[str], session: str | None, env: Mapping[str, str] | None
) -> Serve:
    args = ["--json"]
    if session is not None:
        args += ["--session", session]
    process = await asyncio.create_subprocess_exec(
        *_command(*args, *serve_args),
        cwd=cwd,
        env={**os.environ, **env} if env else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,  # its stderr is ours: the editor's log
        start_new_session=True,  # a Ctrl-C meant for tau-acp is not also τ's: we stop it
        preexec_fn=tied(),
    )
    child = Child(process)
    assert process.stdout is not None
    try:
        line = await process.stdout.readline()
        record = _parse(line, None if line else await process.wait())
        found = ServeRecord(
            socket=record["socket"], pid=record["pid"], session=record["session"], cwd=record["cwd"]
        )
        if found.pid != process.pid:
            raise ServeError("ServeFailed", f"tau serve pid {process.pid} printed pid {found.pid}")
    except BaseException:
        await Serve(ServeRecord("", process.pid, "", ""), child).stop()
        raise
    return Serve(found, child)
