"""dnctl — one agent-shaped CLI over DriveNets DNOS devices.

Collapses four MCP servers (cli / netconf / gnmi / restconf) into a
single command-line tool. The device/RPC layer is lifted verbatim from
those servers; this package adds a shared ``core`` (registry, auth,
output, payload, confirm) and a Typer front-end.
"""

__version__ = "0.2.0"
