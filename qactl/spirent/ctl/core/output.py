"""Rendering, exit codes, and payload reading for ``qactl.spirent.ctl``.

Identical contract to ``qactl.ixia.ctl.core.output`` — two modes selected by
``--json``: a compact human rendering, or the exact ``result`` envelope
pretty-printed for ``jq``. Exit code is ``0`` on ``ok``/``warning``, non-zero
otherwise, so shell pipelines can branch on ``$?``.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional


_OK_STATUSES = {"ok", "warning"}


def exit_code_for(env: Dict[str, Any]) -> int:
    return 0 if env.get("status") in _OK_STATUSES else 1


def emit(env: Dict[str, Any], *, as_json: bool) -> int:
    """Print ``env`` in the selected format and return the exit code."""
    if as_json:
        print(json.dumps(env, indent=2, default=str, sort_keys=False))
    else:
        _print_text(env)
    return exit_code_for(env)


def _print_text(env: Dict[str, Any]) -> None:
    status = env.get("status", "?")
    kind = env.get("kind", "?")
    host = env.get("host")
    session = env.get("session")

    head = f"[{status}] {kind}"
    locus = []
    if host:
        locus.append(str(host))
    if session is not None:
        locus.append(f"session={session}")
    if locus:
        head += "  (" + " ".join(locus) + ")"
    stream = sys.stdout if status in _OK_STATUSES else sys.stderr
    print(head, file=stream)

    result = env.get("result")
    if result is not None:
        _print_result(result, stream)

    for w in env.get("warnings") or []:
        print(f"  ! {w}", file=sys.stderr)
    for e in env.get("errors") or []:
        print(f"  x {e}", file=sys.stderr)
    for n in env.get("next_actions") or []:
        print(f"  -> {n}", file=sys.stderr)


def _print_result(result: Any, stream) -> None:
    if isinstance(result, dict):
        list_key = None
        for k, v in result.items():
            if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
                list_key = k
                break
        scalars = {
            k: v for k, v in result.items()
            if not (isinstance(v, (list, dict)) and v)
        }
        for k, v in scalars.items():
            print(f"  {k}: {v}", file=stream)
        if list_key is not None:
            print(f"  {list_key}:", file=stream)
            _print_table(result[list_key], stream)
        for k, v in result.items():
            if k == list_key or k in scalars:
                continue
            print(f"  {k}:", file=stream)
            for line in json.dumps(v, indent=2, default=str).splitlines():
                print(f"    {line}", file=stream)
        return
    if isinstance(result, list) and result and all(
        isinstance(i, dict) for i in result
    ):
        _print_table(result, stream)
        return
    for line in json.dumps(result, indent=2, default=str).splitlines():
        print(f"  {line}", file=stream)


def _print_table(rows: List[Dict[str, Any]], stream) -> None:
    cols: List[str] = []
    for r in rows:
        for k in r.keys():
            if k not in cols:
                cols.append(k)

    def cell(v: Any) -> str:
        if isinstance(v, (dict, list)):
            return json.dumps(v, default=str)
        return "" if v is None else str(v)

    widths = {c: len(c) for c in cols}
    str_rows: List[Dict[str, str]] = []
    for r in rows:
        sr = {c: cell(r.get(c)) for c in cols}
        str_rows.append(sr)
        for c in cols:
            widths[c] = max(widths[c], len(sr[c]))

    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print("    " + header, file=stream)
    print("    " + "  ".join("-" * widths[c] for c in cols), file=stream)
    for sr in str_rows:
        print(
            "    " + "  ".join(sr[c].ljust(widths[c]) for c in cols),
            file=stream,
        )


def read_payload(value: Optional[str], file: Optional[str]) -> Optional[str]:
    if file:
        with open(file, "r", encoding="utf-8") as fh:
            return fh.read()
    if value == "-":
        return sys.stdin.read()
    return value


def parse_json_payload(
    value: Optional[str], file: Optional[str]
) -> Optional[Any]:
    raw = read_payload(value, file)
    if raw is None or raw.strip() == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"payload is not valid JSON: {e}") from e
