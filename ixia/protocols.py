from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .session import IxiaSession

from .models import IxiaOperationError, IxiaTimeoutError
from ._helpers import safe_int


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ProtocolSummaryResult:
    """Per-protocol session counts."""
    protocol: str
    up: int = 0
    down: int = 0
    not_started: int = 0

    @property
    def total(self) -> int:
        return self.up + self.down + self.not_started

    @property
    def all_up(self) -> bool:
        return self.down == 0 and self.not_started == 0 and self.up > 0


@dataclass
class ProtocolSessionDetail:
    """Individual protocol session info."""
    name: str
    status: str
    session_type: str


# ---------------------------------------------------------------------------
# Private stat-view helper (local copy — avoids cross-file dep during build)
# ---------------------------------------------------------------------------

def _get_stat_rows(session_assistant: Any, view_name: str) -> list[dict[str, Any]]:
    """Extract rows from a StatViewAssistant view as a list of dicts.

    Handles the restpy row format where each row object contains
    _column_headers + _row_data, plain dicts, and column-oriented layouts.
    """
    sv = session_assistant.StatViewAssistant(view_name)
    rows: list[dict[str, Any]] = []
    try:
        r = sv.Rows
        if r is None:
            return rows
        if hasattr(r, "__iter__") and not isinstance(r, (str, dict)):
            for row in r:
                if hasattr(row, "get") and "_column_headers" in (
                    row if isinstance(row, dict) else getattr(row, "__dict__", {})
                ):
                    row_dict = row if isinstance(row, dict) else vars(row)
                    headers = row_dict.get("_column_headers", [])
                    data_rows = row_dict.get("_row_data", [])
                    for data in data_rows:
                        rows.append(dict(zip(headers, data)))
                    break
                elif hasattr(row, "items"):
                    rows.append(dict(row))
                elif hasattr(row, "__dict__"):
                    d = vars(row)
                    if "_column_headers" in d:
                        headers = d.get("_column_headers", [])
                        data_rows = d.get("_row_data", [])
                        for data in data_rows:
                            rows.append(dict(zip(headers, data)))
                        break
                    rows.append(d)
                else:
                    rows.append(dict(row) if row else {})
        elif hasattr(r, "keys"):
            col_names = list(r.keys())
            if col_names:
                first_col = r.get(col_names[0], []) or []
                n = len(first_col) if isinstance(first_col, (list, tuple)) else 1
                for i in range(n):
                    row = {}
                    for k in col_names:
                        val = r.get(k)
                        if isinstance(val, (list, tuple)) and i < len(val):
                            row[k] = val[i]
                        elif not isinstance(val, (list, tuple)):
                            row[k] = val
                    rows.append(row)
    except Exception:
        pass
    return rows


def _get_protocol_rows(proto_name: str, proto_obj: Any) -> list[ProtocolSessionDetail]:
    """Extract session details from a protocol object's SessionStatus."""
    details: list[ProtocolSessionDetail] = []
    try:
        if hasattr(proto_obj, "SessionStatus"):
            statuses = proto_obj.SessionStatus
            if statuses:
                for i, s in enumerate(statuses):
                    status_str = str(s).lower() if s else "down"
                    details.append(ProtocolSessionDetail(
                        name=getattr(proto_obj, "Name", f"{proto_name}_{i}") or f"{proto_name}_{i}",
                        status="up" if "up" in status_str else "down",
                        session_type=proto_name,
                    ))
    except Exception:
        pass
    return details


# ---------------------------------------------------------------------------
# ProtocolManager
# ---------------------------------------------------------------------------

class ProtocolManager:
    def __init__(self, session: IxiaSession) -> None:
        self._session = session

    def summary(self) -> list[ProtocolSummaryResult]:
        """Protocol summary counts via the 'Protocols Summary' stat view."""
        results: list[ProtocolSummaryResult] = []
        try:
            for row in _get_stat_rows(self._session._session, "Protocols Summary"):
                proto = str(row.get("Protocol", row.get("Protocol Type", "Unknown")))
                results.append(ProtocolSummaryResult(
                    protocol=proto,
                    up=safe_int(row.get("Sessions Up", 0)),
                    down=safe_int(row.get("Sessions Down", 0)),
                    not_started=safe_int(row.get("Sessions Not Started", 0)),
                ))
        except Exception as e:
            raise IxiaOperationError(f"Failed to get protocol summary: {e}") from e
        return results

    def status(self) -> dict[str, list[ProtocolSessionDetail]]:
        """Per-session detail. Walks Topology > DeviceGroup > Ethernet > Ipv4."""
        ixn = self._session.ixn
        result: dict[str, list[ProtocolSessionDetail]] = {}

        try:
            for topo in ixn.Topology.find():
                for dg in topo.DeviceGroup.find():
                    for eth in dg.Ethernet.find():
                        for ipv4 in eth.Ipv4.find():
                            for bgp in ipv4.BgpIpv4Peer.find():
                                rows = _get_protocol_rows("BGP", bgp)
                                if rows:
                                    result.setdefault("BGP", []).extend(rows)

                            for ospf in ipv4.Ospfv2.find():
                                rows = _get_protocol_rows("OSPF", ospf)
                                if rows:
                                    result.setdefault("OSPF", []).extend(rows)

                            if hasattr(ipv4, "IsisL3"):
                                for isis in ipv4.IsisL3.find():
                                    rows = _get_protocol_rows("ISIS", isis)
                                    if rows:
                                        result.setdefault("ISIS", []).extend(rows)

            # Fallback: stat view if topology walk found nothing
            if not result:
                for row in _get_stat_rows(self._session._session, "Protocols Summary"):
                    proto = str(row.get("Protocol", row.get("Protocol Type", "Unknown")))
                    result.setdefault(proto, []).append(ProtocolSessionDetail(
                        name=row.get("Name", ""),
                        status="up" if safe_int(row.get("Sessions Up", 0)) > 0 else "down",
                        session_type=proto,
                    ))
        except Exception as e:
            raise IxiaOperationError(f"Failed to get protocol status: {e}") from e

        return result

    def start_all(self, sync: bool = True) -> None:
        """Start all protocols."""
        ixn = self._session.ixn
        try:
            ixn.StartAllProtocols(Arg1="sync" if sync else "async")
        except Exception as e:
            raise IxiaOperationError(f"Failed to start all protocols: {e}") from e

    def stop_all(self, sync: bool = True) -> None:
        """Stop all protocols."""
        ixn = self._session.ixn
        try:
            ixn.StopAllProtocols(Arg1="sync" if sync else "async")
        except Exception as e:
            raise IxiaOperationError(f"Failed to stop all protocols: {e}") from e

    def wait_up(
        self,
        protocols: Optional[list[str]] = None,
        timeout: int = 120,
        poll_interval: float = 2.0,
    ) -> bool:
        """Poll until all (or specified) protocols report zero down/not-started.

        Raises IxiaTimeoutError if timeout exceeded.
        Returns True on success.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            summaries = self.summary()
            if not summaries:
                time.sleep(poll_interval)
                continue

            summary_map = {s.protocol: s for s in summaries}
            to_check = protocols or list(summary_map.keys())

            all_up = True
            for proto in to_check:
                s = summary_map.get(proto)
                if s is None or not s.all_up:
                    all_up = False
                    break

            if all_up:
                return True
            time.sleep(poll_interval)

        checked = protocols or "all"
        raise IxiaTimeoutError(
            f"Protocols {checked} not up after {timeout}s"
        )

    def table(self) -> None:
        """Pretty-print protocol summary to terminal."""
        summaries = self.summary()
        if not summaries:
            print("No protocol data available.")
            return

        header = f"{'Protocol':<20} {'Up':>6} {'Down':>6} {'NotStarted':>12} {'Total':>6}"
        print(header)
        print("-" * len(header))
        for s in summaries:
            print(f"{s.protocol:<20} {s.up:>6} {s.down:>6} {s.not_started:>12} {s.total:>6}")
