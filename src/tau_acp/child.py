"""The processes tau-acp starts (each ``tau serve``, each MCP server), and how they are signalled.

A pid is only safe to signal while the process has not been reaped: after that, the number can
name another process. asyncio reaps a child as soon as it exits, so a ``returncode`` checked
earlier proves nothing. Where Linux offers them, tau-acp therefore holds a pidfd for each child,
opened when it starts, and signals through it: a pidfd names that one process for good.

A child is also tied to tau-acp's lifetime (Linux ``PR_SET_PDEATHSIG``): if tau-acp is killed
in a way it cannot clean up after (SIGKILL), the kernel sends the child SIGTERM.
"""

import asyncio
import contextlib
import ctypes
import functools
import os
import select
import signal
import sys
from collections.abc import Callable
from pathlib import Path

_PR_SET_PDEATHSIG = 1
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)  # SIGHUP: the terminal closed
_LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform == "linux" else None


def _setup(parent: int) -> None:
    """In the child, before exec. The signals tau-acp stops on take their default action again:
    once tau-acp is shutting down it ignores them, and an ignored signal stays ignored across
    exec. On Linux, the child gets SIGTERM when ``parent`` dies, and exits now if it already has
    (it died between the fork and this call)."""
    for number in STOP_SIGNALS:
        signal.signal(number, signal.SIG_DFL)
    if _LIBC is not None and (
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent
    ):
        os._exit(1)


def tied() -> Callable[[], None]:
    """The ``preexec_fn`` for every child: see ``_setup``. Where the platform cannot tie a child
    to tau-acp's lifetime (anywhere but Linux), a SIGKILLed tau-acp leaves its children."""
    return functools.partial(_setup, os.getpid())


def ignore_stop_signals() -> None:
    """From here on, ``STOP_SIGNALS`` are ignored, for good: the loop's handlers go, and
    a signal that comes during the change is held, then dropped."""
    loop = asyncio.get_running_loop()
    signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        for number in STOP_SIGNALS:
            loop.remove_signal_handler(number)
            signal.signal(number, signal.SIG_IGN)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)


class Child:
    """A process tau-acp started and owns, signalled only while it runs."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.pid = process.pid
        self._fd: int | None = None
        if hasattr(os, "pidfd_open") and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):  # it has exited already
                self._fd = os.pidfd_open(process.pid)

    def running(self) -> bool:
        """Whether it has not exited. While this is true and the caller does not yield to the
        event loop, its pid and its process group id name it and its group, and nothing else."""
        if self.process.returncode is not None:
            return False
        if self._fd is None:  # no pidfds here; or there are, and it had exited when it started
            return not hasattr(os, "pidfd_open")
        return not select.select([self._fd], [], [], 0)[0]  # readable once it exits

    def send(self, number: signal.Signals) -> None:
        """Send it ``number``, unless it has exited."""
        if self._fd is not None:
            with contextlib.suppress(ProcessLookupError):  # exited, not yet reaped
                signal.pidfd_send_signal(self._fd, number)
        elif self.running():
            self.process.send_signal(number)

    def send_group(self, number: signal.Signals) -> None:
        """Send ``number`` to its process group (it leads one: ``start_new_session``), unless it
        has exited: once it has, the group id could name someone else's group."""
        if self.running():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pid, number)

    def group(self) -> list[int]:
        """Pidfds for the other processes in its group now, for ``signal_all`` once it has
        exited. Empty where there is no ``/proc`` or no pidfd, or once it has exited."""
        if not self.running() or not hasattr(os, "pidfd_open"):
            return []
        fds = []
        for pid in _group_members(self.pid):
            with contextlib.suppress(OSError):  # gone since the listing
                fds.append(os.pidfd_open(pid))
        if self.running():  # it ran throughout: each pid had our group id, so each is ours
            return fds
        close_all(fds)
        return []

    def close(self) -> None:
        """Release the pidfd."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def signal_all(fds: list[int], number: signal.Signals) -> None:
    """Send ``number`` to each process of a ``Child.group`` that is still running."""
    for fd in fds:
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(fd, number)


def close_all(fds: list[int]) -> None:
    """Release the pidfds of a ``Child.group``."""
    for fd in fds:
        os.close(fd)


def _group_members(pgid: int) -> list[int]:
    """The pids whose process group is ``pgid``, other than ``pgid`` itself, from ``/proc``."""
    members = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        with contextlib.suppress(OSError, ValueError, IndexError):
            fields = stat.read_text().rsplit(")", 1)[1].split()  # after "pid (comm)"
            pid = int(stat.parent.name)
            if pid != pgid and int(fields[2]) == pgid:
                members.append(pid)
    return members
