"""What the tests share: an isolated τ home, an editor that plays the ACP client with the official
SDK, and a runner for async tests. Everything is a real process: ``python -m tau_acp`` spawning
real ``tau serve`` processes on τ's built-in ``mock`` provider."""

import asyncio
import contextlib
import functools
import json
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from acp import Client
from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    EnvVariable,
    McpServerStdio,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallUpdate,
)
from acp.stdio import spawn_agent_process

TIMEOUT = 90.0
SCRIPTED = str(Path(__file__).with_name("scripted.py"))
MCP_SERVER = str(Path(__file__).with_name("mcp_server.py"))


@dataclass
class Env:
    """A private τ home and a working directory, short enough for socket paths."""

    home: Path
    work: Path

    def configure(self, extensions: dict[str, Any] | None = None, **options: Any) -> None:
        """Write τ's config: the mock models ``m`` (the default) and ``m2``, with these provider
        options, and ``extensions`` (the extensions' options) if given."""
        model = {"model": "m", "provider": "mock", "options": {"delay_ms": 0, **options}}
        config: dict[str, Any] = {
            "default_model": "m",
            "models": {"m": model, "m2": {**model, "model": "m2"}},
        }
        if extensions is not None:
            config["extensions"] = extensions
        (self.home / "config.json").write_text(json.dumps(config))

    @property
    def environ(self) -> dict[str, str]:
        return {
            "TAU_HOME": str(self.home),
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.runtime),
        }

    @property
    def runtime(self) -> Path:
        """Where tau-acp makes its relay directories: empty again once a session is closed."""
        return self.home / "rt"

    def serve_records(self) -> list[dict[str, Any]]:
        """The records of the serves running in this home (a serve that is exiting may remove its
        record while they are read: it is left out)."""
        found = []
        for path in sorted((self.home / "serve").glob("*.json")):
            with contextlib.suppress(FileNotFoundError):
                found.append(json.loads(path.read_text()))
        return found

    def kill_serves(self) -> None:
        """Kill every serve of this home by its recorded pid, and nothing else."""
        for record in self.serve_records():
            with contextlib.suppress(ProcessLookupError):
                os.kill(record["pid"], signal.SIGKILL)


@pytest.fixture
def env() -> Iterator[Env]:
    home = Path(tempfile.mkdtemp(prefix="ta"))
    work = home / "w"
    work.mkdir()
    env = Env(home, work)
    env.runtime.mkdir()
    env.configure()
    try:
        yield env
    finally:
        env.kill_serves()
        shutil.rmtree(home, ignore_errors=True)


def scripted(test: Callable[..., Any]) -> Callable[..., None]:
    """Run an ``async def`` test on a fresh event loop, with a timeout."""

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(asyncio.wait_for(test(*args, **kwargs), TIMEOUT))

    return run


class Editor(Client):
    """An ACP client that records what the agent sends, and answers permission requests with
    ``choice``: the kind of option to select, or None to answer ``cancelled``."""

    def __init__(self, choice: str | None = "allow_once") -> None:
        self.updates: list[Any] = []
        self.permissions: list[tuple[ToolCallUpdate, list[PermissionOption]]] = []
        self.choice = choice

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update)

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permissions.append((tool_call, options))
        if self.choice is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        option = next(o for o in options if o.kind == self.choice)
        outcome = AllowedOutcome(outcome="selected", option_id=option.option_id)
        return RequestPermissionResponse(outcome=outcome)

    def of(self, kind: str) -> list[Any]:
        """The updates of one ``sessionUpdate`` kind, in order."""
        return [u for u in self.updates if u.session_update == kind]

    def text(self, kind: str = "agent_message_chunk") -> str:
        """The text of the chunks of one kind, joined."""
        return "".join(u.content.text for u in self.of(kind))

    def forget(self) -> None:
        self.updates.clear()


@contextlib.asynccontextmanager
async def agent(
    env: Env, *serve_args: str, choice: str | None = "allow_once"
) -> AsyncIterator[tuple[Any, Editor, Any]]:
    """``python -m tau_acp [-- serve args]`` as an agent: the SDK's connection, the editor, and
    the process. The first request must be ``initialize``."""
    args = ["-m", "tau_acp", *(["--", *serve_args] if serve_args else [])]
    editor = Editor(choice)
    async with spawn_agent_process(
        editor, sys.executable, *args, env=env.environ, cwd=env.work
    ) as (conn, process):
        await conn.initialize(protocol_version=1)
        yield conn, editor, process


def text(value: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=value)


async def until(condition: Callable[[], Any], what: str, seconds: float = 30.0) -> None:
    """Wait for ``condition()`` to hold."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


def gone(pid: int) -> bool:
    """Whether no process has this pid."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class Raw:
    """``python -m tau_acp`` driven with raw JSON-RPC lines, for what the SDK hides: the exact
    bytes, the order of messages, and process exit."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.last_id = 0

    @classmethod
    async def start(cls, env: Env, *serve_args: str) -> "Raw":
        args = ["-m", "tau_acp", *(["--", *serve_args] if serve_args else [])]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *args,
            cwd=env.work,
            env={**os.environ, **env.environ},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=1 << 24,
        )
        return cls(process)

    async def write(self, line: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line + b"\n")
        await self.process.stdin.drain()

    async def read(self) -> dict[str, Any]:
        """The next message; every line of stdout must be one JSON object."""
        assert self.process.stdout is not None
        line = await asyncio.wait_for(self.process.stdout.readline(), 60)
        assert line.endswith(b"\n"), f"stdout ended mid-line: {line!r}"
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
        return message

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}).encode()
        )

    async def request(
        self, method: str, params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Send a request: the messages that came before its response, and the response."""
        self.last_id += 1
        message = {"jsonrpc": "2.0", "id": self.last_id, "method": method, "params": params}
        await self.write(json.dumps(message).encode())
        before = []
        while (got := await self.read()).get("id") != self.last_id or "method" in got:
            before.append(got)
        return before, got

    async def eof(self) -> int:
        """Close stdin; the exit status."""
        assert self.process.stdin is not None
        self.process.stdin.close()
        return await asyncio.wait_for(self.process.wait(), 60)

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.kill()
            await self.process.wait()


def mcp_server(env: Env, name: str = "test", **variables: str) -> McpServerStdio:
    """The stdio entry for ``tests/mcp_server.py``; it logs to ``env.home/<name>.log``."""
    variables = {"MCP_LOG": str(env.home / f"{name}.log"), **variables}
    return McpServerStdio(
        name=name,
        command=sys.executable,
        args=[MCP_SERVER],
        env=[EnvVariable(name=k, value=v) for k, v in variables.items()],
    )


def mcp_log(env: Env, name: str = "test") -> list[dict[str, Any]]:
    """What the test MCP server logged: its start, and every message it received."""
    path = env.home / f"{name}.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def mcp_pid(env: Env, name: str = "test") -> int:
    """The pid the test MCP server logged at its start."""
    return next(e["started"] for e in mcp_log(env, name) if "started" in e)
