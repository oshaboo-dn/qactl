"""Global-flag carrier shared by every subcommand.

Typer stores one :class:`Ctx` on ``ctx.obj`` at the root callback; each
subcommand reads it for the connection target (``--device`` / ``--host``
/ ``--user`` / ``--password`` / ``--port`` / ``--timeout`` /
``--no-verify``) and the two output/safety switches (``--json`` /
``--yes``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Ctx:
    device: Optional[str] = None
    host: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    port: Optional[int] = None
    timeout: Optional[int] = None
    no_verify: bool = True
    json: bool = False
    yes: bool = False
    log: Optional[str] = None
