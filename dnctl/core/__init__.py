"""dnctl.core — the shared layer used by every subcommand group.

Lifted verbatim from the monorepo's ``dn_common`` (device map I/O,
credentials, dnftp SFTP, validators, request-log helpers) plus the
front-end glue that makes dnctl agent-shaped:

- :mod:`dnctl.core.registry` — device alias → mgmt-IP / SN (façade over
  :mod:`dnctl.core.devices`).
- :mod:`dnctl.core.auth`     — credential resolution (façade over
  :mod:`dnctl.core.credentials`).
- :mod:`dnctl.core.output`   — text vs ``--json`` formatter + exit codes.
- :mod:`dnctl.core.payload`  — stdin (``-``) / ``--file`` / inline body.
- :mod:`dnctl.core.confirm`  — ``--yes`` destructive-op gate.
- :mod:`dnctl.core.paths`    — portable state / data dir resolution.
- :mod:`dnctl.core.context`  — global-flag carrier.
"""

__version__ = "0.1.0"
