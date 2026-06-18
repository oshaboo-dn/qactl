from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from .models import IxiaConnectionError

if TYPE_CHECKING:
    from .traffic import TrafficManager
    from .topology import TopologyManager
    from .stats import StatsManager
    from .protocols import ProtocolManager
    from .config import ConfigManager


class IxiaSession:
    """Fluent entry point for IxNetwork interaction.

    Usage:
        # Explicit port
        with IxiaSession("10.0.0.5", port=443, user="admin") as s:
            items = s.traffic.list()

        # Auto-discover port
        s = IxiaSession("win-client199", user="dn")
        s.connect()

        # Attach to existing session
        s = IxiaSession("win-client199", port=11009)
        s.attach(session_id=1)
    """

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        user: str = "",
        password: str = "",
        session_name: Optional[str] = None,
        api_key: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.session_name = session_name
        self.api_key = api_key

        self._session: Any = None
        self._ixnetwork: Any = None
        self._connected: bool = False

        self._traffic: Optional[TrafficManager] = None
        self._topology: Optional[TopologyManager] = None
        self._stats: Any = None
        self._protocols: Any = None
        self._config: Any = None

    # ------------------------------------------------------------------
    # Connection state
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ixn(self) -> Any:
        """Raw ixnetwork object for advanced use."""
        if not self._connected or self._ixnetwork is None:
            raise IxiaConnectionError("Not connected. Call connect() first.")
        return self._ixnetwork

    # ------------------------------------------------------------------
    # Sub-manager properties (lazy, local imports to avoid circular deps)
    # ------------------------------------------------------------------

    @property
    def traffic(self) -> TrafficManager:
        """Traffic item control and introspection."""
        if self._traffic is None:
            from .traffic import TrafficManager
            self._traffic = TrafficManager(self)
        return self._traffic

    @property
    def topology(self) -> TopologyManager:
        """Topology and device group control."""
        if self._topology is None:
            from .topology import TopologyManager
            self._topology = TopologyManager(self)
        return self._topology

    @property
    def stats(self) -> StatsManager:
        """Traffic statistics collection and analysis."""
        if self._stats is None:
            from .stats import StatsManager
            self._stats = StatsManager(self)
        return self._stats

    @property
    def protocols(self) -> ProtocolManager:
        """Protocol session monitoring and control."""
        if self._protocols is None:
            from .protocols import ProtocolManager
            self._protocols = ProtocolManager(self)
        return self._protocols

    @property
    def config(self) -> ConfigManager:
        """Configuration save/load/new."""
        if self._config is None:
            from .config import ConfigManager
            self._config = ConfigManager(self)
        return self._config

    # ------------------------------------------------------------------
    # Connect / attach / disconnect
    # ------------------------------------------------------------------

    def connect(self, port: Optional[int] = None) -> IxiaSession:
        """Connect to the IxNetwork API server.

        Port resolution order: arg > self.port > discover_api_port().
        Returns self for chaining.
        """
        if self._connected:
            return self

        resolved_port = port or self.port
        if resolved_port is None:
            resolved_port = self._discover_port()
        self.port = resolved_port

        self._open_session(
            RestPort=resolved_port,
            ClearConfig=False,
        )
        return self

    def attach(
        self,
        session_id: Optional[int] = None,
        session_name: Optional[str] = None,
        port: Optional[int] = None,
    ) -> IxiaSession:
        """Attach to an existing IxNetwork session by ID or name.
        Returns self for chaining.
        """
        resolved_port = port or self.port
        if resolved_port is None:
            resolved_port = self._discover_port()
        self.port = resolved_port

        extra: dict[str, Any] = {}
        if session_id is not None:
            extra["SessionId"] = session_id
        if session_name is not None:
            extra["SessionName"] = session_name

        self._open_session(
            RestPort=resolved_port,
            ClearConfig=False,
            **extra,
        )
        return self

    def disconnect(self) -> None:
        """Clean disconnect from IxNetwork. Safe to call multiple times."""
        if not self._connected:
            return
        try:
            if self._session is not None:
                tp = getattr(self._session, "TestPlatform", None)
                # Windows sessions are shared -- never remove them
                if tp and getattr(tp, "Platform", "").lower() != "windows":
                    session_obj = getattr(self._session, "Session", None)
                    if session_obj and hasattr(session_obj, "remove"):
                        session_obj.remove()
        except Exception:
            pass
        finally:
            self._session = None
            self._ixnetwork = None
            self._connected = False
            self._traffic = None
            self._topology = None
            self._stats = None
            self._protocols = None
            self._config = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> IxiaSession:
        if not self._connected:
            self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _discover_port(self) -> int:
        from ._discovery import discover_api_port
        try:
            return discover_api_port(
                self.host, ssh_user=self.user, ssh_password=self.password,
            )
        except Exception as exc:
            raise IxiaConnectionError(
                f"Port discovery failed for {self.host}: {exc}"
            ) from exc

    def _open_session(self, **kwargs: Any) -> None:
        """Create a SessionAssistant and store session + ixnetwork handles."""
        from ixnetwork_restpy import SessionAssistant

        sa_kwargs: dict[str, Any] = {
            "IpAddress": self.host,
            "LogLevel": SessionAssistant.LOGLEVEL_NONE,
        }
        if self.user:
            sa_kwargs["UserName"] = self.user
        if self.password:
            sa_kwargs["Password"] = self.password
        if self.api_key:
            sa_kwargs["ApiKey"] = self.api_key
        if self.session_name:
            sa_kwargs["SessionName"] = self.session_name

        sa_kwargs.update(kwargs)

        try:
            self._session = SessionAssistant(**sa_kwargs)
            self._ixnetwork = self._session.Ixnetwork
            self._connected = True
        except Exception as exc:
            raise IxiaConnectionError(
                f"Connection to {self.host}:{sa_kwargs.get('RestPort', '?')} failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        return f"IxiaSession({self.host}:{self.port}, {status})"
