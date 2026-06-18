"""``run_shell`` tool — run arbitrary Linux command(s) via ``run start shell``.

General-purpose front end over the shell-exec path used internally by
``get_gitcommit`` and the log/trace tools. It takes one command or a
sequence of commands, joins them into a single shell line, runs them
inside DNOS' ``run start shell`` (optionally targeting a specific
NCC / NCP / container), and returns the combined stdout.

The shell is always left afterwards: :func:`dnctl.cli.core.shell.send_shell_exec`
sends ``exit`` back to the DNOS prompt once the command line completes,
even on error or timeout — so "one or a sequence of commands, then exit"
is the whole contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from dnctl.cli.core.envelope import error_response
from dnctl.cli.core.errors import RUN_SHELL_NEXT_ACTION
from dnctl.cli.core.session import DEFAULT_CMD_TIMEOUT, DEFAULT_PASSWORD, DEFAULT_USER
from dnctl.cli.core.shell_exec import _build_shell_entry, run_linux_on_device


def run_shell(
    commands: Union[str, List[str]],
    device: Optional[str] = None,
    host: Optional[str] = None,
    ncc: Optional[str] = None,
    ncp: Optional[str] = None,
    container: Optional[str] = None,
    continue_on_error: bool = False,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Run one Linux command, or a sequence, inside ``run start shell`` and exit.

    Enters ``run start shell`` (active NCC default container unless you
    target another context), runs the joined command line, captures its
    stdout, then exits back to the DNOS prompt.

    Multiple ``commands`` are chained into a single shell line:
      - default: joined with ``&&`` — the sequence stops at the first
        command that fails (non-zero exit).
      - ``continue_on_error=True``: joined with ``;`` — every command runs
        regardless of the previous one's exit status.

    Targeting (mirrors the ``run start shell`` grammar; ncc and ncp are
    mutually exclusive, and container has no meaning under ncp):
      - all unset → ``run start shell`` (active NCC, default container).
      - ncc       → ``run start shell ncc <0|1|active>``.
      - container → ``run start shell ncc <id|active> container <name>``.
      - ncp       → ``run start shell ncp <0-191|bfd-master>``.

    Args:
        commands: A single command string, or a list of command strings
            (each one a full command line).
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        ncc: Target NCC — '0', '1', or 'active'.
        ncp: Target NCP — '0'..'191' or 'bfd-master'.
        container: Target container name on the selected NCC.
        continue_on_error: Chain commands with ';' instead of '&&'.
        user: SSH username (default dnroot).
        password: SSH password (default dnroot); also answers the
            ``run start shell`` challenge.
        timeout: Per-command timeout seconds.

    Returns the standard envelope; ``stdout`` carries the combined output
    of the command line, and ``command`` is the exact joined line that ran.
    """
    if isinstance(commands, str):
        commands = [commands]
    cmds = [c.strip() for c in (commands or []) if c and c.strip()]
    if not cmds:
        return error_response(
            "Provide at least one non-empty shell command.",
            device=device, host=host, next_action=RUN_SHELL_NEXT_ACTION,
        )

    shell_entry, err = _build_shell_entry(ncc, ncp, container)
    if err:
        return error_response(
            err, device=device, host=host, next_action=RUN_SHELL_NEXT_ACTION,
        )

    separator = " ; " if continue_on_error else " && "
    linux_command = separator.join(cmds)

    return run_linux_on_device(
        "run_shell", device, host, user, password,
        linux_command, timeout, RUN_SHELL_NEXT_ACTION,
        shell_entry=shell_entry or "run start shell",
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(run_shell)
