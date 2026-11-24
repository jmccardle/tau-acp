"""The ACP agent side over stdio, translating to a τ ``RemoteClient``.

Future role: speak Agent Client Protocol (JSON-RPC 2.0 over stdin/stdout) to an
editor, and drive a ``tau serve`` session on its behalf. Not built yet.
"""


def serve() -> None:
    """Run the ACP agent over stdio until the editor disconnects."""
    raise NotImplementedError(
        "tau-acp: the ACP bridge is not built yet; its shape (beside tau serve, "
        "or on the editor's machine) awaits a design decision"
    )
