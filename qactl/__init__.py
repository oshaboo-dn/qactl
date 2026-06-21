"""qactl — one agent-shaped tool for an entire QA workflow, CLI or MCP.

A single executable that unifies every surface a QA engineer drives:

    cli / nc / gnmi / rc / setup   DNOS devices   (vendored dnctl)
    ixia                           IxNetwork      (vendored ixiactl)
    jira / confluence / jenkins    Atlassian/CI   (native)

Two fronts over one shared tool layer: the shell-driven CLI
(``--json`` everywhere, real exit codes, stdin payloads, ``--yes``
confirm gate) and a local stdio MCP server (``qactl mcp <group>``).
Credentials resolve at runtime from the environment; none are stored in
the repo.
"""

__version__ = "0.5.0"
