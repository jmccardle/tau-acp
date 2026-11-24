"""The ``tau-acp`` command line: ``tau-acp [--version] [-- TAU SERVE ARGS...]``."""

import argparse
import asyncio
import os
import signal
import stat
import sys
from collections.abc import Sequence
from typing import BinaryIO

from tau_acp import __version__
from tau_acp.agent import Agent
from tau_acp.jsonrpc import Connection

READ_LIMIT = 1 << 26  # a prompt may embed a whole file in one line


def split_argv(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """tau-acp's own arguments, and the ``tau serve`` arguments after ``--``."""
    args = list(argv)
    if "--" not in args:
        return args, []
    cut = args.index("--")
    return args[:cut], args[cut + 1 :]


async def _streams(out: BinaryIO) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=READ_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, out)
    return reader, asyncio.StreamWriter(transport, protocol, None, loop)


async def run_agent(serve_args: Sequence[str], out: BinaryIO) -> None:
    """Speak ACP on stdin and ``out`` until stdin closes, then close every session."""
    reader, writer = await _streams(out)
    agent = Agent(serve_args)
    agent.conn = Connection(reader, writer, agent.request, agent.notification)
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    assert main_task is not None
    for sig in (signal.SIGTERM, signal.SIGINT):  # an editor may stop us with a signal
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        await agent.conn.run()
    except asyncio.CancelledError:
        pass  # a signal: the sessions are closed below, and that is a clean exit
    finally:
        await agent.close()


def main(argv: Sequence[str] | None = None) -> int:
    """The ``tau-acp`` entry point."""
    own, serve_args = split_argv(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="tau-acp",
        description="Run as an Agent Client Protocol agent on stdio, driving τ. "
        "Arguments after -- go to every `tau serve` it starts.",
    )
    parser.add_argument("--version", action="version", version=f"tau-acp {__version__}")
    parser.parse_args(own)
    for fd, name in ((0, "stdin"), (1, "stdout")):
        if not stat.S_ISFIFO(mode := os.fstat(fd).st_mode) and not stat.S_ISSOCK(mode):
            print(f"tau-acp: {name} must be a pipe or socket: an editor starts it", file=sys.stderr)
            return 2
    # Stdout carries only protocol messages: keep a private copy of it for them, and point fd 1
    # at stderr so that a stray print, or a child that inherits it, cannot corrupt the stream.
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    asyncio.run(run_agent(serve_args, out))
    return 0
