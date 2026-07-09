"""Interactive SSH shell IO for DriveNets DNOS CLI.

Connects via paramiko.invoke_shell(), tracks the DNOS prompt, and runs
commands by writing a line and reading until the prompt re-appears.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Optional, Tuple

import paramiko

if TYPE_CHECKING:  # avoid an import cycle (vendor plugins import this module)
    from qactl.dnos.cli.vendors.base import Dialect


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# DNOS prompt: hostname + optional parenthesised context (timestamp or
# "(config)") + '#' at end of a line. Accept trailing whitespace after '#'.
# DNOS often rewrites the prompt on every command with a fresh timestamp,
# so we treat the *shape* as the signal and the hostname as the stable part.
_PROMPT_RE = re.compile(
    r"(?P<prompt>^[\w.\-]+(?:\([^)]*\))?#)[ \t]*\Z",
    re.MULTILINE,
)
# Generic trailing-prompt matcher (same shape, used for trim).
_TAIL_PROMPT_RE = re.compile(r"^[\w.\-]+(?:\([^)]*\))?#[ \t]*$")

# Linux shell prompt inside ``run start shell``. Looks like
# ``(dn40-cl-301a-ncc1)root@routing_engine:/[2026-04-20 21:55:32][inband_ns]#``
# — the trailing ``]#`` is the stable signal (timestamp + netns vary).
_SHELL_PROMPT_RE = re.compile(r"\]#[ \t]*\Z")
_SHELL_TAIL_RE = re.compile(r"\]#[ \t]*$")

# NCM (ICOS-style) nested-CLI prompts seen inside ``run start shell ncm <id>``.
# The NCM is a management switch with its own CLI, not Linux and not DNOS:
#   exec        → ``<hostname>#``                 e.g. ``AAF-NCM-A0#``
#   config      → ``(config)#`` / ``<hostname>(config)#``
#   interface   → ``(conf-if-eth-0/X)#``
# We match on *shape* (a hostname and/or a parenthesised context immediately
# before ``#``) rather than a fixed token, because the prompt mutates as we
# descend into config / interface modes. The two alternatives below require
# either a real hostname or a ``(...)`` block before ``#`` — so a bare ``#``
# never matches, and the Linux ``...]#`` shell prompt (``]`` is neither) is
# deliberately excluded too, keeping this disjoint from the shell matcher.
_NCM_PROMPT_RE = re.compile(
    r"(?:[\w.\-]+(?:\([^)]*\))?|\([^)]*\))#[ \t]*\Z"
)
_NCM_TAIL_RE = re.compile(
    r"(?:[\w.\-]+(?:\([^)]*\))?|\([^)]*\))#[ \t]*$"
)

# Password prompt emitted by DNOS immediately after ``run start shell``.
# Not newline-terminated, so we match it anchored to end-of-buffer.
_PASSWORD_RE = re.compile(r"[Pp]assword:[ \t]*\Z")

# Yes/No confirmation prompt emitted by DNOS commands like
# ``request system target-stack load`` / ``request system tech-support`` /
# ``request system restart`` when ``set cli-no-confirm`` is NOT in effect.
# Observed forms (case-insensitive):
#   ``...are you sure you want to delete (yes/no)?``
#   ``Continue (Yes/No)?``
#   ``Are you sure? (Yes/No)?``
#   ``Continue [y/n]?``
#   ``Do you want to continue? (yes/no) [no]?``  <-- GI uses this one,
#       with a ``[no]`` default-value annotation between the choices and
#       the final ``?``.
# We match the trailing ``(yes/no) [<default>]?`` shape anchored at the
# end of the buffer (the device usually waits for input right after this
# token without printing a newline). On the GI (Genesis Image) shell —
# which doesn't accept ``set cli-no-confirm`` — we have to answer this
# inline or the channel hangs forever. The default annotation is
# typically ``[no]``; sending ``yes\n`` overrides it explicitly.
_CONFIRM_RE = re.compile(
    r"[\(\[]\s*(?:yes\s*/\s*no|y\s*/\s*n)\s*[\)\]]"   # (yes/no) | [y/n]
    r"(?:\s*\[\s*(?:yes|no|y|n)\s*\])?"               # optional [default]
    r"\s*\??[ \t]*\Z",                                # trailing '?' + EOL
    re.IGNORECASE,
)

# Interactive "rebase" prompt DNOS raises on a live ``commit`` when another
# session committed since this candidate's transaction started, e.g.::
#
#   Warning: User 'dnroot' committed at 03-Jul-2025 06:48:02 UTC, your
#   configuration is out of sync.
#   What would you like to do (commit, merge-only, abort) [abort]?
#
# The device then waits for input right after the ``?`` (no trailing newline),
# so an apply that doesn't answer it hangs until the per-step timeout. We match
# the question line anchored at end-of-buffer; the option set / ``[default]``
# spelling can drift across builds, so keep the choices/whitespace lenient.
_COMMIT_CONFLICT_RE = re.compile(
    r"what\s+would\s+you\s+like\s+to\s+do\s*"
    r"\(\s*commit\s*,\s*merge-only\s*,\s*abort\s*\)"   # (commit, merge-only, abort)
    r"(?:\s*\[\s*\w+\s*\])?"                            # optional [abort] default
    r"\s*\?[ \t]*\Z",                                  # trailing '?' + EOL
    re.IGNORECASE,
)

# Interactive confirm prompt emitted by the NCM (ICOS/StrataX) nested CLI.
# Unlike DNOS' ``(yes/no)?`` (see :data:`_CONFIRM_RE`), the NCM phrases its
# confirm as a ``[y/n]`` / ``[yes/no]`` choice followed by a ``:`` (it waits
# for input right after the colon, no newline), e.g.::
#
#   copy running-config startup-config
#   This operation will modify your startup configuration.
#   Do you want to continue? [y/n]:
#
# Without an answer the channel never returns to the ``#`` prompt and the
# read times out even though the command itself is correct. We accept a
# trailing ``:`` *or* ``?`` so both the NCM colon form and any DNOS-style
# ``?`` form are caught, anchored at end-of-buffer.
_NCM_CONFIRM_RE = re.compile(
    r"[\(\[]\s*(?:yes\s*/\s*no|y\s*/\s*n)\s*[\)\]]"   # [y/n] | (yes/no)
    r"\s*[:?]?[ \t]*\Z",                              # trailing ':' or '?'
    re.IGNORECASE,
)

# Progress-bar frame: the redraws DNOS emits during ``request file upload`` /
# ``request file download``. After :func:`strip_ansi` turns ``\\r`` into
# ``\\n`` we get one line per frame — noisy in transcripts and envelopes, so
# we collapse runs of these frames down to the final one.
_PROGRESS_RE = re.compile(r"^\s*(?:\[[=> ]*\]\s*)?\S.*\b\d+%[\s\S]*$")


def collapse_progress(lines: list[str]) -> list[str]:
    """Keep only the last frame of each consecutive run of progress-bar lines.

    Leaves everything else untouched. A "progress line" is anything ending in
    ``NN%`` — strict enough that ordinary output (show output, error lines,
    ...) is never touched.
    """
    out: list[str] = []
    run_end: Optional[str] = None
    for line in lines:
        if _PROGRESS_RE.match(line):
            run_end = line
            continue
        if run_end is not None:
            out.append(run_end)
            run_end = None
        out.append(line)
    if run_end is not None:
        out.append(run_end)
    return out


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences; treat a lone ``\\r`` as a line break.

    DNOS streams progress bars for ``request file upload`` /
    ``request file download`` as ``[===>] 1%\\r[===>] 2%\\r...`` — every
    ``\\r`` rewinds the cursor to column 0 so the user sees the bar update
    in place. If we just dropped ``\\r`` the whole burst would collapse
    into a single physical line and the next prompt that DNOS prints on a
    fresh line would get glued onto the end of it, defeating
    :func:`ends_with_prompt`. By turning a bare ``\\r`` into a newline we
    preserve the line-based layout the prompt matcher assumes: each
    progress frame becomes its own line and the tail prompt stays clean.
    """
    cleaned = _ANSI_RE.sub("", text)
    # ``\\r\\n`` is a normal CRLF line ending — collapse to ``\\n`` first so we
    # don't emit duplicate blank lines. Then every remaining ``\\r`` is a
    # standalone cursor reset from a progress-style redraw; treat it as a line
    # break of its own.
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return cleaned


def detect_prompt(text: str, dialect: Optional["Dialect"] = None) -> Optional[str]:
    """Return the last prompt-looking token at the very end of ``text``.

    For DNOS we normalise the parenthesised timestamp/context out, so the
    prompt we store is the stable ``HOSTNAME#`` form. That way matching on
    subsequent output (where timestamp differs) still works. ``dialect``
    (when given) supplies a vendor's own prompt regex / normalisation;
    omitted, the DNOS defaults apply unchanged.
    """
    prompt_re = dialect.prompt_re if dialect is not None else _PROMPT_RE
    strip_paren = dialect.strip_paren_context if dialect is not None else True
    clean = strip_ansi(text)
    tail = clean[-4096:]
    m = None
    for m in prompt_re.finditer(tail):
        pass
    if not m:
        return None
    raw = m.group("prompt")
    return _normalise_prompt(raw, strip_paren=strip_paren)


def _normalise_prompt(raw: str, strip_paren: bool = True) -> str:
    """Drop a trailing ``(...)`` block from a ``HOST(...)#`` / ``HOST(...)>`` prompt."""
    if not strip_paren:
        return raw
    for suffix in (")#", ")>"):
        if raw.endswith(suffix):
            open_idx = raw.rfind("(")
            if open_idx > 0:
                return raw[:open_idx] + suffix[-1]
    return raw


def ends_with_prompt(
    text: str, prompt: str, dialect: Optional["Dialect"] = None
) -> bool:
    """True if ``text`` ends in a prompt whose hostname matches ``prompt``."""
    tail_re = dialect.tail_re if dialect is not None else _TAIL_PROMPT_RE
    strip_paren = dialect.strip_paren_context if dialect is not None else True
    clean = strip_ansi(text).rstrip()
    if not clean:
        return False
    last = clean.rsplit("\n", 1)[-1]
    if not tail_re.match(last):
        return False
    return _normalise_prompt(last.rstrip(), strip_paren=strip_paren) == prompt


def drain(
    channel: paramiko.Channel,
    max_wait: float = 2.0,
    stop_on_prompt: bool = False,
    dialect: Optional["Dialect"] = None,
) -> str:
    """Read whatever the channel offers until it goes idle.

    If ``stop_on_prompt`` is set, return as soon as the accumulated buffer
    ends in a prompt for ``dialect`` (DNOS-shaped when omitted) — lets the
    banner drain bail out in ~100-300 ms on a responsive device instead of
    waiting the full window.
    """
    chunks = []
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if channel.recv_ready():
            chunks.append(channel.recv(65535).decode("utf-8", errors="replace"))
            deadline = time.time() + 0.5
            if stop_on_prompt and detect_prompt("".join(chunks), dialect=dialect):
                return "".join(chunks)
            continue
        time.sleep(0.05)
    return "".join(chunks)


def read_until_prompt(
    channel: paramiko.Channel,
    prompt: str,
    overall_timeout: float = 30.0,
    idle_timeout: float = 1.0,
    dialect: Optional["Dialect"] = None,
) -> Tuple[str, bool]:
    """Read from channel until ``prompt`` appears at end of output, or timeout.

    Returns (raw_text, hit_prompt). ``hit_prompt=False`` means timeout tripped.
    """
    buf = []
    text = ""
    start = time.time()
    last_rx = start
    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            last_rx = time.time()
            if ends_with_prompt(text, prompt, dialect=dialect):
                return text, True
        else:
            now = time.time()
            if now - start > overall_timeout:
                return text, False
            if now - last_rx > idle_timeout and buf:
                if ends_with_prompt(text, prompt, dialect=dialect):
                    return text, True
            time.sleep(0.05)


_PROMPT_ECHO_RE = re.compile(r"^[\w.\-]+(?:\([^)]*\))?#")


def send_help(
    channel: paramiko.Channel,
    path: str,
    prompt: str,
    overall_timeout: float = 10.0,
) -> Tuple[str, str, str, bool]:
    """Send ``<path> ?`` WITHOUT a trailing newline and collect the help block.

    DNOS treats ``?`` as an inline context-help trigger: it prints the
    accepted children for the current prefix but leaves the prefix
    buffered, waiting for more keystrokes. Because we never submit a
    newline, the base command is never executed — this is the only safe
    way to enumerate children of leaf-complete commands such as
    ``request system restart`` (which would otherwise open a real reboot
    dialog).

    After the help block prints we send ``Ctrl-U`` (``\\x15``, kill-line)
    followed by a newline to clear the buffered prefix and force DNOS to
    repaint a fresh prompt, which we then wait for.

    Returns the same ``(clean_output, head_prompt_line, tail_prompt,
    hit_prompt)`` tuple shape as :func:`send_command`.
    """
    prefix = (path or "").strip().rstrip("?").strip()
    trigger = (f"{prefix} ?" if prefix else "?")

    channel.send(trigger)
    help_block = drain(channel, max_wait=max(overall_timeout / 2, 2.0))

    channel.send("\x15\n")
    raw_tail, hit_prompt = read_until_prompt(
        channel, prompt, overall_timeout=overall_timeout,
    )

    cleaned = strip_ansi(help_block + raw_tail).replace("\x07", "")
    lines = cleaned.splitlines()

    head_prompt_line = ""
    if prefix and lines and prefix in lines[0] and "#" not in lines[0]:
        head_prompt_line = lines[0].rstrip()
        lines = lines[1:]

    filtered: list[str] = []
    for ln in lines:
        s = ln.rstrip()
        if _PROMPT_ECHO_RE.match(s):
            continue
        filtered.append(ln)

    while filtered and filtered[-1].strip() == "":
        filtered.pop()

    output = "\n".join(filtered)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, head_prompt_line, "", hit_prompt


def send_config_help(
    channel: paramiko.Channel,
    path: str,
    prompt: str,
    overall_timeout: float = 10.0,
) -> Tuple[str, str, str, bool]:
    """Enumerate children of a CONFIGURE-mode path via ``?`` context help.

    Same safety model as :func:`send_help` — the ``?`` trigger is sent
    without a newline so DNOS only prints children and never executes the
    base command — but the channel is first put into ``configure`` mode so
    the enumerated tree is the configuration grammar (``set`` / ``delete``
    / ``protocols`` / ...) rather than the operational one.

    Flow on one channel:

        1. send ``configure`` and wait for the config prompt. DNOS renders
           it as ``HOST(cf)#``; :func:`_normalise_prompt` strips the
           parenthesised segment so ``ends_with_prompt`` keeps matching the
           same stable ``HOST#`` token used in operational mode.
        2. send ``<path> ?`` (no newline), drain the help block.
        3. send Ctrl-U (``\\x15``) + newline to wipe the buffered prefix.
        4. send ``end`` and wait for the operational prompt, so the
           candidate is never touched and the channel is left on firm
           ground before teardown.

    Returns the same ``(clean_output, head_prompt_line, tail_prompt,
    hit_prompt)`` tuple shape as :func:`send_help`. ``hit_prompt`` tracks
    the post-``?`` prompt wait (step 3); the ``end`` wait in step 4 is
    best-effort, mirroring :func:`send_shell_exec`'s exit-back behaviour.
    """
    channel.send("configure\n")
    _, hit_cfg = read_until_prompt(
        channel, prompt, overall_timeout=overall_timeout,
    )
    if not hit_cfg:
        try:
            channel.send("end\n")
            read_until_prompt(
                channel, prompt, overall_timeout=overall_timeout,
            )
        except Exception:
            pass
        return "", "", "", False

    prefix = (path or "").strip().rstrip("?").strip()
    trigger = (f"{prefix} ?" if prefix else "?")

    channel.send(trigger)
    help_block = drain(channel, max_wait=max(overall_timeout / 2, 2.0))

    channel.send("\x15\n")
    raw_tail, hit_prompt = read_until_prompt(
        channel, prompt, overall_timeout=overall_timeout,
    )

    try:
        channel.send("end\n")
        read_until_prompt(
            channel, prompt, overall_timeout=overall_timeout,
        )
    except Exception:
        pass

    cleaned = strip_ansi(help_block + raw_tail).replace("\x07", "")
    lines = cleaned.splitlines()

    head_prompt_line = ""
    if prefix and lines and prefix in lines[0] and "#" not in lines[0]:
        head_prompt_line = lines[0].rstrip()
        lines = lines[1:]

    filtered: list[str] = []
    for ln in lines:
        s = ln.rstrip()
        if _PROMPT_ECHO_RE.match(s):
            continue
        filtered.append(ln)

    while filtered and filtered[-1].strip() == "":
        filtered.pop()

    output = "\n".join(filtered)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, head_prompt_line, "", hit_prompt


def send_probe(
    channel: paramiko.Channel,
    prefix: str,
    key: str,
    prompt: str,
    overall_timeout: float = 10.0,
) -> Tuple[str, str, bool]:
    """Send ``prefix`` WITHOUT a newline, inject one keystroke, harvest output.

    The primitive behind interactive CLI-discoverability probes: type a
    command-line prefix exactly as given (no strip — a trailing space is
    the difference between "list children of ``bfd``" and "act on the
    partial token ``str``"), then inject a single keystroke:

    - ``key="?"``: DNOS prints the context-help block for the buffered
      prefix and repaints the line.
    - ``key="tab"``: DNOS completes the partial last token in place (the
      completion characters are echoed onto the line) or, when ambiguous,
      lists the candidates.

    No newline is ever sent for the probe itself, so the buffered line is
    never submitted; afterwards ``Ctrl-U`` (``\\x15``, kill-line) plus a
    newline wipe the buffer and force a fresh prompt, which we wait for —
    the same safety model as :func:`send_help`.

    Returns ``(clean_output, line_buffer, hit_prompt)``. ``clean_output``
    is the harvested block (help text / completion candidates) with the
    prefix echo and prompt repaints filtered out. ``line_buffer`` is the
    CLI's line buffer after the keystroke: for ``tab`` it is reconstructed
    from the echo (prefix + completion), for ``?`` it is ``prefix``
    unchanged (``?`` never edits the line).
    """
    keystroke = "\t" if key == "tab" else "?"
    prefix = prefix or ""
    if prefix:
        channel.send(prefix)
    channel.send(keystroke)
    block = drain(channel, max_wait=max(overall_timeout / 2, 2.0))

    channel.send("\x15\n")
    raw_tail, hit_prompt = read_until_prompt(
        channel, prompt, overall_timeout=overall_timeout,
    )

    # DNOS pads the PTY stream with NUL bursts around keystroke echoes —
    # drop them along with bell characters before any line parsing.
    cleaned_block = strip_ansi(block).replace("\x07", "").replace("\x00", "")

    if keystroke == "\t":
        # The echo stream is literally what the line buffer became: the
        # typed prefix followed by whatever completion DNOS appended. On
        # an ambiguous tab DNOS lists candidates and repaints
        # ``PROMPT# <prefix>`` — strip the prompt, keep the buffer.
        last = ""
        for ln in cleaned_block.splitlines():
            if ln.strip():
                last = ln
        line_buffer = _PROMPT_ECHO_RE.sub("", last).lstrip()
    else:
        line_buffer = prefix

    cleaned = cleaned_block + strip_ansi(raw_tail).replace("\x07", "").replace(
        "\x00", ""
    )
    lines = cleaned.splitlines()

    stripped_prefix = prefix.strip()
    if stripped_prefix and lines and stripped_prefix in lines[0] and "#" not in lines[0]:
        lines = lines[1:]

    filtered: list[str] = []
    for ln in lines:
        if _PROMPT_ECHO_RE.match(ln.rstrip()):
            continue
        filtered.append(ln)

    while filtered and filtered[-1].strip() == "":
        filtered.pop()

    output = "\n".join(filtered)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, line_buffer, hit_prompt


def ends_with_shell_prompt(text: str) -> bool:
    """True if ``text`` ends with a Linux shell prompt from ``run start shell``."""
    clean = strip_ansi(text).rstrip()
    if not clean:
        return False
    last = clean.rsplit("\n", 1)[-1]
    return bool(_SHELL_TAIL_RE.search(last))


def read_until_password(
    channel: paramiko.Channel,
    overall_timeout: float = 10.0,
) -> Tuple[str, bool]:
    """Read until a ``Password:`` prompt appears at end of buffer, or timeout."""
    buf: list[str] = []
    text = ""
    start = time.time()
    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = strip_ansi("".join(buf))
            if _PASSWORD_RE.search(text[-256:]):
                return text, True
        else:
            if time.time() - start > overall_timeout:
                return text, False
            time.sleep(0.05)


def read_until_shell_prompt(
    channel: paramiko.Channel,
    overall_timeout: float = 30.0,
    idle_timeout: float = 1.0,
) -> Tuple[str, bool]:
    """Read from channel until a Linux shell prompt appears, or timeout."""
    buf: list[str] = []
    text = ""
    start = time.time()
    last_rx = start
    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            last_rx = time.time()
            if ends_with_shell_prompt(text):
                return text, True
        else:
            now = time.time()
            if now - start > overall_timeout:
                return text, False
            if now - last_rx > idle_timeout and buf:
                if ends_with_shell_prompt(text):
                    return text, True
            time.sleep(0.05)


def send_shell_exec(
    channel: paramiko.Channel,
    linux_cmd: str,
    password: str,
    dnos_prompt: str,
    overall_timeout: float = 30.0,
    shell_entry: str = "run start shell",
) -> Tuple[str, str, str, bool]:
    """Run a single Linux command inside a DNOS ``run start shell`` variant.

    Flow (all on one channel):

        1. send ``shell_entry`` (default ``run start shell``), wait for
           ``Password:``.
        2. send ``password``, wait for the Linux shell prompt.
        3. send ``linux_cmd``, wait for the Linux shell prompt; capture output.
        4. ALWAYS send ``exit`` and wait for the DNOS prompt — so the channel
           is left on firm ground even if step 3 errored or timed out.

    ``shell_entry`` lets the caller target a specific NCC / NCP / container
    (e.g. ``run start shell ncc 1``, ``run start shell ncp 0``,
    ``run start shell ncc active container netconf``). The password challenge
    and ``]#`` prompt shape are identical across all targets.

    Returns ``(clean_output, head_prompt_line, tail_prompt, hit_prompt)`` —
    same shape as :func:`send_command`. ``hit_prompt`` is the result of the
    *Linux* command wait (step 3); the exit-back-to-DNOS wait is best-effort.
    """
    def _exit_back() -> None:
        try:
            channel.send("exit\n")
            read_until_prompt(
                channel, dnos_prompt, overall_timeout=overall_timeout,
            )
        except Exception:
            pass

    channel.send(shell_entry + "\n")
    _, hit_pw = read_until_password(channel, overall_timeout=overall_timeout)
    if not hit_pw:
        _exit_back()
        return "", "", "", False

    channel.send(password + "\n")
    _, hit_shell = read_until_shell_prompt(
        channel, overall_timeout=overall_timeout,
    )
    if not hit_shell:
        _exit_back()
        return "", "", "", False

    channel.send(linux_cmd + "\n")
    raw, hit_cmd = read_until_shell_prompt(
        channel, overall_timeout=overall_timeout,
    )

    _exit_back()

    cleaned = strip_ansi(raw).replace("\x07", "")
    lines = cleaned.splitlines()

    # Drop leading echo of our command (shell echoes the typed line back).
    cmd_stripped = linux_cmd.strip()
    head_prompt_line = ""
    if cmd_stripped:
        while lines and cmd_stripped in lines[0]:
            head_prompt_line = lines[0].rstrip()
            lines = lines[1:]

    # Trim trailing shell prompt (the one that closed the command).
    while lines and lines[-1].strip() == "":
        lines.pop()
    tail_prompt = ""
    if lines and _SHELL_TAIL_RE.search(lines[-1].rstrip()):
        tail_prompt = lines.pop().rstrip()
    while lines and lines[-1].strip() == "":
        lines.pop()

    output = "\n".join(lines)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, head_prompt_line, tail_prompt, hit_cmd


def ends_with_ncm_prompt(text: str) -> bool:
    """True if ``text`` ends with an NCM (ICOS-style) nested-CLI prompt.

    Matches the exec / config / interface-config prompt shapes the NCM
    switch renders inside ``run start shell ncm <id>`` (see
    :data:`_NCM_PROMPT_RE`). Shape-only, so it keeps matching as the
    prompt mutates between modes.
    """
    clean = strip_ansi(text).rstrip()
    if not clean:
        return False
    last = clean.rsplit("\n", 1)[-1]
    return bool(_NCM_TAIL_RE.search(last.rstrip()))


def read_until_ncm_prompt(
    channel: paramiko.Channel,
    overall_timeout: float = 30.0,
    idle_timeout: float = 1.0,
) -> Tuple[str, bool]:
    """Read from channel until an NCM nested-CLI prompt appears, or timeout."""
    buf: list[str] = []
    text = ""
    start = time.time()
    last_rx = start
    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            last_rx = time.time()
            if ends_with_ncm_prompt(text):
                return text, True
        else:
            now = time.time()
            if now - start > overall_timeout:
                return text, False
            if now - last_rx > idle_timeout and buf:
                if ends_with_ncm_prompt(text):
                    return text, True
            time.sleep(0.05)


def read_until_ncm_prompt_answering(
    channel: paramiko.Channel,
    answer: str = "y",
    overall_timeout: float = 30.0,
    idle_timeout: float = 1.0,
) -> Tuple[str, bool]:
    """Read until an NCM prompt appears, answering interactive confirms first.

    Same as :func:`read_until_ncm_prompt`, but watches the tail for an NCM
    ``[y/n]:`` / ``[yes/no]:`` confirmation (see :data:`_NCM_CONFIRM_RE`) and
    sends ``answer`` + newline when one shows up, instead of waiting for a
    ``#`` prompt that will never arrive until the prompt is answered. The
    device echoes the answer character, which pushes the choice token off the
    end of the buffer so the same confirm is never answered twice; a
    length-watermark guards against double-answering within a single chunk.
    A multi-confirm flow (a command that asks more than once) is handled by
    answering each new confirm as it appears.
    """
    buf: list[str] = []
    text = ""
    start = time.time()
    last_rx = start
    answered_at_len = 0
    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            last_rx = time.time()
            tail = strip_ansi(text)[-256:]
            if _NCM_CONFIRM_RE.search(tail) and len(text) > answered_at_len:
                channel.send(answer + "\n")
                answered_at_len = len(text)
                last_rx = time.time()
                continue
            if ends_with_ncm_prompt(text):
                return text, True
        else:
            now = time.time()
            if now - start > overall_timeout:
                return text, False
            if now - last_rx > idle_timeout and buf:
                if ends_with_ncm_prompt(text):
                    return text, True
            time.sleep(0.05)


def read_until_password_or_ncm_prompt(
    channel: paramiko.Channel,
    overall_timeout: float = 15.0,
    idle_timeout: float = 1.0,
) -> Tuple[str, str]:
    """Read until a ``Password:`` challenge OR an NCM prompt appears.

    Entering the NCM nested CLI via ``run start shell ncm <id>`` may or may
    not challenge for a password before landing at the switch prompt, so we
    watch for both and let the caller branch. Returns ``(text, kind)`` where
    ``kind`` is ``"password"``, ``"prompt"``, or ``"timeout"``.
    """
    buf: list[str] = []
    text = ""
    start = time.time()
    last_rx = start
    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            last_rx = time.time()
            if _PASSWORD_RE.search(strip_ansi(text)[-256:]):
                return text, "password"
            if ends_with_ncm_prompt(text):
                return text, "prompt"
        else:
            now = time.time()
            if now - start > overall_timeout:
                return text, "timeout"
            if now - last_rx > idle_timeout and buf:
                if ends_with_ncm_prompt(text):
                    return text, "prompt"
            time.sleep(0.05)


def _clean_ncm_segment(raw: str, command: str) -> Tuple[str, str]:
    """Strip the echoed command and trailing NCM prompt from one step's output.

    Returns ``(clean_output, tail_prompt)``.
    """
    cleaned = strip_ansi(raw).replace("\x07", "")
    lines = cleaned.splitlines()

    cmd_stripped = command.strip()
    if cmd_stripped:
        while lines and cmd_stripped in lines[0]:
            lines = lines[1:]

    while lines and lines[-1].strip() == "":
        lines.pop()
    tail_prompt = ""
    if lines and _NCM_TAIL_RE.search(lines[-1].rstrip()):
        tail_prompt = lines.pop().rstrip()
    while lines and lines[-1].strip() == "":
        lines.pop()

    return "\n".join(lines), tail_prompt


def send_ncm_cli(
    channel: paramiko.Channel,
    ncm_commands: list[str],
    password: str,
    dnos_prompt: str,
    shell_entry: str,
    overall_timeout: float = 30.0,
    answer: str = "y",
) -> Tuple[str, str, str, bool]:
    """Drive the NCM switch's nested (ICOS-style) CLI and return its transcript.

    Flow (all on one channel):

        1. send ``shell_entry`` (``run start shell ncm <id>``); wait for
           either a ``Password:`` challenge or the NCM prompt.
        2. if challenged, send ``password`` and wait for the NCM prompt.
        3. send each command in ``ncm_commands`` in order, capturing the
           output between NCM prompts. The prompt shape is tracked across
           ``configure`` / ``interface eth 0/X`` mode changes. A command
           that pauses on an interactive ``[y/n]:`` / ``[yes/no]:`` confirm
           (e.g. ``copy running-config startup-config``) is answered with
           ``answer`` so it can complete instead of timing out.
        4. ALWAYS try to back out: ``end`` (leave any config mode) then
           ``exit`` (leave the NCM CLI) until the DNOS prompt re-appears —
           so the channel is left on firm ground even on error / timeout.

    Returns ``(clean_output, head_prompt_line, tail_prompt, hit_prompt)`` —
    same shape as :func:`send_command`. ``clean_output`` is the combined
    transcript of every command (segments joined by a blank line);
    ``hit_prompt`` reflects whether the LAST command returned to a prompt.
    """
    def _exit_back() -> None:
        # Leave any config sub-mode, then exit the NCM CLI back to DNOS.
        try:
            channel.send("end\n")
            read_until_ncm_prompt(
                channel, overall_timeout=min(overall_timeout, 5.0),
            )
        except Exception:
            pass
        for _ in range(2):
            try:
                channel.send("exit\n")
                _, hit = read_until_prompt(
                    channel, dnos_prompt,
                    overall_timeout=min(overall_timeout, 5.0),
                )
                if hit:
                    return
            except Exception:
                return

    channel.send(shell_entry + "\n")
    _, kind = read_until_password_or_ncm_prompt(
        channel, overall_timeout=overall_timeout,
    )
    if kind == "password":
        channel.send(password + "\n")
        _, hit_entry = read_until_ncm_prompt(
            channel, overall_timeout=overall_timeout,
        )
        if not hit_entry:
            _exit_back()
            return "", "", "", False
    elif kind != "prompt":
        _exit_back()
        return "", "", "", False

    segments: list[str] = []
    tail_prompt = ""
    hit_cmd = True
    for cmd in ncm_commands:
        channel.send(cmd + "\n")
        raw, hit_cmd = read_until_ncm_prompt_answering(
            channel, answer=answer, overall_timeout=overall_timeout,
        )
        seg, seg_tail = _clean_ncm_segment(raw, cmd)
        if seg_tail:
            tail_prompt = seg_tail
        segments.append(seg)
        if not hit_cmd:
            break

    _exit_back()

    output = "\n\n".join(s for s in segments if s)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, "", tail_prompt, hit_cmd


def send_command_with_password(
    channel: paramiko.Channel,
    command: str,
    password: str,
    prompt: str,
    overall_timeout: float = 60.0,
    idle_timeout: float = 1.0,
) -> Tuple[str, str, str, bool]:
    """Send a DNOS command that may be interrupted by a ``Password:`` prompt.

    Used for ``request file upload`` / ``request file download`` — DNOS
    shells out to ``sftp``/``scp``, which asks for the remote account's
    password inline, then returns to the DNOS prompt once the transfer is
    done. Flow:

        1. send ``command + "\\n"``
        2. read until either ``Password:`` or the DNOS prompt re-appears
        3. on password: send ``password + "\\n"`` and keep reading until
           the DNOS prompt; on prompt-first (e.g. syntax error caught
           before sshd was even contacted) return straight away.

    Returns the same ``(clean_output, head_prompt_line, tail_prompt,
    hit_prompt)`` tuple shape as :func:`send_command`. The password itself
    is never echoed into ``clean_output``.
    """
    channel.send(command + "\n")

    buf: list[str] = []
    text = ""
    start = time.time()
    last_rx = start
    saw_password = False
    hit_prompt = False

    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            last_rx = time.time()

            if not saw_password:
                # Scan tail for a password prompt; if we see one, answer it
                # and flip the flag so we don't retrigger. The DNOS prompt
                # might *also* appear at the tail if the command failed
                # fast (bad syntax, no such path, ...), so we check both.
                tail = strip_ansi(text)[-256:]
                if _PASSWORD_RE.search(tail):
                    channel.send(password + "\n")
                    saw_password = True
                    last_rx = time.time()
                    continue
            if ends_with_prompt(text, prompt):
                hit_prompt = True
                break
        else:
            now = time.time()
            if now - start > overall_timeout:
                break
            if now - last_rx > idle_timeout and buf:
                if ends_with_prompt(text, prompt):
                    hit_prompt = True
                    break
            time.sleep(0.05)

    cleaned = strip_ansi(text)
    lines = cleaned.splitlines()

    cmd_stripped = command.strip()
    head_prompt_line = ""
    if cmd_stripped:
        while lines and cmd_stripped in lines[0]:
            head_prompt_line = lines[0].rstrip()
            lines = lines[1:]

    while lines and lines[-1].strip() == "":
        lines.pop()
    tail_prompt = ""
    if lines and _TAIL_PROMPT_RE.match(lines[-1].rstrip()):
        tail_prompt = lines.pop().rstrip()

    while lines and lines[-1].strip() == "":
        lines.pop()

    lines = collapse_progress(lines)
    output = "\n".join(lines)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, head_prompt_line, tail_prompt, hit_prompt


def send_command_with_confirm(
    channel: paramiko.Channel,
    command: str,
    prompt: str,
    overall_timeout: float = 60.0,
    idle_timeout: float = 1.0,
    answer: str = "yes",
) -> Tuple[str, str, str, bool]:
    """Send a DNOS command that may prompt with ``(yes/no)?`` mid-execution.

    Used on the GI (Genesis Image) shell, which doesn't accept
    ``set cli-no-confirm`` to suppress confirmation prompts the way DNOS
    does. ``request system target-stack load`` and friends pause with
    ``... (yes/no)?`` waiting for input; without an answer the channel
    hangs until the per-step timeout fires. Flow:

        1. send ``command + "\\n"``
        2. read until either ``(yes/no)?`` (any case / ``[y/n]?`` shape)
           or the DNOS prompt re-appears
        3. on confirm: send ``answer + "\\n"`` and keep reading until the
           DNOS prompt; on prompt-first (e.g. command rejected outright)
           return straight away.

    The same prompt may appear more than once for a single command (some
    multi-step ``request`` flows ask twice); we keep answering as long as
    new ``(yes/no)?`` tokens show up before the DNOS prompt.

    Returns the same ``(clean_output, head_prompt_line, tail_prompt,
    hit_prompt)`` tuple shape as :func:`send_command`. Mirrors
    :func:`send_command_with_password` except for the trigger regex and
    the supplied response.
    """
    channel.send(command + "\n")

    buf: list[str] = []
    text = ""
    start = time.time()
    last_rx = start
    answered_at_len = 0  # text length last time we answered, to avoid loops
    hit_prompt = False

    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            last_rx = time.time()

            tail = strip_ansi(text)[-256:]
            if (
                _CONFIRM_RE.search(tail)
                and len(text) > answered_at_len
            ):
                channel.send(answer + "\n")
                answered_at_len = len(text)
                last_rx = time.time()
                continue
            if ends_with_prompt(text, prompt):
                hit_prompt = True
                break
        else:
            now = time.time()
            if now - start > overall_timeout:
                break
            if now - last_rx > idle_timeout and buf:
                if ends_with_prompt(text, prompt):
                    hit_prompt = True
                    break
            time.sleep(0.05)

    cleaned = strip_ansi(text)
    lines = cleaned.splitlines()

    cmd_stripped = command.strip()
    head_prompt_line = ""
    if cmd_stripped:
        while lines and cmd_stripped in lines[0]:
            head_prompt_line = lines[0].rstrip()
            lines = lines[1:]

    while lines and lines[-1].strip() == "":
        lines.pop()
    tail_prompt = ""
    if lines and _TAIL_PROMPT_RE.match(lines[-1].rstrip()):
        tail_prompt = lines.pop().rstrip()

    while lines and lines[-1].strip() == "":
        lines.pop()

    lines = collapse_progress(lines)
    output = "\n".join(lines)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, head_prompt_line, tail_prompt, hit_prompt


def send_command_with_commit_conflict(
    channel: paramiko.Channel,
    command: str,
    prompt: str,
    overall_timeout: float = 60.0,
    idle_timeout: float = 1.0,
    answer: str = "abort",
) -> Tuple[str, str, str, bool]:
    """Send a DNOS command that may raise the live-``commit`` rebase prompt.

    When another session committed since this candidate's transaction
    started, DNOS interrupts a live ``commit`` with::

        What would you like to do (commit, merge-only, abort) [abort]?

    and waits for input (see :data:`_COMMIT_CONFLICT_RE`). Without an
    answer the channel hangs until the per-step timeout. We answer with
    ``answer`` — defaulting to ``abort``, the prompt's own default, so we
    never silently merge our candidate on top of someone else's change.
    The warning + question text is left in the returned output so the
    caller can classify the outcome as a commit-conflict.

    Returns the same ``(clean_output, head_prompt_line, tail_prompt,
    hit_prompt)`` tuple shape as :func:`send_command`. Mirrors
    :func:`send_command_with_confirm` except for the trigger regex and
    the supplied response.
    """
    channel.send(command + "\n")

    buf: list[str] = []
    text = ""
    start = time.time()
    last_rx = start
    answered_at_len = 0  # text length last time we answered, to avoid loops
    hit_prompt = False

    while True:
        if channel.recv_ready():
            chunk = channel.recv(65535).decode("utf-8", errors="replace")
            buf.append(chunk)
            text = "".join(buf)
            last_rx = time.time()

            tail = strip_ansi(text)[-256:]
            if (
                _COMMIT_CONFLICT_RE.search(tail)
                and len(text) > answered_at_len
            ):
                channel.send(answer + "\n")
                answered_at_len = len(text)
                last_rx = time.time()
                continue
            if ends_with_prompt(text, prompt):
                hit_prompt = True
                break
        else:
            now = time.time()
            if now - start > overall_timeout:
                break
            if now - last_rx > idle_timeout and buf:
                if ends_with_prompt(text, prompt):
                    hit_prompt = True
                    break
            time.sleep(0.05)

    cleaned = strip_ansi(text)
    lines = cleaned.splitlines()

    cmd_stripped = command.strip()
    head_prompt_line = ""
    if cmd_stripped:
        while lines and cmd_stripped in lines[0]:
            head_prompt_line = lines[0].rstrip()
            lines = lines[1:]

    while lines and lines[-1].strip() == "":
        lines.pop()
    tail_prompt = ""
    if lines and _TAIL_PROMPT_RE.match(lines[-1].rstrip()):
        tail_prompt = lines.pop().rstrip()

    while lines and lines[-1].strip() == "":
        lines.pop()

    lines = collapse_progress(lines)
    output = "\n".join(lines)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, head_prompt_line, tail_prompt, hit_prompt


def send_command(
    channel: paramiko.Channel,
    command: str,
    prompt: str,
    overall_timeout: float = 30.0,
    dialect: Optional["Dialect"] = None,
) -> Tuple[str, str, str, bool]:
    """Write ``command`` then collect output until prompt. Strips echo + prompt.

    Returns ``(clean_output, head_prompt_line, tail_prompt, hit_prompt)``:

    - ``clean_output``       — agent-facing stdout, free of echo and prompts.
    - ``head_prompt_line``   — the full rendered prompt+command line as DNOS
                               displayed it (e.g. ``HOST(20-Apr-2026-20:21:25)#
                               show system``). Empty if we could not capture
                               one. Intended for the transcript log only.
    - ``tail_prompt``        — trailing ``HOST(timestamp)#`` line. Empty on
                               timeout.

    ``dialect`` (when given) supplies the vendor's prompt regexes so the
    trailing-prompt trim and prompt detection match the device; omitted,
    the DNOS defaults apply unchanged.
    """
    tail_re = dialect.tail_re if dialect is not None else _TAIL_PROMPT_RE
    channel.send(command + "\n")
    raw, hit_prompt = read_until_prompt(
        channel, prompt, overall_timeout=overall_timeout, dialect=dialect,
    )
    cleaned = strip_ansi(raw)
    lines = cleaned.splitlines()

    # 1. Drop the leading echo lines. DNOS repaints the prompt+command line:
    #    it prints "HOST# <cmd>\n", then emits an ANSI cursor-up sequence and
    #    rewrites the line as "HOST(timestamp)# <cmd>\n". After ANSI strip we
    #    get two consecutive lines that both contain the full command. Keep
    #    the *last* such line as the head prompt (it's the one with the
    #    timestamp), and drop them all from the output.
    cmd_stripped = command.strip()
    head_prompt_line = ""
    if cmd_stripped:
        while lines and cmd_stripped in lines[0]:
            head_prompt_line = lines[0].rstrip()
            lines = lines[1:]

    # 2. Capture and drop a trailing prompt line (bare "HOST(...)#"). We keep
    #    it around so the transcript log can show it; the agent-facing output
    #    must not include it.
    while lines and lines[-1].strip() == "":
        lines.pop()
    tail_prompt = ""
    if lines and tail_re.match(lines[-1].rstrip()):
        tail_prompt = lines.pop().rstrip()

    # 3. Drop leftover blank lines introduced by the prompt removal.
    while lines and lines[-1].strip() == "":
        lines.pop()

    lines = collapse_progress(lines)
    output = "\n".join(lines)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, head_prompt_line, tail_prompt, hit_prompt
