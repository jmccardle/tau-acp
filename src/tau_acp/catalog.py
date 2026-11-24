"""``session/list``: τ's session catalog, as ACP session infos."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tau

from tau_acp.jsonrpc import log

TITLE_WIDTH = 80


def tau_home() -> Path:
    """``$TAU_HOME``, or ``~/.tau``, the way τ documents it (a leading ``~`` is ``$HOME``). τ has
    no public function for this, so the rule is repeated here."""
    given = os.environ.get("TAU_HOME")
    home = Path(os.environ.get("HOME") or Path.home())
    if not given:
        return home / ".tau"
    if given == "~" or given.startswith("~/"):
        return home / given[2:]
    if not Path(given).is_absolute():
        raise ValueError(f"TAU_HOME must be an absolute path, not {given!r}")
    return Path(given)


def _title(row: tau.SessionInfo) -> str | None:
    line = " ".join((row.name or row.first or "").split())
    if not line:
        return None
    return line if len(line) <= TITLE_WIDTH else line[: TITLE_WIDTH - 1] + "…"


def _info(row: tau.SessionInfo) -> dict[str, Any]:
    info: dict[str, Any] = {"sessionId": row.id, "cwd": row.cwd}
    if (title := _title(row)) is not None:
        info["title"] = title
    if row.updated is not None:
        info["updatedAt"] = datetime.fromtimestamp(row.updated, UTC).isoformat()
    return info


def _rows(cwd: Path | None) -> list[dict[str, Any]]:
    infos = []
    for row in tau.list_sessions(tau_home() / "sessions", cwd=cwd):
        if row.error or not row.cwd:
            # Not a session the editor could load, and ACP requires a cwd: leave it out, loudly.
            log(f"session/list: skipping {row.path}: {row.error or 'no cwd'}")
        else:
            infos.append(_info(row))
    return infos


async def list_sessions(cwd: Path | None) -> list[dict[str, Any]]:
    """The sessions of τ's catalog, newest first, those of ``cwd`` only when it is given."""
    return await asyncio.to_thread(_rows, cwd)
