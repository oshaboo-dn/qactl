"""qactl — one agent-shaped CLI for an entire QA workflow.

A single executable that unifies every surface a QA engineer drives:

    cli / nc / gnmi / rc / setup   DNOS devices   (vendored dnctl)
    ixia                           IxNetwork      (vendored ixiactl)
    jira / confluence / jenkins    Atlassian/CI   (native)

One shell-driven contract across all of them: ``--json`` everywhere,
real exit codes, stdin payloads, and a ``--yes`` confirm gate on
destructive ops. Credentials resolve at runtime from the environment;
none are stored in the repo.
"""

__version__ = "0.2.0"
