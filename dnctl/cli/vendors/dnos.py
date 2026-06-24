"""DNOS vendor plugin — the reference, fully-featured vendor.

The DNOS prompt regexes and error patterns already live in
``dnctl.cli.core.shell`` / ``dnctl.cli.core.errors`` (where the rest of
the DNOS transport logic uses them); this plugin reuses them verbatim so
there is a single source of truth and the default (no-dialect) transport
path stays byte-for-byte identical to pre-plugin behaviour.
"""

from __future__ import annotations

from dnctl.cli.core import errors as _errors
from dnctl.cli.core import shell as _shell
from dnctl.cli.vendors.base import ALL_CAPABILITIES, Dialect, VendorPlugin

DNOS_DIALECT = Dialect(
    name="dnos",
    prompt_re=_shell._PROMPT_RE,
    tail_re=_shell._TAIL_PROMPT_RE,
    page_off=("set cli-terminal-length 0",),
    strip_paren_context=True,
)

# DNOS is the reference vendor: it supports every tool family. Its error
# detection reuses the canonical DNOS pattern set so the plugin and the
# legacy ``errors.detect_error`` agree line-for-line.
PLUGIN = VendorPlugin(
    name="dnos",
    capabilities=frozenset(ALL_CAPABILITIES),
    dialect=DNOS_DIALECT,
    error_patterns=tuple(_errors._ERR_PATTERNS),
)
