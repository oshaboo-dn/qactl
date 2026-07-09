"""Cisco vendor plugin — read-only ``show`` for now (IOS / IOS-XE / NX-OS).

Best-effort dialect distilled from the common Cisco CLI surface; the
exact prompt shapes / pager command / error strings should be confirmed
against a live box (per the repo's ssh-vs-cli rule: explore over SSH,
then harden here). Cisco supports only the ``show`` capability today —
no candidate edit, no operational mutations — so every other tool family
is gated off with a "not implemented" envelope.
"""

from __future__ import annotations

import re

from qactl.dnctl.cli.vendors.base import CAP_SHOW, Dialect, VendorPlugin, compile_patterns

# Cisco prompts across platforms:
#   IOS / IOS-XE / NX-OS user-exec ``R1>`` / privileged ``R1#``,
#   config-mode ``R1(config)#`` / ``R1(config-if)#``, and
#   IOS-XR ``RP/0/RSP0/CPU0:hostname#`` (the node/location prefix carries
#   ``/`` and ``:``), config ``RP/0/RSP0/CPU0:hostname(config)#``.
# So the prompt token allows word chars plus ``.-/:`` , an optional
# parenthesised mode context, and a trailing ``>`` or ``#``; anchored at
# the end of the buffer (detection) or as a lone line (trim).
CISCO_DIALECT = Dialect(
    name="cisco",
    prompt_re=re.compile(
        r"(?P<prompt>^[\w.\-/:]+(?:\([^)]*\))?[>#])[ \t]*\Z",
        re.MULTILINE,
    ),
    tail_re=re.compile(r"^[\w.\-/:]+(?:\([^)]*\))?[>#][ \t]*$"),
    # `terminal length 0` disables the pager for this exec session (valid
    # on IOS / IOS-XE / NX-OS / IOS-XR).
    page_off=("terminal length 0",),
    strip_paren_context=True,
)

PLUGIN = VendorPlugin(
    name="cisco",
    capabilities=frozenset({CAP_SHOW}),
    dialect=CISCO_DIALECT,
    error_patterns=compile_patterns(
        r"^%",                              # % Invalid input detected ...
        r"(?i)\binvalid input\b",
        r"(?i)\bambiguous command\b",
        r"(?i)\bincomplete command\b",
        r"(?i)\bunknown command\b",
        r"(?i)\bunrecognized command\b",
        r"(?i)\bsyntax error\b",
        r"(?i)^error:",
    ),
)
