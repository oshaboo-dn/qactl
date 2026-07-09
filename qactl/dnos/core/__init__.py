"""qactl.core — the shared layer used by every subcommand group.

Lifted verbatim from the monorepo's ``dn_common`` (device map I/O,
credentials, dnftp SFTP, validators, request-log helpers) plus the
front-end glue that makes qactl agent-shaped:

- :mod:`qactl.core.registry` — device alias → mgmt-IP / SN (façade over
  :mod:`qactl.core.devices`).
- :mod:`qactl.core.auth`     — credential resolution (façade over
  :mod:`qactl.core.credentials`).
- :mod:`qactl.core.output`   — text vs ``--json`` formatter + exit codes.
- :mod:`qactl.core.payload`  — stdin (``-``) / ``--file`` / inline body.
- :mod:`qactl.core.confirm`  — ``--yes`` destructive-op gate.
- :mod:`qactl.core.paths`    — portable state / data dir resolution.
- :mod:`qactl.core.context`  — global-flag carrier.
"""

__version__ = "0.1.0"
