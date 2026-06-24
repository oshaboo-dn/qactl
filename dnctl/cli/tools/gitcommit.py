"""``get_gitcommit`` MCP tool.

Reads ``/.gitcommit`` on the active NCC — DNOS writes the build identity
into that file at install time. Output is parsed into a sha + optional
PR number.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from dnctl.cli.core.errors import GET_GITCOMMIT_NEXT_ACTION
from dnctl.cli.core.session import DEFAULT_CMD_TIMEOUT, DEFAULT_PASSWORD, DEFAULT_USER
from dnctl.cli.core.shell_exec import run_linux_on_device
from dnctl.cli.vendors import CAP_LOGS, requires


_GITCOMMIT_RE = re.compile(r"^([0-9a-fA-F]{7,40})(?:-PR-(\d+))?$")


@requires(CAP_LOGS)
def get_gitcommit(
    device: Optional[str] = None,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Read ``/.gitcommit`` on the active NCC — the DNOS build identifier.

    Enters ``run start shell`` and runs ``cat /.gitcommit``. The file
    typically contains a single line of the form
    ``<commit_sha>-PR-<pr_number>`` (e.g.
    ``b669275319207358e3a196c1dd0c7a5f4b67116b-PR-86107``), but older /
    non-PR builds may have just the bare sha. Both shapes are parsed into
    the response.

    Args:
        device: Device alias (cl, sa, kira, slava-1, slava-2, ariel-cl).
        host: Raw hostname/IP (alternative to device).
        user: SSH username (default dnroot).
        password: SSH password (default dnroot); also used to authenticate
                  the ``run start shell`` challenge.
        timeout: Per-step timeout seconds.

    Response adds (when the file exists and parses):
        - ``gitcommit``: raw contents, trimmed.
        - ``commit_sha``: the hex sha prefix.
        - ``pr_number``: integer PR number, if present.
    """
    response = run_linux_on_device(
        "get_gitcommit", device, host, user, password,
        "cat /.gitcommit", timeout, GET_GITCOMMIT_NEXT_ACTION,
    )
    raw = (response.get("stdout") or "").strip()
    if raw:
        response["gitcommit"] = raw
        m = _GITCOMMIT_RE.match(raw)
        if m:
            response["commit_sha"] = m.group(1)
            if m.group(2) is not None:
                response["pr_number"] = int(m.group(2))
    # `cat /.gitcommit` of a missing/empty file (e.g. "No such file or
    # directory") leaves status "ok" with no parseable sha — that's a
    # failed read, not a build with no commit id. Surface it.
    if response.get("status") == "ok" and "commit_sha" not in response:
        response["status"] = "error"
        response.setdefault("errors", []).append(
            "could not read a commit id from /.gitcommit "
            f"(got {raw[:200]!r}); the file may be missing or the DNOS "
            "image layout may have shifted."
        )
        response.setdefault("next_actions", []).append(GET_GITCOMMIT_NEXT_ACTION)
    return response


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(get_gitcommit)
