"""Juniper vendor plugin — read-only ``show`` for now (Junos).

Best-effort dialect distilled from the common Junos CLI surface; confirm
the prompt shapes / pager command / error strings against a live box
before relying on them (explore over SSH, then harden here). Junos rides
on stock OpenSSH so it is indistinguishable at the banner — the vendor
is taken from the registry entry, not sniffed. Juniper supports only the
``show`` capability today; every other tool family is gated off.
"""

from __future__ import annotations

import re

from qactl.dnos.cli.vendors.base import CAP_SHOW, Dialect, VendorPlugin, compile_patterns

# Junos operational prompt is ``user@host>`` (configuration mode is
# ``user@host#`` under an ``[edit]`` banner, but config is out of scope).
# A ``{master:0}`` / ``{master}`` RE-status line may precede the prompt;
# we anchor on the ``user@host>`` line itself at end-of-buffer.
JUNIPER_DIALECT = Dialect(
    name="juniper",
    prompt_re=re.compile(
        r"(?P<prompt>^[\w.\-]+@[\w.\-]+[>#])[ \t]*\Z",
        re.MULTILINE,
    ),
    tail_re=re.compile(r"^[\w.\-]+@[\w.\-]+[>#][ \t]*$"),
    # Disable the Junos CLI pager for this session.
    page_off=("set cli screen-length 0", "set cli screen-width 0"),
    strip_paren_context=False,
)

PLUGIN = VendorPlugin(
    name="juniper",
    capabilities=frozenset({CAP_SHOW}),
    dialect=JUNIPER_DIALECT,
    error_patterns=compile_patterns(
        r"(?i)^error:",
        r"(?i)\bunknown command\b",
        r"(?i)\bsyntax error\b",
        r"(?i)\binvalid value\b",
        r"(?i)\bmissing argument\b",
        r"(?i)^\s*\^\s*$",                  # caret pointing at the bad token
        r"(?i)^unknown command\.",
    ),
)
