from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

from ._helpers import safe_float, safe_int
from .models import IxiaOperationError

if TYPE_CHECKING:
    from .session import IxiaSession


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TrafficItemStats:
    name: str
    tx_frames: int = 0
    rx_frames: int = 0
    loss_pct: float = 0.0
    frames_delta: int = 0
    current_loss_ms: float = 0.0
    tx_rate_fps: float = 0.0
    rx_rate_fps: float = 0.0
    tx_rate_mbps: float = 0.0
    rx_rate_mbps: float = 0.0
    latency_avg_ns: int = 0

    @property
    def has_loss(self) -> bool:
        return self.loss_pct > 0.0 or self.frames_delta > 0

    @property
    def is_running(self) -> bool:
        return self.tx_rate_fps > 0.0


@dataclass
class StatsResult:
    timestamp: str
    items: list[TrafficItemStats] = field(default_factory=list)

    def __getitem__(self, name: str) -> TrafficItemStats:
        for item in self.items:
            if item.name == name:
                return item
        raise KeyError(f"No traffic item named {name!r}")

    def loss_items(self) -> list[TrafficItemStats]:
        return [i for i in self.items if i.has_loss]

    def clean_items(self) -> list[TrafficItemStats]:
        return [i for i in self.items if not i.has_loss]

    def running_items(self) -> list[TrafficItemStats]:
        return [i for i in self.items if i.is_running]

    def table(self, show_clean: bool = False) -> None:
        subset = self.items if show_clean else self.loss_items()
        if not subset and not show_clean:
            subset = self.items

        headers = [
            "Name", "TxRate", "RxRate", "Loss%",
            "FramesDelta", "CurrLoss(ms)", "Latency(ns)",
        ]
        rows: list[list[str]] = []
        for i in subset:
            rows.append([
                (i.name[:57] + "...") if len(i.name) > 60 else i.name,
                f"{i.tx_rate_fps:.1f}",
                f"{i.rx_rate_fps:.1f}",
                f"{i.loss_pct:.4f}",
                str(i.frames_delta),
                f"{i.current_loss_ms:.2f}",
                str(i.latency_avg_ns),
            ])

        use_color = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

        try:
            from tabulate import tabulate as _tabulate
            table_str = _tabulate(rows, headers=headers, tablefmt="simple")
            if use_color:
                colored_lines: list[str] = []
                for line in table_str.splitlines():
                    colored_lines.append(_color_line(line, self, subset))
                print("\n".join(colored_lines))
            else:
                print(table_str)
        except ImportError:
            widths = [max(len(h), *(len(r[ci]) for r in rows)) if rows else len(h)
                      for ci, h in enumerate(headers)]
            hdr = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
            sep = "  ".join("-" * w for w in widths)
            print(hdr)
            print(sep)
            for row in rows:
                line = "  ".join(v.ljust(w) for v, w in zip(row, widths))
                if use_color:
                    name = row[0].rstrip(".")
                    item = next((i for i in subset if i.name.startswith(name.rstrip("."))), None)
                    if item and item.has_loss:
                        line = f"\033[31m{line}\033[0m"
                    elif item and item.is_running and not item.has_loss:
                        line = f"\033[32m{line}\033[0m"
                    else:
                        line = f"\033[90m{line}\033[0m"
                print(line)

    def to_json(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for item in self.items:
            out[item.name] = {
                "tx_frames": item.tx_frames,
                "rx_frames": item.rx_frames,
                "loss_pct": item.loss_pct,
                "frames_delta": item.frames_delta,
                "current_loss_ms": item.current_loss_ms,
                "tx_rate_fps": item.tx_rate_fps,
                "rx_rate_fps": item.rx_rate_fps,
                "tx_rate_mbps": item.tx_rate_mbps,
                "rx_rate_mbps": item.rx_rate_mbps,
                "latency_ns": {"avg": item.latency_avg_ns},
            }
        return out


@dataclass
class ConvergenceResult:
    converged: bool
    loss_duration_seconds: float
    max_loss_pct: float
    affected_items: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ANSI color helper
# ---------------------------------------------------------------------------

def _color_line(line: str, result: StatsResult, subset: list[TrafficItemStats]) -> str:
    for item in subset:
        truncated = (item.name[:57] + "...") if len(item.name) > 60 else item.name
        if truncated in line:
            if item.has_loss:
                return f"\033[31m{line}\033[0m"
            if item.is_running and not item.has_loss:
                return f"\033[32m{line}\033[0m"
            return f"\033[90m{line}\033[0m"
    return line


# ---------------------------------------------------------------------------
# StatsManager
# ---------------------------------------------------------------------------

class StatsManager:
    def __init__(self, session: IxiaSession) -> None:
        self._session = session

    def _get_stat_rows(self, view_name: str) -> list[dict[str, Any]]:
        """Get rows from StatViewAssistant as list of dicts.

        Handles restpy row formats: _column_headers+_row_data, plain dict,
        __dict__ with _column_headers, and column-oriented dict-of-lists.
        """
        sv = self._session._session.StatViewAssistant(view_name)
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
                col_names = list(r.keys()) if hasattr(r, "keys") else []
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

    def _row_to_item(self, row: dict[str, Any]) -> Optional[TrafficItemStats]:
        name = str(row.get("Traffic Item", row.get("Traffic Item Name", "")))
        if not name:
            return None
        return TrafficItemStats(
            name=name,
            tx_frames=safe_int(row.get("Tx Frames", 0)),
            rx_frames=safe_int(row.get("Rx Frames", 0)),
            loss_pct=safe_float(row.get("Loss %")),
            frames_delta=safe_int(row.get("Frames Delta")),
            current_loss_ms=safe_float(row.get("Current Loss(Ms)")),
            tx_rate_fps=safe_float(row.get("Tx Frame Rate")),
            rx_rate_fps=safe_float(row.get("Rx Frame Rate")),
            tx_rate_mbps=safe_float(row.get("Tx Rate (Mbps)")),
            rx_rate_mbps=safe_float(row.get("Rx Rate (Mbps)")),
            latency_avg_ns=safe_int(row.get("Store-Forward Avg Latency (ns)")),
        )

    def all(self) -> StatsResult:
        try:
            raw_rows = self._get_stat_rows("Traffic Item Statistics")
        except Exception as e:
            raise IxiaOperationError(f"Failed to get traffic stats: {e}") from e

        items: list[TrafficItemStats] = []
        for row in raw_rows:
            item = self._row_to_item(row)
            if item is not None:
                items.append(item)

        return StatsResult(
            timestamp=datetime.now().isoformat(),
            items=items,
        )

    def item(self, name: str) -> TrafficItemStats:
        result = self.all()
        try:
            return result[name]
        except KeyError:
            raise IxiaOperationError(f"Traffic item {name!r} not found in stats") from None

    def clear(self) -> None:
        try:
            self._session.ixn.ClearStats()
        except Exception as e:
            raise IxiaOperationError(f"Failed to clear stats: {e}") from e

    def baseline(self) -> StatsResult:
        return self.all()

    def compare(self, baseline: StatsResult) -> StatsResult:
        current = self.all()
        baseline_map = {i.name: i for i in baseline.items}
        current_map = {i.name: i for i in current.items}
        all_names = list(dict.fromkeys(
            [i.name for i in baseline.items] + [i.name for i in current.items]
        ))

        delta_items: list[TrafficItemStats] = []
        for name in all_names:
            b = baseline_map.get(name)
            c = current_map.get(name)
            b_tx = b.tx_frames if b else 0
            b_rx = b.rx_frames if b else 0
            c_tx = c.tx_frames if c else 0
            c_rx = c.rx_frames if c else 0

            delta = TrafficItemStats(
                name=name,
                tx_frames=c_tx - b_tx,
                rx_frames=c_rx - b_rx,
                loss_pct=(c.loss_pct if c else 0.0) - (b.loss_pct if b else 0.0),
                frames_delta=(c.frames_delta if c else 0) - (b.frames_delta if b else 0),
                current_loss_ms=c.current_loss_ms if c else 0.0,
                tx_rate_fps=c.tx_rate_fps if c else 0.0,
                rx_rate_fps=c.rx_rate_fps if c else 0.0,
                tx_rate_mbps=c.tx_rate_mbps if c else 0.0,
                rx_rate_mbps=c.rx_rate_mbps if c else 0.0,
                latency_avg_ns=c.latency_avg_ns if c else 0,
            )
            # Stash baseline values for inspection
            delta._baseline_tx = b_tx  # type: ignore[attr-defined]
            delta._baseline_rx = b_rx  # type: ignore[attr-defined]
            delta._baseline_loss_pct = b.loss_pct if b else 0.0  # type: ignore[attr-defined]
            delta_items.append(delta)

        return StatsResult(
            timestamp=datetime.now().isoformat(),
            items=delta_items,
        )

    def convergence(
        self,
        baseline: StatsResult,
        timeout: int = 60,
        poll_interval: float = 2.0,
        threshold_pct: float = 0.1,
    ) -> ConvergenceResult:
        baseline_map = {i.name: i for i in baseline.items}
        start = time.time()
        deadline = start + timeout
        loss_start: Optional[float] = None
        max_loss = 0.0
        affected: list[str] = []

        while time.time() < deadline:
            current = self.all()
            all_converged = True

            for item in current.items:
                if item.loss_pct > threshold_pct:
                    all_converged = False
                    max_loss = max(max_loss, item.loss_pct)
                    if item.name not in affected:
                        affected.append(item.name)
                    if loss_start is None:
                        loss_start = time.time()

            if all_converged and loss_start is not None:
                duration = time.time() - loss_start
                return ConvergenceResult(
                    converged=True,
                    loss_duration_seconds=round(duration, 2),
                    max_loss_pct=round(max_loss, 2),
                    affected_items=affected,
                )

            if all_converged and loss_start is None:
                return ConvergenceResult(
                    converged=True,
                    loss_duration_seconds=0.0,
                    max_loss_pct=0.0,
                    affected_items=[],
                )

            time.sleep(poll_interval)

        duration = (time.time() - loss_start) if loss_start else (time.time() - start)
        return ConvergenceResult(
            converged=False,
            loss_duration_seconds=round(duration, 2),
            max_loss_pct=round(max_loss, 2),
            affected_items=affected,
        )

    def flow_stats(self) -> list[dict[str, Any]]:
        try:
            return self._get_stat_rows("Flow Statistics")
        except Exception as e:
            raise IxiaOperationError(f"Failed to get flow stats: {e}") from e
