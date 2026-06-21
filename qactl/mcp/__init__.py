"""The qactl MCP front: expose the shared tool layer over stdio.

``qactl mcp <group> [<group> ...]`` launches a local FastMCP server that
speaks JSON-RPC over stdio and registers the selected groups' tools. The
same envelope-returning functions the CLI calls are registered here, so
the two fronts stay in lockstep. See :mod:`qactl.mcp.registry` for the
group -> tool surface map (including which tools stay CLI-only).
"""

from qactl.mcp.registry import ALL_GROUPS, list_group_tools, register_group

__all__ = ["ALL_GROUPS", "list_group_tools", "register_group"]
