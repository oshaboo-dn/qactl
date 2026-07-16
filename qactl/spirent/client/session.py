"""Reattach-first Spirent TestCenter REST session.

Thin wrapper over ``stcrestclient.stchttp.StcHttp`` that gives qactl the same
session semantics ``qactl ixia`` has: because qactl is **process-per-
invocation**, every command reattaches to the existing STC session instead
of creating a duplicate (which, on STC, would strand the config the previous
command set up).

STC REST sessions are named — the server exposes them as ``"<name> - <user>"``
strings. So reattach here means: list the server's sessions; if ours is
present, **join** it; otherwise **create** it. ``new_session=True`` forces a
fresh one. This mirrors the proven ``connect_to_session`` in cheetah's
``dnstc`` layer (``src/tests/routing/spirent/dnstc/api/stc_rest.py``).

``stcrestclient`` is imported lazily so that building the CLI parser,
``--help``, and the offline unit tests never need the package installed.
"""

from __future__ import annotations

from typing import Any, List, Optional


class SpirentConnectionError(RuntimeError):
    """Raised when the STC REST server can't be reached or the lib is absent."""


def full_session_name(name: str, user: str) -> str:
    """The server-visible session id: ``"<name> - <user>"``."""
    return f"{name} - {user}"


def _load_stchttp() -> Any:
    """Import ``stcrestclient.stchttp.StcHttp`` with a friendly failure."""
    try:
        from stcrestclient.stchttp import StcHttp  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via tools tests
        raise SpirentConnectionError(
            "stcrestclient is not installed — run `pip install qactl[spirent]` "
            "(or `pip install stcrestclient`) to enable the qactl spirent group."
        ) from exc
    return StcHttp


class SpirentSession:
    """A connected STC REST session, reattach-first.

    Construction records intent only; :meth:`connect` does the network work
    so callers can build the object cheaply and control when the wire is hit.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        *,
        session_name: str,
        new_session: bool = False,
        timeout: Optional[int] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.session_name = session_name
        self.new_session = bool(new_session)
        self.timeout = timeout
        self._stc: Any = None
        self.joined_existing: Optional[bool] = None

    @property
    def full_name(self) -> str:
        return full_session_name(self.session_name, self.user)

    @property
    def stc(self) -> Any:
        """The underlying ``StcHttp`` handle (must call :meth:`connect` first)."""
        if self._stc is None:
            raise SpirentConnectionError("not connected — call connect() first")
        return self._stc

    def _open_http(self) -> Any:
        StcHttp = _load_stchttp()
        try:
            return StcHttp(self.host, self.port, timeout=self.timeout)
        except Exception as exc:  # connection refused / DNS / etc.
            raise SpirentConnectionError(
                f"cannot reach STC REST server at {self.host}:{self.port}: {exc}"
            ) from exc

    def list_sessions(self) -> List[str]:
        """Sessions the server currently holds — no join required."""
        if self._stc is None:
            self._stc = self._open_http()
        try:
            return list(self._stc.sessions())
        except Exception as exc:
            raise SpirentConnectionError(f"STC sessions query failed: {exc}") from exc

    def connect(self) -> "SpirentSession":
        """Reattach to our named session, or create it. Idempotent per process."""
        if self._stc is not None and self._stc.started():
            return self
        if self._stc is None:
            self._stc = self._open_http()
        try:
            existing = list(self._stc.sessions())
            if self.new_session:
                self._stc.new_session(self.user, self.session_name)
                self.joined_existing = False
            elif self.full_name in existing:
                self._stc.join_session(self.full_name)
                self.joined_existing = True
            else:
                self._stc.new_session(self.user, self.session_name)
                self.joined_existing = False
        except Exception as exc:
            raise SpirentConnectionError(
                f"STC session connect failed for {self.full_name!r}: {exc}"
            ) from exc
        return self

    # -- lightweight reads used by the diag tools -------------------------

    def server_info(self) -> Any:
        return self.stc.server_info()

    def system_info(self) -> Any:
        return self.stc.system_info()

    def bll_version(self) -> Any:
        return self.stc.bll_version()

    def end_session(self, kill: bool = False) -> None:
        """Leave (or, with ``kill``, terminate) the session."""
        if self._stc is not None:
            self._stc.end_session("kill" if kill else True, self.full_name)
