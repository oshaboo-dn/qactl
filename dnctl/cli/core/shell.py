"""Interactive SSH shell IO for DriveNets DNOS CLI.

Connects via paramiko.invoke_shell(), tracks the DNOS prompt, and runs
commands by writing a line and reading until the prompt re-appears.
"""

from __future__ import annotations

import re
import time
from typing import Optional, Tuple

import paramiko


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


def detect_prompt(text: str) -> Optional[str]:
    """Return the last prompt-looking token at the very end of ``text``.

    For DNOS we normalise the parenthesised timestamp/context out, so the
    prompt we store is the stable ``HOSTNAME#`` form. That way matching on
    subsequent output (where timestamp differs) still works.
    """
    clean = strip_ansi(text)
    tail = clean[-4096:]
    m = None
    for m in _PROMPT_RE.finditer(tail):
        pass
    if not m:
        return None
    raw = m.group("prompt")
    return _normalise_prompt(raw)


def _normalise_prompt(raw: str) -> str:
    """Drop a trailing ``(...)`` block from a ``HOST(...)#`` prompt."""
    if raw.endswith(")#"):
        open_idx = raw.rfind("(")
        if open_idx > 0:
            return raw[:open_idx] + "#"
    return raw


def ends_with_prompt(text: str, prompt: str) -> bool:
    """True if ``text`` ends in a prompt whose hostname matches ``prompt``."""
    clean = strip_ansi(text).rstrip()
    if not clean:
        return False
    last = clean.rsplit("\n", 1)[-1]
    if not _TAIL_PROMPT_RE.match(last):
        return False
    return _normalise_prompt(last.rstrip()) == prompt


def drain(
    channel: paramiko.Channel,
    max_wait: float = 2.0,
    stop_on_prompt: bool = False,
) -> str:
    """Read whatever the channel offers until it goes idle.

    If ``stop_on_prompt`` is set, return as soon as the accumulated buffer
    ends in a DNOS-shaped prompt — lets the banner drain bail out in
    ~100-300 ms on a responsive device instead of waiting the full window.
    """
    chunks = []
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if channel.recv_ready():
            chunks.append(channel.recv(65535).decode("utf-8", errors="replace"))
            deadline = time.time() + 0.5
            if stop_on_prompt and detect_prompt("".join(chunks)):
                return "".join(chunks)
            continue
        time.sleep(0.05)
    return "".join(chunks)


def read_until_prompt(
    channel: paramiko.Channel,
    prompt: str,
    overall_timeout: float = 30.0,
    idle_timeout: float = 1.0,
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
            if ends_with_prompt(text, prompt):
                return text, True
        else:
            now = time.time()
            if now - start > overall_timeout:
                return text, False
            if now - last_rx > idle_timeout and buf:
                if ends_with_prompt(text, prompt):
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


def send_command(
    channel: paramiko.Channel,
    command: str,
    prompt: str,
    overall_timeout: float = 30.0,
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
    """
    channel.send(command + "\n")
    raw, hit_prompt = read_until_prompt(
        channel, prompt, overall_timeout=overall_timeout,
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
    if lines and _TAIL_PROMPT_RE.match(lines[-1].rstrip()):
        tail_prompt = lines.pop().rstrip()

    # 3. Drop leftover blank lines introduced by the prompt removal.
    while lines and lines[-1].strip() == "":
        lines.pop()

    lines = collapse_progress(lines)
    output = "\n".join(lines)
    if output and not output.endswith("\n"):
        output += "\n"
    return output, head_prompt_line, tail_prompt, hit_prompt
