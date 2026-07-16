"""``qactl.spirent.ctl`` — the command-line front for Spirent TestCenter.

A process-per-invocation CLI (the sibling of ``qactl.ixia.ctl``) that drives
a Spirent TestCenter REST server over ``stcrestclient``: ``--json`` everywhere,
real exit codes, a ``--yes`` confirm gate on destructive ops, and session
reattach so consecutive calls share one STC session.

Scaffold (2026-07-16): session lifecycle only.
"""

__version__ = "0.1.0"
