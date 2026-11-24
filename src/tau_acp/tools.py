"""Editor-provided MCP tools, exposed to τ as tools.

Future role: ACP clients may hand the agent MCP servers; this module would make
those tools callable by a τ session. Not built yet.
"""


def expose_mcp_tools() -> None:
    """Make the editor's MCP tools available to the τ session."""
    raise NotImplementedError(
        "tau-acp: exposing editor-provided MCP tools to τ is not built yet; "
        "it depends on the bridge's shape, which awaits a design decision"
    )
