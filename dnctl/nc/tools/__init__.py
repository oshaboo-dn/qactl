"""Tool surface for the ``dnctl nc`` (NETCONF) group.

Each module in this package exposes plain tool functions plus a
``register(mcp)`` entry point. ``register`` is retained so the modules
stay liftable onto a FastMCP server; the ``dnctl nc`` Typer front-end
(:mod:`dnctl.nc.app`) imports and calls the functions directly.
"""
