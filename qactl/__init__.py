"""qactl — one agent-shaped CLI for a QA engineer's external services.

A single executable that unifies the tools a QA workflow touches outside
the lab — Jira, Confluence, and Jenkins — behind one consistent,
shell-driven contract: ``--json`` everywhere, real exit codes, stdin
payloads, and a ``--yes`` confirm gate on destructive ops. Credentials
are resolved at runtime from the environment; none are stored in the
repo.

Pairs with the device/traffic CLIs (`dnctl`, `ixiactl`); those remain
their own tools/repos.
"""

__version__ = "0.1.0"
