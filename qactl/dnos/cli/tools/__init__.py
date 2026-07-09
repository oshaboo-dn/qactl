"""MCP tool surface for the CLI MCP server.

Each module in this package exposes plain tool functions plus a
``register(mcp)`` entry point that wires them onto a FastMCP instance.
The entry-point file (``cli_mcp_server.py``) imports the modules and
calls their ``register`` functions in turn — there are no import-time
side effects.
"""
