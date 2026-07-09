"""Arista vendor plugin — read-only ``show`` for now (Arista EOS).

Arista EOS speaks an IOS-style CLI: user-exec ``host>`` / enable ``host#``
/ config ``host(config)#`` prompts, ``terminal length 0`` to disable the
pager, and ``% ...`` rejection lines. Best-effort dialect distilled from
that common surface; confirm the prompt shapes / pager command / error
strings against a live box before relying on them (per the repo's
ssh-vs-cli rule: explore over SSH, then harden here). EOS supports only
the ``show`` capability today — no candidate edit, no operational
mutations — so every other tool family is gated off, exactly like the
other non-DNOS vendors.
"""

from __future__ import annotations

import re

from qactl.dnctl.cli.vendors.base import CAP_SHOW, Dialect, VendorPlugin, compile_patterns

# Arista EOS prompts mirror Cisco IOS: user-exec ``host>`` / enable
# ``host#`` / config ``host(config)#`` / ``host(config-if)#``. The prompt
# token allows word chars plus ``.-`` , an optional parenthesised mode
# context, and a trailing ``>`` or ``#``; anchored at the end of the
# buffer (detection) or as a lone line (trim).
ARISTA_DIALECT = Dialect(
    name="arista",
    prompt_re=re.compile(
        r"(?P<prompt>^[\w.\-]+(?:\([^)]*\))?[>#])[ \t]*\Z",
        re.MULTILINE,
    ),
    tail_re=re.compile(r"^[\w.\-]+(?:\([^)]*\))?[>#][ \t]*$"),
    # `terminal length 0` disables the pager for this exec session on EOS.
    page_off=("terminal length 0",),
    strip_paren_context=True,
)

PLUGIN = VendorPlugin(
    name="arista",
    capabilities=frozenset({CAP_SHOW}),
    dialect=ARISTA_DIALECT,
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
