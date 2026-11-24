"""Start and stop one ``tau serve`` process per ACP session.

tau-acp only uses τ's public command line: ``tau serve -d --json`` prints one JSON line (the
serve record, or an error) and exits; ``tau serve --stop --session ID --json`` stops it.
"""

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tau_acp.catalog import tau_home
from tau_acp.jsonrpc import log

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


async def _run(
    args: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *_command(*args),
        cwd=cwd,
        env={**os.environ, **env} if env else None,
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


async def start(
    cwd: Path,
    serve_args: Sequence[str],
    session: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ServeRecord:
    """Start (or find) the serve for a new session, or for the existing session ``session``.
    ``env`` is added to tau-acp's environment for it.

    Cancelling this does not leave a serve behind: the start runs to its end (it takes under a
    few seconds), and a serve it started is stopped before the cancellation goes on."""
    task = asyncio.ensure_future(_start(cwd, serve_args, session, env))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            record = await task
        except Exception as error:  # nothing started, or no way to know: say why
            log(f"a cancelled session start ended with: {error}")
        else:
            if record.started:  # not ours otherwise: another process serves it
                await stop(record)
        raise


async def _start(
    cwd: Path, serve_args: Sequence[str], session: str | None, env: Mapping[str, str] | None
) -> ServeRecord:
    args = ["-d", "--json"]
    if session is not None:
        args += ["--session", session]
    code, out, err = await _run([*args, *serve_args], cwd, env)
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


def recorded_pid(session: str) -> int | None:
    """The pid in the serve record file of ``session`` (``$TAU_HOME/serve/<id>.json``), or None
    when there is none: no serve is running for it. τ removes the file when a serve exits."""
    path = tau_home() / "serve" / f"{session}.json"
    try:
        return int(json.loads(path.read_text())["pid"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ServeError("BadRecord", f"cannot read the serve record {path}: {error}") from None


async def stop(record: ServeRecord) -> None:
    """Stop the serve ``record`` describes, with ``tau serve --stop --session ID``, which only
    stops a serve that answers on its socket with the recorded pid.

    It does nothing, and says why with ``ServeError``, when the session's record file names
    another pid: another serve holds the session now, and it is not ours to stop. Nothing is
    signalled directly, because a pid can be reused."""
    named = recorded_pid(record.session)
    if named is None:
        return  # gone already
    if named != record.pid:
        raise ServeError(
            "Replaced",
            f"session {record.session} is served by pid {named} now, not by our pid "
            f"{record.pid}: left running",
        )
    code, out, err = await _run(["--stop", "--session", record.session, "--json"])
    _json_line(out, err, code)
    deadline = asyncio.get_running_loop().time() + STOP_WAIT_SECONDS
    while recorded_pid(record.session) == record.pid and alive(record.pid):
        if asyncio.get_running_loop().time() > deadline:
            raise ServeError("StopFailed", f"tau serve pid {record.pid} did not exit")
        await asyncio.sleep(0.05)
