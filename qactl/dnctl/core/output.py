"""Text-vs-``--json`` rendering and exit-code mapping.

Every subcommand calls one of the lifted tool functions, which all
return the *same envelope dict* the MCP tools returned. This module is
the single place that turns that dict into either:

* ``--json``: the **exact** payload, ``json.dumps``-ed losslessly, on
  stdout — so an agent can pipe straight to ``jq``. This is the most
  important property of the whole tool.
* default text: a readable, greppable rendering. The primary body
  (device ``stdout`` / ``result_xml`` / ``result``) goes to **stdout**;
  status, warnings, errors and next-actions go to **stderr** so stdout
  stays clean for piping.

Exit code is derived from the envelope ``status`` so ``&&`` chaining and
shell loops behave: ``ok`` → 0, ``warning`` → 0, ``error`` → 1,
``connect_error`` → 2, ``timeout`` → 3, any other non-ok → 1. ``ok`` and
``warning`` are the only zero-exit statuses (per the agent contract: "0
only on status ok/warning").
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict

# ``warning`` exits 0 — it's a successful result that merely carries
# advisory notes, so `&&` chaining must not treat it as a failure.
_EXIT = {"ok": 0, "warning": 0, "error": 1, "connect_error": 2, "timeout": 3}

# Envelope scaffolding keys handled explicitly by the text renderer; any
# other key is "tool-specific extra" and gets dumped as JSON in text mode.
_ENVELOPE_KEYS = {
    "status", "device", "host", "command", "stdout",
    "warnings", "errors", "next_actions", "result", "result_xml",
}


def exit_code_for(payload: Any) -> int:
    """Map an envelope to a process exit code."""
    if not isinstance(payload, dict):
        return 0
    status = payload.get("status")
    if status is None:
        return 0
    return _EXIT.get(status, 0 if status == "ok" else 1)


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def emit(payload: Any, *, as_json: bool) -> int:
    """Render ``payload`` to stdout/stderr and return the exit code."""
    if as_json:
        sys.stdout.write(_dump(payload) + "\n")
        return exit_code_for(payload)

    _render_text(payload)
    return exit_code_for(payload)


def _render_text(p: Any) -> None:
    if not isinstance(p, dict):
        sys.stdout.write(f"{p}\n")
        return

    # Header (stderr): status / device-or-host / command.
    bits = []
    if p.get("status"):
        bits.append(str(p["status"]))
    if p.get("device"):
        bits.append(f"device={p['device']}")
    elif p.get("host"):
        bits.append(f"host={p['host']}")
    if p.get("command"):
        bits.append(f"cmd={p['command']!r}")
    if bits:
        sys.stderr.write("# " + "  ".join(bits) + "\n")

    # Primary body (stdout): first string-ish field wins, else result.
    body = None
    for key in ("stdout", "result_xml", "output", "text"):
        v = p.get(key)
        if isinstance(v, str) and v:
            body = v
            break
    if body is not None:
        sys.stdout.write(body if body.endswith("\n") else body + "\n")
    elif p.get("result") is not None:
        sys.stdout.write(_dump(p["result"]) + "\n")
    else:
        extra: Dict[str, Any] = {k: v for k, v in p.items() if k not in _ENVELOPE_KEYS}
        if extra:
            sys.stdout.write(_dump(extra) + "\n")

    # Diagnostics (stderr).
    for w in p.get("warnings") or []:
        sys.stderr.write(f"warning: {w}\n")
    for e in p.get("errors") or []:
        sys.stderr.write(f"error: {e}\n")
    for n in p.get("next_actions") or []:
        sys.stderr.write(f"next: {n}\n")
