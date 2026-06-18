"""MCP tool surface for the NETCONF MCP server.

Each module in this package exposes plain tool functions plus a
``register(mcp)`` entry point that wires them onto a FastMCP instance.
The entry-point file (``netconf_mcp_server.py``) imports the modules and
calls their ``register`` functions in turn — there are no import-time
side effects.

Mirrors the layout of ``cli-mcp/dnctl.cli.tools/``; the two MCP servers are
deployed independently and share no Python code.
"""
