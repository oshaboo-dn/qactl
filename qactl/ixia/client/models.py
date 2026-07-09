from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class IxiaError(Exception):
    """Base exception for all Ixia wrapper errors."""
    pass


class IxiaConnectionError(IxiaError):
    """Connection or port discovery failure."""
    pass


class IxiaNotFoundError(IxiaError):
    """Traffic item, device group, or topology not found."""
    pass


class IxiaTimeoutError(IxiaError):
    """Timeout waiting for protocol convergence or operation."""
    pass


class IxiaOperationError(IxiaError):
    """General operation failure."""
    pass


# ---------------------------------------------------------------------------
# Phase 1 dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TrafficItemSummary:
    """Lightweight traffic item info from list()."""
    name: str
    state: str
    enabled: bool

    def __repr__(self) -> str:
        status = "ON" if self.enabled else "OFF"
        return f"TrafficItem({self.name!r}, state={self.state}, {status})"


@dataclass
class TopologySummary:
    """Lightweight topology info from list()."""
    name: str
    ports: list[str]
    device_groups: list[str]
    href: str = ""

    def __repr__(self) -> str:
        return f"Topology({self.name!r}, ports={len(self.ports)}, dgs={len(self.device_groups)})"


@dataclass
class DeviceGroupSummary:
    """Lightweight DG info."""
    name: str
    multiplier: int
    href: str = ""
    children: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"DeviceGroup({self.name!r}, multiplier={self.multiplier})"
