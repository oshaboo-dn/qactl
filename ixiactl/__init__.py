"""ixiactl — a command-line front-end for an IxNetwork REST API server.

A process-per-invocation CLI that drives IxNetwork session / config /
topology / protocol / traffic / stats operations. It reuses the same
RestPy wrapper, NGPF builders, and response-envelope contract as the
``ixia-mcp`` server it was lifted from, swapping the MCP front-end for a
shell-shaped one (``--json`` everywhere, exit codes, a ``--yes`` confirm
gate, and session reattach so consecutive calls share one IxNetwork
session).
"""

__version__ = "0.1.0"
