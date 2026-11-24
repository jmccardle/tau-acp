"""Start and stop one ``tau serve`` process per ACP session.

tau-acp only uses τ's public command line: ``tau serve -d --json`` prints one JSON line (the
serve record, or an error) and exits; ``tau serve --stop --session ID --json`` stops it.
"""

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

STOP_WAIT_SECONDS = 5.0


class ServeError(Exception):
    """``tau serve`` refused to start or stop; ``kind`` is the error type τ named."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ServeRecord:
    """What ``tau serve -d --json`` prints for a serve."""

    socket: str
    pid: int
    session: str
    cwd: str
    started: bool


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "tau", "serve", *args]


async def _run(args: Sequence[str], cwd: Path | None = None) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *_command(*args),
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _json_line(out: str, err: str, code: int) -> dict:
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        raise ServeError("ServeFailed", f"tau serve exited {code} with no output: {err.strip()}")
    try:
        record = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise ServeError("ServeFailed", f"tau serve printed {lines[-1]!r}: {err.strip()}") from None
    if "error" in record:
        error = record["error"]
        raise ServeError(str(error.get("type", "ServeFailed")), str(error.get("message", error)))
    return record


async def start(cwd: Path, serve_args: Sequence[str], session: str | None = None) -> ServeRecord:
    """Start (or find) the serve for a new session, or for the existing session ``session``."""
    args = ["-d", "--json"]
    if session is not None:
        args += ["--session", session]
    code, out, err = await _run([*args, *serve_args], cwd)
    record = _json_line(out, err, code)
    return ServeRecord(
        socket=record["socket"],
        pid=record["pid"],
        session=record["session"],
        cwd=record["cwd"],
        started=record["started"],
    )


def alive(pid: int) -> bool:
    """Whether a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def stop(record: ServeRecord) -> None:
    """Stop the serve: ``tau serve --stop``, then SIGTERM to its recorded pid if it lives on."""
    stopped = False
    with contextlib.suppress(ServeError):
        code, out, err = await _run(["--stop", "--session", record.session, "--json"])
        stopped = _json_line(out, err, code).get("stopped", False)
    if not stopped and alive(record.pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(record.pid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + STOP_WAIT_SECONDS
    while alive(record.pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    if alive(record.pid):
        raise ServeError("StopFailed", f"tau serve pid {record.pid} did not exit")
