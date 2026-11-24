#!/usr/bin/env python3
"""Fail if any function under src/ has a cyclomatic complexity (radon) over the limit."""

import json
import subprocess
import sys
from pathlib import Path

LIMIT = 15
SRC = Path(__file__).resolve().parent.parent / "src"


def main() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "radon", "cc", "-j", "-n", "C", str(SRC)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    over = [
        f"{path}:{block['lineno']} {block['name']}: CC {block['complexity']} > {LIMIT}"
        for path, blocks in json.loads(out).items()
        for block in blocks
        if block["complexity"] > LIMIT
    ]
    print(
        "\n".join(over) if over else f"cyclomatic complexity: every function in src/ is <= {LIMIT}"
    )
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
