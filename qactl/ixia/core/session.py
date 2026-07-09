"""IxNetwork session opener for qactl.ixia.ctl — reattach-first, CLI-shaped.

This is the single most important difference from the ixia-mcp server it
was lifted from. The MCP is a long-lived process: it caches one
``IxiaSession`` per ``(host, port, user)`` and reuses it across every
tool call for the lifetime of the daemon. ``qactl.ixia.ctl`` is the opposite —
**process-per-invocation**. Every ``qactl.ixia.ctl ...`` command starts cold.

If each invocation blindly opened a brand-new IxNetwork session, it
would strand (or, with ``ClearConfig``, wipe) the config / traffic /
protocol state the previous command set up. So the default behaviour
here is **reattach**:

- ``--session <id>`` pins an explicit IxNetwork session id.
- ``--new-session`` forces a fresh session (Linux API servers create a
  new one; Windows API servers expose a single shared session — see the
  caveat in :func:`_open`).
- otherwise: connect and, on a multi-session server, re-target the
  most-recent existing session rather than leaving a duplicate behind.

The CLI front-end records the user's choice once via
:func:`set_session_policy`; the vendored ``qactl.ixia.tools.*`` modules call
:func:`get_session` exactly as they did under the MCP, so they need no
changes. The per-process cache is still useful: a single command (e.g.
``proto start-all``) calls ``get_session`` several times (apply-changes,
then start); caching keeps that to one attach.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from qactl.ixia.client import IxiaSession
from qactl.ixia.client.models import IxiaConnectionError


DEFAULT_PORT = 11009
DEFAULT_USER = "dn"

# IxNetwork stat views (`StatViewAssistant`) block up to 180 s waiting for
# the view to "exist" when there's no data yet. That default is wrong for
# a CLI; tool-layer wrappers enforce their own bounded timeout on top.
STAT_VIEW_WAIT_SECONDS = 10

_Key = Tuple[str, int, str]

_SESSIONS: Dict[_Key, IxiaSession] = {}
_LOCKS: Dict[_Key, threading.RLock] = {}
_CACHE_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# Session policy — set once per CLI invocation by the front-end.
# --------------------------------------------------------------------------

_POLICY: Dict[str, Any] = {
    "session_id": None,
    "new_session": False,
    "password": None,
    "api_key": None,
}


def set_session_policy(
    *,
    session_id: Optional[int] = None,
    new_session: bool = False,
    password: Optional[str] = None,
    api_key: Optional[str] = None,
) -> None:
    """Record the reattach + credential choice for this process.

    Called by the CLI global-options callback before any subcommand runs.
    ``session_id`` and ``new_session`` are mutually exclusive at the CLI
    layer; if both arrive here, ``session_id`` wins. ``password`` /
    ``api_key`` are optional — no-auth Windows API servers ignore them.
    """
    _POLICY["session_id"] = session_id
    _POLICY["new_session"] = bool(new_session)
    _POLICY["password"] = password
    _POLICY["api_key"] = api_key


def current_policy() -> Dict[str, Any]:
    return dict(_POLICY)


def _key(host: str, port: int, user: str) -> _Key:
    return (host, int(port), user or "")


def _new_handle(
    host: str, port: int, user: str, *, session_name: Optional[str] = None
) -> IxiaSession:
    """Build an ``IxiaSession`` carrying this process's credential policy.

    Centralises credential injection so every construction path (pinned
    attach, forced-new, default connect, retarget) authenticates the same
    way. Credentials stay empty strings when unset, so the no-auth Windows
    path is byte-for-byte unchanged.
    """
    return IxiaSession(
        host=host, port=port, user=user,
        password=_POLICY.get("password") or "",
        api_key=_POLICY.get("api_key") or "",
        session_name=session_name,
    )


def get_session(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> IxiaSession:
    """Return a connected ``IxiaSession`` for ``(host, port, user)``.

    Honours the process session policy (:func:`set_session_policy`):
    attach to a pinned id, force a new session, or reattach to the
    most-recent existing one. Cached for the rest of this process so a
    single command's repeated calls share one attach.

    Raises ``IxiaConnectionError`` on failure.
    """
    k = _key(host, port, user)
    with _CACHE_LOCK:
        s = _SESSIONS.get(k)
        if s is not None and s.connected:
            return s
        s = _open(
            host, port, user,
            session_id=_POLICY.get("session_id"),
            new_session=bool(_POLICY.get("new_session")),
        )
        _SESSIONS[k] = s
        _LOCKS.setdefault(k, threading.RLock())
        return s


def _open(
    host: str,
    port: int,
    user: str,
    *,
    session_id: Optional[int],
    new_session: bool,
) -> IxiaSession:
    """Open an IxiaSession according to the reattach rules.

    Caveat on Windows: a Windows IxNetwork API server hosts a single
    shared session (id is conventionally 1) — it is the same binary as
    the GUI and cannot host a second concurrent session. There
    ``--new-session`` cannot literally fork a new session; it attaches
    to the shared one. On Linux / container API servers each
    ``SessionAssistant`` with a fresh name creates a real new session.
    """
    s = _new_handle(host, port, user)
    try:
        if session_id is not None:
            s.attach(session_id=int(session_id))
            return s
        if new_session:
            import time as _t
            fresh = _new_handle(
                host, port, user,
                session_name=f"qactl.ixia.ctl-{int(_t.time())}",
            )
            fresh.connect()
            return fresh
        # Default path: connect (attaches to the existing session on a
        # Windows API server). On a multi-session server, re-target the
        # most-recent existing session so we don't operate on a stale or
        # empty one.
        s.connect()
        _retarget_most_recent(s, host, port, user)
        return _SESSIONS.get(_key(host, port, user), s) or s
    except IxiaConnectionError:
        raise
    except Exception as e:  # pragma: no cover - passthrough
        raise IxiaConnectionError(
            f"Connect to {host}:{port} as user={user!r} failed: {e}"
        ) from e


def _retarget_most_recent(
    s: IxiaSession, host: str, port: int, user: str
) -> None:
    """If a newer session than the one we attached to exists, reattach.

    No-op on a single-session (Windows) server. On a Linux server where
    several sessions can coexist, this picks the highest id among the
    ACTIVE ones so consecutive ``qactl.ixia.ctl`` calls converge on the same
    "current" session instead of fanning out.
    """
    try:
        sessions = s.config.sessions()
    except Exception:
        return
    if not sessions:
        return
    cur = session_id_of(s)
    candidates: List[Dict[str, Any]] = [
        x for x in sessions
        if str(x.get("state", "")).upper() in ("ACTIVE", "IN USE", "INUSE")
    ] or sessions
    ids = [x.get("id") for x in candidates if isinstance(x.get("id"), int)]
    if not ids:
        return
    most_recent = max(ids)
    if cur is not None and most_recent == cur:
        return
    # Re-target: drop the current attach, attach to the most recent.
    try:
        s.disconnect()
    except Exception:
        pass
    fresh = _new_handle(host, port, user)
    fresh.attach(session_id=int(most_recent))
    # Replace whatever the caller cached under this key.
    _SESSIONS[_key(host, port, user)] = fresh


def write_lock(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> threading.RLock:
    """Per-session write lock. Context-manager this around any tool that
    PATCHes/POSTs to IxNetwork. Within a single CLI invocation this
    mostly guards the start/apply tools that call into IxNetwork twice.
    """
    k = _key(host, port, user)
    with _CACHE_LOCK:
        return _LOCKS.setdefault(k, threading.RLock())


def drop_session(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> None:
    """Evict the cached session and best-effort disconnect."""
    k = _key(host, port, user)
    with _CACHE_LOCK:
        s = _SESSIONS.pop(k, None)
    if s is None:
        return
    try:
        s.disconnect()
    except Exception:
        pass


def session_id_of(s: IxiaSession) -> Optional[int]:
    """Best-effort: read the attached IxNetwork session id from the handle."""
    try:
        href = s.ixn.href  # /api/v1/sessions/<id>/ixnetwork/
        for part in href.strip("/").split("/"):
            if part.isdigit():
                return int(part)
    except Exception:
        return None
    return None
