"""Session diagnostics for ``qactl spirent`` — the scaffold's read surface.

connect (reattach probe) / sessions (list) / describe (one-call snapshot).
Each returns the standard envelope; ``stcrestclient`` is only imported when
one of these actually runs, so parser/help/offline tests stay dependency-free.
"""

from __future__ import annotations

from typing import Any, Dict

from qactl.spirent.client import SpirentConnectionError, SpirentSession, full_session_name
from qactl.spirent.core import session as session_mod
from qactl.spirent.core.envelope import make_envelope


def _fail(env: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
    env["status"] = "error"
    env["errors"].append(str(exc))
    if isinstance(exc, SpirentConnectionError):
        env["next_actions"].append(
            "Check $SPIRENT_HOST / --host and that the STC REST server is up; "
            "`pip install qactl[spirent]` if stcrestclient is missing."
        )
    return env


def spirent_connect_check(host: str, port: int, user: str) -> Dict[str, Any]:
    """Cheap reachability + reattach probe; reports whether the session existed."""
    name = session_mod.current_policy().get("session_name") \
        or session_mod.default_session_name()
    env = make_envelope(
        kind="spirent_connect", host=host, port=port,
        session=full_session_name(name, user),
        request={"host": host, "port": port, "user": user},
    )
    try:
        sess = session_mod.get_session(host, port, user)
        env["result"] = {
            "session": sess.full_name,
            "joined_existing": sess.joined_existing,
            "reachable": True,
        }
    except Exception as exc:
        return _fail(env, exc)
    return env


def spirent_list_sessions(host: str, port: int, user: str) -> Dict[str, Any]:
    """List the STC REST server's sessions — no join required."""
    env = make_envelope(
        kind="spirent_sessions", host=host, port=port,
        request={"host": host, "port": port, "user": user},
    )
    try:
        probe = SpirentSession(
            host, port, user, session_name=session_mod.default_session_name(),
        )
        names = probe.list_sessions()
        env["result"] = {
            "count": len(names),
            "sessions": [{"session": n} for n in names],
        }
    except Exception as exc:
        return _fail(env, exc)
    return env


def spirent_describe_session(host: str, port: int, user: str) -> Dict[str, Any]:
    """One-call snapshot: connect (reattach) + server/system/BLL info."""
    env = make_envelope(
        kind="spirent_describe", host=host, port=port,
        request={"host": host, "port": port, "user": user},
    )
    try:
        sess = session_mod.get_session(host, port, user)
        result: Dict[str, Any] = {
            "session": sess.full_name,
            "joined_existing": sess.joined_existing,
        }
        for label, fn in (
            ("server_info", sess.server_info),
            ("system_info", sess.system_info),
            ("bll_version", sess.bll_version),
        ):
            try:
                result[label] = fn()
            except Exception as exc:  # best-effort — one probe failing is a warning
                env["warnings"].append(f"{label} unavailable: {exc}")
        env["session"] = sess.full_name
        env["result"] = result
    except Exception as exc:
        return _fail(env, exc)
    return env
