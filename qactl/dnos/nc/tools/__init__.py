"""Tool surface for the ``qactl nc`` (NETCONF) group.

Each module in this package exposes plain tool functions plus a
``register(mcp)`` entry point. ``register`` is retained so the modules
stay liftable onto a FastMCP server; the ``qactl nc`` Typer front-end
(:mod:`qactl.nc.app`) imports and calls the functions directly.
"""
