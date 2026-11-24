"""The ``tau-acp`` command line."""

import argparse
import sys
from collections.abc import Sequence

from tau_acp import __version__
from tau_acp.bridge import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tau-acp",
        description="Bridge between Agent Client Protocol editors and a τ session.",
    )
    parser.add_argument("--version", action="version", version=f"tau-acp {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the ACP agent over stdio (not built yet)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    try:
        serve()
    except NotImplementedError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0
