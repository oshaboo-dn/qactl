"""Run a Linux one-liner on a device via DNOS' ``run start shell``.

Two responsibilities, one home:

1. ``_build_shell_entry`` translates the user-facing
   ``ncc`` / ``ncp`` / ``container`` selectors into the DNOS
   ``run start shell ...`` line that picks the right execution context
   on the device.
2. ``run_linux_on_device`` is the standard tool body for the shell-exec
   pattern: thin wrapper around ``_run_on_device(mode="shell_exec", ...)``
   that hides the magic mode string and lets every shell-exec tool say
   what it actually means ("run this Linux command on a device").

Tools that need the raw ``RunResult`` (e.g. the per-device TZ probe used
by the trace tools) still call ``qactl.cli.core.session.run_once`` directly —
that's a different shape and not what this module is for.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from qactl.dnos.cli.core.runner import _run_on_device


_SHELL_CONTAINER_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
# NCM ids are shelf-relative tokens like ``A0`` / ``B0`` (and similar);
# keep the matcher permissive but safe (alphanumeric, no whitespace).
_SHELL_NCM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]*$")


# --- read-only classification (the ``shell`` --yes gate) -------------------
#
# ``run start shell`` exec is destructive by default, but the common use is
# pure inspection (``grep``/``ps``/``cat /proc/...`` etc.). ``is_read_only``
# lets the CLI drop the ``--yes`` gate for provably read-only command lines
# while keeping it for everything else. It is deliberately FAIL-CLOSED: only
# a line whose every segment starts with a known read-only binary — and
# which carries no redirection, command substitution, or known write flag —
# is read-only; anything unrecognised stays gated. Relaxing here can only
# ever remove friction for safe commands, never open a new write path.

# Binaries that cannot mutate device state in ordinary use. Mutating tools
# (``rm``/``tee``/``dd``/``ip``/``sed -i``/``xargs`` ...) are intentionally
# absent so they keep the gate.
_READ_ONLY_BINS = frozenset({
    "cat", "tac", "zcat", "ls", "grep", "egrep", "fgrep", "zgrep",
    "head", "tail", "wc", "stat", "readlink", "realpath", "ldd",
    "file", "echo", "printf", "pwd", "whoami", "id", "uname",
    "hostname", "env", "printenv", "date", "uptime", "free", "df",
    "du", "lsof", "netstat", "ss", "dmesg", "cut", "sort", "uniq",
    "tr", "basename", "dirname", "true", "nproc", "lscpu", "lsmod",
    "md5sum", "sha1sum", "sha256sum", "strings", "nm", "objdump",
    "readelf", "xxd", "od", "hexdump", "ps", "find", "which", "type",
    "vmstat", "iostat", "getconf", "column", "comm", "fold", "expand",
})

# ``find`` is read-only only without an action that writes or runs a command.
_FIND_WRITE_FLAGS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprintf", "-fls",
})

_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
# Shell operators that separate one command from the next.
_SEGMENT_OPS = frozenset({";", "|", "&"})


def _split_segments(raw: str) -> Tuple[Optional[list], bool]:
    """Quote-aware split of a command line into command segments.

    Walks the string tracking single/double quotes and backslash escapes,
    so an operator *inside quotes* (e.g. the ``|`` in ``grep 'a|b'``) is not
    mistaken for a pipe. Splits on unquoted ``;`` / ``|`` / ``&`` runs
    (``&&`` / ``||`` / ``;;`` collapse to one boundary).

    Returns ``(segments, has_write)``. ``has_write`` is ``True`` if an
    unquoted output redirection (``>``), command substitution (``$(``), or
    backtick appears. ``segments`` is ``None`` on an unbalanced quote — the
    caller treats either as "not provably read-only".
    """
    segments: list = []
    cur: list = []
    has_write = False
    quote: Optional[str] = None
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            i += 1
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            i += 1
        elif ch == "\\":
            cur.append(ch)
            if i + 1 < n:
                cur.append(raw[i + 1])
                i += 2
            else:
                i += 1
        elif ch == ">" or ch == "`":
            has_write = True
            i += 1
        elif ch == "$" and i + 1 < n and raw[i + 1] == "(":
            has_write = True
            i += 2
        elif ch in _SEGMENT_OPS:
            segments.append("".join(cur))
            cur = []
            while i < n and raw[i] in _SEGMENT_OPS:
                i += 1
        else:
            cur.append(ch)
            i += 1
    if quote is not None:
        return None, True
    segments.append("".join(cur))
    return segments, has_write


def is_read_only_shell(commands: Any) -> bool:
    """True iff every command line is provably a read-only inspection.

    Used by ``qactl cli shell`` to decide whether the destructive ``--yes``
    gate applies. Fail-closed: returns ``False`` on anything it cannot prove
    safe (unknown binary, redirection, command substitution, a writing
    ``find`` action, an unbalanced quote), so an unrecognised line is treated
    as a write.
    """
    if isinstance(commands, str):
        commands = [commands]
    saw_command = False
    for raw in commands or []:
        if not raw or not raw.strip():
            continue
        segments, has_write = _split_segments(raw)
        if has_write or segments is None:
            return False
        for segment in segments:
            tokens = segment.split()
            # Step past leading ``VAR=value`` assignments to the real command.
            i = 0
            while i < len(tokens) and _ASSIGNMENT_RE.fullmatch(tokens[i]):
                i += 1
            if i >= len(tokens):
                continue
            base = tokens[i].rsplit("/", 1)[-1]  # /usr/bin/grep -> grep
            if base not in _READ_ONLY_BINS:
                return False
            if base == "find" and any(t in _FIND_WRITE_FLAGS for t in tokens[i + 1:]):
                return False
            saw_command = True
    return saw_command


# ``run start shell`` grammar (crawled):
#   run start shell                                → active NCC default container
#   run start shell ncc <0|1|active>               → select NCC
#   run start shell ncc <id> container <name>      → select NCC + container
#   run start shell ncp <0-191|bfd-master>         → select NCP (line card)
#   run start shell ncm <A0|B0|...>                → select NCM (fabric mgmt)
# ncf is deferred.


def _build_shell_entry(
    ncc: Optional[str],
    ncp: Optional[str],
    container: Optional[str],
    ncm: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Translate ncc/ncp/ncm/container into the DNOS ``run start shell ...`` line.

    - all None   → ``run start shell`` (active NCC default container).
    - ncc only   → ``run start shell ncc <id>``.
    - container  → ``run start shell ncc <id|active> container <name>``.
    - ncp only   → ``run start shell ncp <id>``.
    - ncm only   → ``run start shell ncm <id>``.
    - ncc/ncp/ncm together → error (mutually exclusive).
    - ncp/ncm + container → error (grammar has no container under ncp/ncm).
    """
    targets = [t for t in (ncc, ncp, ncm) if t is not None]
    if len(targets) > 1:
        return None, "ncc, ncp, and ncm are mutually exclusive."
    if ncp is not None and container is not None:
        return (
            None,
            "container cannot be combined with ncp; ``run start shell ncp`` "
            "has no container sub-option.",
        )
    if ncm is not None and container is not None:
        return (
            None,
            "container cannot be combined with ncm; ``run start shell ncm`` "
            "has no container sub-option.",
        )

    ncm_val: Optional[str] = None
    if ncm is not None:
        s = str(ncm).strip()
        if not _SHELL_NCM_RE.fullmatch(s):
            return None, "ncm must be an id like 'A0' or 'B0'."
        ncm_val = s

    ncc_val: Optional[str] = None
    if ncc is not None:
        s = str(ncc).strip()
        if s not in ("0", "1", "active"):
            return None, "ncc must be '0', '1', or 'active'."
        ncc_val = s

    ncp_val: Optional[str] = None
    if ncp is not None:
        s = str(ncp).strip()
        if s == "bfd-master":
            ncp_val = s
        else:
            if not s.isdigit() or not (0 <= int(s) <= 191):
                return None, "ncp must be 0..191 or 'bfd-master'."
            ncp_val = s

    if container is not None:
        if not isinstance(container, str) or not container.strip():
            return None, "container must be a non-empty string when provided."
        cs = container.strip()
        if not _SHELL_CONTAINER_RE.fullmatch(cs):
            return None, "container must match [A-Za-z0-9_-]+."
        target = ncc_val or "active"
        return f"run start shell ncc {target} container {cs}", None

    if ncm_val is not None:
        return f"run start shell ncm {ncm_val}", None
    if ncp_val is not None:
        return f"run start shell ncp {ncp_val}", None
    if ncc_val is not None:
        return f"run start shell ncc {ncc_val}", None
    return "run start shell", None


def run_linux_on_device(
    tool: str,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    linux_command: str,
    timeout: float,
    next_action_on_error: str,
    *,
    shell_entry: str = "run start shell",
) -> Dict[str, Any]:
    """Run a Linux one-liner inside ``run start shell`` (or a variant).

    Drop-in replacement for ``_run_on_device(mode="shell_exec", ...)``.
    Pass the ``shell_entry`` returned by :func:`_build_shell_entry` to
    target a specific NCC / NCP / container; omit it for the default
    (active NCC, default container).
    """
    return _run_on_device(
        tool, device, host, user, password,
        linux_command, timeout, next_action_on_error,
        mode="shell_exec", shell_entry=shell_entry,
    )
