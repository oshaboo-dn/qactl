"""Process-level session policy + cache for ``qactl spirent``.

The same shape as ``qactl.ixia.core.session``: a CLI invocation records its
reattach choice once (:func:`set_session_policy`), and the tool layer calls
:func:`get_session` to obtain a connected :class:`SpirentSession` — reattaching
to the named STC session by default, forcing a fresh one with ``--new-session``,
or pinning an explicit ``--session <name>``.

The per-process cache keys on ``(host, port, user)`` so a single command that
touches the session twice attaches only once.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional, Tuple

from qactl.spirent.client import SpirentSession


# STC REST server defaults. Port 80 is the ``stcrestclient`` default; the
# per-install target lives in $SPIRENT_HOST (see the CLI global options).
DEFAULT_PORT = 80
DEFAULT_USER = "dn"
DEFAULT_SESSION_NAME = "qactl-session"

_Key = Tuple[str, int, str]

_SESSIONS: Dict[_Key, SpirentSession] = {}
_CACHE_LOCK = threading.RLock()

_POLICY: Dict[str, Any] = {
    "session_name": None,
    "new_session": False,
    "password": None,
    "timeout": None,
}


def default_session_name() -> str:
    return os.environ.get("SPIRENT_SESSION") or DEFAULT_SESSION_NAME


def set_session_policy(
    *,
    session_name: Optional[str] = None,
    new_session: bool = False,
    password: Optional[str] = None,
    timeout: Optional[int] = None,
) -> None:
    """Record the reattach + credential choice for this process."""
    _POLICY["session_name"] = session_name
    _POLICY["new_session"] = bool(new_session)
    _POLICY["password"] = password
    _POLICY["timeout"] = timeout


def current_policy() -> Dict[str, Any]:
    return dict(_POLICY)


def _key(host: str, port: int, user: str) -> _Key:
    return (host, int(port), user or "")


def get_session(
    host: str,
    port: int = DEFAULT_PORT,
    user: str = DEFAULT_USER,
) -> SpirentSession:
    """Return a connected :class:`SpirentSession` honouring the process policy.

    Cached for the rest of this process so repeated ``get_session`` calls in
    one command share a single attach.
    """
    key = _key(host, port, user)
    with _CACHE_LOCK:
        sess = _SESSIONS.get(key)
        if sess is not None:
            return sess
        name = _POLICY.get("session_name") or default_session_name()
        sess = SpirentSession(
            host, port, user,
            session_name=name,
            new_session=bool(_POLICY.get("new_session")),
            timeout=_POLICY.get("timeout"),
        )
        sess.connect()
        _SESSIONS[key] = sess
        return sess


def reset_cache() -> None:
    """Drop the per-process session cache (test hook)."""
    with _CACHE_LOCK:
        _SESSIONS.clear()
