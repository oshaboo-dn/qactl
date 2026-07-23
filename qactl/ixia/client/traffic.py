from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING
from fnmatch import fnmatch

if TYPE_CHECKING:
    from .session import IxiaSession

from .models import TrafficItemSummary, IxiaNotFoundError, IxiaOperationError
from ._helpers import raw_read, safe_float, safe_int


@dataclass
class HeaderInfo:
    protocol: str
    fields: dict[str, Any]


@dataclass
class StreamInfo:
    name: str
    tx_port: str
    state: str
    headers: list[HeaderInfo]
    packet_hex: str
    src_ip: Optional[str] = None
    dst_ip: Optional[str] = None
    src_mac: Optional[str] = None
    dst_mac: Optional[str] = None
    vlan_id: Optional[int] = None
    mpls_labels: list[int] = field(default_factory=list)


@dataclass
class PrefixPool:
    prefixes: list[str]
    address_family: str
    count: int
    pool_href: str = ""


@dataclass
class Endpoint:
    src_pools: list[PrefixPool]
    dst_pools: list[PrefixPool]
    src_refs: list[str]
    dst_refs: list[str]


@dataclass
class TrafficRate:
    value: float
    rate_type: str
    units: str = ""


@dataclass
class FrameSize:
    fixed_size: int = 0
    size_type: str = "fixed"
    min_size: int = 64
    max_size: int = 1518


@dataclass
class TrafficItemInfo:
    name: str
    state: str
    enabled: bool
    rate: TrafficRate
    frame_size: FrameSize
    endpoints: list[Endpoint]
    streams: list[StreamInfo]

    def table(self) -> None:
        """Pretty-print the full config."""
        print(f"Traffic Item: {self.name}")
        print(f"  State: {self.state}  Enabled: {self.enabled}")
        print(f"  Rate: {self.rate.value} {self.rate.rate_type}"
              + (f" ({self.rate.units})" if self.rate.units else ""))
        sz = self.frame_size
        if sz.size_type == "fixed":
            print(f"  Frame Size: {sz.fixed_size} bytes (fixed)")
        else:
            print(f"  Frame Size: {sz.size_type} [{sz.min_size}-{sz.max_size}]")
        print(f"  Endpoints: {len(self.endpoints)}")
        for idx, ep in enumerate(self.endpoints):
            print(f"    [{idx}] src: {len(ep.src_pools)} prefix pools, "
                  f"dst: {len(ep.dst_pools)} prefix pools")
        print(f"  Streams: {len(self.streams)}")
        for idx, s in enumerate(self.streams):
            parts = []
            if s.src_ip:
                parts.append(f"src={s.src_ip}")
            if s.dst_ip:
                parts.append(f"dst={s.dst_ip}")
            if s.vlan_id is not None:
                parts.append(f"vlan={s.vlan_id}")
            if s.mpls_labels:
                parts.append(f"mpls={s.mpls_labels}")
            detail = " ".join(parts)
            print(f"    [{idx}] {detail}" if detail else f"    [{idx}] {s.name}")

    def to_json(self, path: str) -> None:
        """Save to JSON file."""
        from dataclasses import asdict
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, default=str)


class TrafficItemProxy:
    """Fluent proxy for a single traffic item. Phase 2 adds .inspect(), .preview(), .stats(), .start(), .stop()."""

    def __init__(self, session: IxiaSession, name: str):
        self._session = session
        self._name = name
        self._ti: Any = None

    @property
    def name(self) -> str:
        return self._name

    def _resolve(self) -> Any:
        """Find the RestPy TrafficItem by name. Raises IxiaNotFoundError if not found."""
        if self._ti is None:
            ixn = self._session.ixn
            ti = ixn.Traffic.TrafficItem.find(Name=self._name)
            if not ti:
                raise IxiaNotFoundError(f"Traffic item {self._name!r} not found")
            self._ti = ti
        return self._ti

    def start(self) -> TrafficItemProxy:
        """Start this traffic item. Returns self for chaining."""
        ti = self._resolve()
        try:
            ti.StartStatelessTrafficBlocking()
        except Exception as e:
            raise IxiaOperationError(f"Failed to start {self._name!r}: {e}") from e
        return self

    def stop(self) -> TrafficItemProxy:
        """Stop this traffic item. Returns self for chaining."""
        ti = self._resolve()
        try:
            ti.StopStatelessTrafficBlocking()
        except Exception as e:
            raise IxiaOperationError(f"Failed to stop {self._name!r}: {e}") from e
        return self

    def enable(self) -> TrafficItemProxy:
        """Enable this traffic item. Returns self for chaining."""
        ti = self._resolve()
        try:
            ti.Enabled = True
            ti.update()
        except Exception as e:
            raise IxiaOperationError(f"Failed to enable {self._name!r}: {e}") from e
        return self

    def disable(self) -> TrafficItemProxy:
        """Disable this traffic item. Returns self for chaining."""
        ti = self._resolve()
        try:
            ti.Enabled = False
            ti.update()
        except Exception as e:
            raise IxiaOperationError(f"Failed to disable {self._name!r}: {e}") from e
        return self

    def delete(self) -> None:
        """Delete this traffic item from the session."""
        ti = self._resolve()
        try:
            ti.remove()
        except Exception as e:
            raise IxiaOperationError(f"Failed to delete {self._name!r}: {e}") from e
        self._ti = None

    def modify_rate(self, rate: float, rate_type: str = "framesPerSecond") -> TrafficItemProxy:
        """Modify traffic rate. rate_type: 'framesPerSecond', 'bitsPerSecond', 'percentLineRate'.

        Operates on ConfigElement.FrameRate.
        """
        ti = self._resolve()
        try:
            config = ti.ConfigElement.find()
            if not config:
                raise IxiaOperationError(f"No ConfigElement on {self._name!r}")
            config.FrameRate.update(Type=rate_type, Rate=rate)
        except IxiaOperationError:
            raise
        except Exception as e:
            raise IxiaOperationError(f"Failed to modify rate on {self._name!r}: {e}") from e
        return self

    def modify_frame_size(self, size: int) -> TrafficItemProxy:
        """Modify frame size (fixed). Operates on ConfigElement.FrameSize."""
        ti = self._resolve()
        try:
            config = ti.ConfigElement.find()
            if not config:
                raise IxiaOperationError(f"No ConfigElement on {self._name!r}")
            config.FrameSize.update(Type="fixed", FixedSize=size)
        except IxiaOperationError:
            raise
        except Exception as e:
            raise IxiaOperationError(f"Failed to modify frame size on {self._name!r}: {e}") from e
        return self

    def preview(self, max_streams: int = 5) -> list[StreamInfo]:
        """Walk HighLevelStream > Stack > Field to extract packet structure."""
        ti = self._resolve()
        ixn = self._session.ixn
        result: list[StreamInfo] = []

        _skip_fields = {
            "Reserved", "Unused", "Padding", "No operation",
            "Security type", "Option length", "Option type",
            "Pointer", "Route data", "Overflow", "Flags",
            "Address", "Timestamp", "End of options",
            "Router alert value", "Stream identifier",
            "Compartments", "Handling restrictions",
            "Transmission control code", "Security",
            "PFC Queue", "Label Tracker",
        }

        try:
            streams = ti.HighLevelStream.find()
            for i, stream in enumerate(streams):
                if i >= max_streams:
                    break

                packet_hex = ""
                try:
                    packet_hex = stream.GetPacketViewInHex(0)
                except Exception:
                    pass

                headers: list[HeaderInfo] = []
                src_ip = dst_ip = src_mac = dst_mac = None
                vlan_id: Optional[int] = None
                mpls_labels: list[int] = []

                for stack in stream.Stack.find():
                    proto = stack.DisplayName
                    fields: dict[str, Any] = {}

                    for fld in stack.Field.find():
                        display = fld.DisplayName
                        if display in _skip_fields:
                            continue

                        vtype = getattr(fld, "ValueType", "singleValue")
                        val: Any = getattr(fld, "SingleValue", None)
                        if val is None:
                            val = getattr(fld, "FieldValue", None)
                        if val is None:
                            val = ""

                        if vtype == "increment":
                            val = {
                                "start": getattr(fld, "StartValue", ""),
                                "step": getattr(fld, "StepValue", ""),
                                "count": getattr(fld, "CountValue", ""),
                            }
                        elif vtype == "valueList":
                            val = getattr(fld, "ValueList", val)

                        if val in ("0", "", None) and vtype == "singleValue":
                            continue

                        is_learnt = (
                            isinstance(val, list) and
                            all(str(v).lower() in ("learntinfo", "learnt") for v in val)
                        )

                        if is_learnt:
                            fields[display] = {"value": "(dynamic)", "type": "learnt"}
                            continue

                        fields[display] = {"value": val, "type": vtype}

                        if isinstance(val, list) and val:
                            raw_val = str(val[0])
                        elif isinstance(val, str):
                            raw_val = val
                        elif isinstance(val, dict):
                            raw_val = val.get("start", "")
                        else:
                            raw_val = str(val)

                        if display == "Source Address" and "IP" in proto.upper():
                            src_ip = raw_val
                        elif display == "Destination Address" and "IP" in proto.upper():
                            dst_ip = raw_val
                        elif display == "Source MAC Address":
                            src_mac = raw_val
                        elif display == "Destination MAC Address":
                            dst_mac = raw_val
                        elif display == "VLAN ID":
                            vlan_id = safe_int(raw_val) or None
                        elif display == "Label Value":
                            lbl = safe_int(raw_val)
                            if lbl:
                                mpls_labels.append(lbl)

                    if fields:
                        headers.append(HeaderInfo(protocol=proto, fields=fields))

                result.append(StreamInfo(
                    name=getattr(stream, "Name", ""),
                    tx_port=getattr(stream, "TxPortName", ""),
                    state=getattr(stream, "State", "unknown"),
                    headers=headers,
                    packet_hex=packet_hex,
                    src_ip=src_ip,
                    dst_ip=dst_ip,
                    src_mac=src_mac,
                    dst_mac=dst_mac,
                    vlan_id=vlan_id,
                    mpls_labels=mpls_labels,
                ))
        except Exception as e:
            raise IxiaOperationError(
                f"Failed to get preview for {self._name!r}: {e}"
            ) from e

        return result

    def inspect(self, max_streams: int = 5) -> TrafficItemInfo:
        """Full deep introspection via raw REST: rate, frame size, endpoints, streams."""
        ti = self._resolve()
        ixn = self._session.ixn
        item_href = ti.href

        rate = TrafficRate(value=0.0, rate_type="framesPerSecond")
        frame_size = FrameSize()

        # Try RestPy first (reliable — same path as modify_rate/modify_frame_size)
        try:
            config = ti.ConfigElement.find()
            if config:
                fr = config.FrameRate
                rate = TrafficRate(
                    value=safe_float(getattr(fr, "Rate", 0)),
                    rate_type=getattr(fr, "Type", "framesPerSecond"),
                    units=getattr(fr, "BitRateUnitsType", ""),
                )
                fs = config.FrameSize
                frame_size = FrameSize(
                    fixed_size=safe_int(getattr(fs, "FixedSize", 0)),
                    size_type=getattr(fs, "Type", "fixed"),
                    min_size=safe_int(getattr(fs, "MinSize", 64)),
                    max_size=safe_int(getattr(fs, "MaxSize", 1518)),
                )
        except Exception:
            # Fall back to raw REST
            try:
                ces = raw_read(ixn, item_href + "/configElement?skip=0&take=10")
                if ces:
                    ce = ces[0] if isinstance(ces, list) else ces
                    try:
                        fr_href = ce.get("frameRate", {}).get("href", "")
                        if fr_href:
                            fr = raw_read(ixn, fr_href)
                            rate = TrafficRate(
                                value=safe_float(fr.get("rate", 0)),
                                rate_type=fr.get("type", "framesPerSecond"),
                                units=fr.get("bitRateUnitsType", ""),
                            )
                    except Exception:
                        pass
                    try:
                        fs_href = ce.get("frameSize", {}).get("href", "")
                        if fs_href:
                            fs = raw_read(ixn, fs_href)
                            frame_size = FrameSize(
                                fixed_size=safe_int(fs.get("fixedSize", 0)),
                                size_type=fs.get("type", "fixed"),
                                min_size=safe_int(fs.get("minSize", 64)),
                                max_size=safe_int(fs.get("maxSize", 1518)),
                            )
                    except Exception:
                        pass
            except Exception:
                pass

        endpoints: list[Endpoint] = []
        try:
            eps = raw_read(ixn, item_href + "/endpointSet?skip=0&take=50")
            if not isinstance(eps, list):
                eps = [eps] if eps else []
            for ep in eps:
                src_pools: list[PrefixPool] = []
                dst_pools: list[PrefixPool] = []
                src_refs: list[str] = []
                dst_refs: list[str] = []

                for s in ep.get("scalableSources", []):
                    pool_href = s.get("arg1", "")
                    if pool_href:
                        src_refs.append(pool_href)
                        pool = self._resolve_prefix_pool(ixn, pool_href)
                        if pool:
                            src_pools.append(pool)

                for d in ep.get("scalableDestinations", []):
                    pool_href = d.get("arg1", "")
                    if pool_href:
                        dst_refs.append(pool_href)
                        pool = self._resolve_prefix_pool(ixn, pool_href)
                        if pool:
                            dst_pools.append(pool)

                for ref in ep.get("sources", []):
                    if isinstance(ref, str):
                        src_refs.append(ref)
                        if "/PrefixPools/" in ref or "/prefixPools/" in ref:
                            pool = self._resolve_prefix_pool(ixn, ref)
                            if pool:
                                src_pools.append(pool)
                for ref in ep.get("destinations", []):
                    if isinstance(ref, str):
                        dst_refs.append(ref)
                        if "/PrefixPools/" in ref or "/prefixPools/" in ref:
                            pool = self._resolve_prefix_pool(ixn, ref)
                            if pool:
                                dst_pools.append(pool)

                endpoints.append(Endpoint(
                    src_pools=src_pools,
                    dst_pools=dst_pools,
                    src_refs=src_refs,
                    dst_refs=dst_refs,
                ))
        except Exception:
            pass

        try:
            streams = self.preview(max_streams=max_streams)
        except Exception:
            streams = []

        return TrafficItemInfo(
            name=self._name,
            state=getattr(ti, "State", "unknown"),
            enabled=bool(getattr(ti, "Enabled", False)),
            rate=rate,
            frame_size=frame_size,
            endpoints=endpoints,
            streams=streams,
        )

    @staticmethod
    def _resolve_prefix_pool(ixn: Any, pool_href: str) -> Optional[PrefixPool]:
        """Resolve a prefix pool href to its address list. Returns None on failure."""
        af = "ipv6" if "ipv6PrefixPools" in pool_href else "ipv4"
        try:
            pool_data = raw_read(ixn, pool_href)
            count = safe_int(pool_data.get("count", 0))
            if count == 0:
                return PrefixPool(prefixes=[], address_family=af, count=0, pool_href=pool_href)

            addr_href = pool_data.get("networkAddress", {}).get("href", "")
            pfx_href = pool_data.get("prefixLength", {}).get("href", "")
            if not addr_href or not pfx_href:
                return PrefixPool(prefixes=[], address_family=af, count=count, pool_href=pool_href)

            addrs = raw_read(ixn, addr_href + f"?skip=0&take={count}").get("values", [])
            pfxlens = raw_read(ixn, pfx_href + f"?skip=0&take={count}").get("values", [])
            prefixes = [f"{a}/{p}" for a, p in zip(addrs, pfxlens)]

            return PrefixPool(
                prefixes=prefixes,
                address_family=af,
                count=count,
                pool_href=pool_href,
            )
        except Exception:
            return PrefixPool(prefixes=[], address_family=af, count=0, pool_href=pool_href)

    def __repr__(self) -> str:
        return f"TrafficItemProxy({self._name!r})"


class TrafficManager:
    """Traffic item listing, search, and control.

    Usage:
        s.traffic.list()              # all items
        s.traffic.find("INDIA*")      # glob search
        s.traffic("item_name")        # get proxy for one item
    """

    def __init__(self, session: IxiaSession):
        self._session = session

    def list(self) -> list[TrafficItemSummary]:
        """List all traffic items with name, state, enabled status."""
        ixn = self._session.ixn
        items = []
        for ti in ixn.Traffic.TrafficItem.find():
            items.append(TrafficItemSummary(
                name=ti.Name,
                state=getattr(ti, "State", "unknown"),
                enabled=bool(getattr(ti, "Enabled", False)),
            ))
        return items

    def find(self, pattern: str) -> list[TrafficItemSummary]:
        """Find traffic items by glob pattern. Supports * and ? wildcards."""
        return [item for item in self.list() if fnmatch(item.name, pattern)]

    def start_all(self) -> None:
        """Start all traffic items (blocking)."""
        try:
            self._session.ixn.Traffic.StartStatelessTrafficBlocking()
        except Exception as e:
            raise IxiaOperationError(f"Failed to start all traffic: {e}") from e

    def stop_all(self) -> None:
        """Stop all traffic items (blocking)."""
        try:
            self._session.ixn.Traffic.StopStatelessTrafficBlocking()
        except Exception as e:
            raise IxiaOperationError(f"Failed to stop all traffic: {e}") from e

    def regenerate(self) -> None:
        """Full regenerate sequence: stop -> regenerate -> apply -> start.

        This is the safe sequence that avoids stale config issues.
        """
        ixn = self._session.ixn
        try:
            ixn.Traffic.StopStatelessTrafficBlocking()
            all_ti = ixn.Traffic.TrafficItem.find()
            if all_ti:
                all_ti.Generate()
            ixn.Traffic.Apply()
            ixn.Traffic.StartStatelessTrafficBlocking()
        except Exception as e:
            raise IxiaOperationError(f"Failed to regenerate traffic: {e}") from e

    def clear_stats(self) -> None:
        """Clear all traffic statistics counters."""
        try:
            self._session.ixn.ClearStats()
        except Exception as e:
            raise IxiaOperationError(f"Failed to clear stats: {e}") from e

    def create(
        self,
        name: str,
        src: Any,
        dst: Any,
        rate_fps: Optional[int] = None,
        frame_size: Optional[int] = None,
        traffic_type: str = "raw",
    ) -> TrafficItemProxy:
        """Create a new traffic item. Returns proxy for the new item."""
        ixn = self._session.ixn
        try:
            ti = ixn.Traffic.TrafficItem.add(Name=name, TrafficType=traffic_type)
            ti.EndpointSet.add(Sources=src, Destinations=dst)
            config = ti.ConfigElement.find()
            if config:
                if rate_fps is not None:
                    config.FrameRate.update(Type="framesPerSecond", Rate=rate_fps)
                if frame_size is not None:
                    config.FrameSize.update(Type="fixed", FixedSize=frame_size)
            ti.Generate()
        except Exception as e:
            raise IxiaOperationError(f"Failed to create traffic item {name!r}: {e}") from e
        return TrafficItemProxy(self._session, name)

    def __call__(self, name: str) -> TrafficItemProxy:
        """Get a fluent proxy for a single traffic item by exact name.

        Usage: s.traffic("item_name").start()
        """
        return TrafficItemProxy(self._session, name)

    def __repr__(self) -> str:
        return "TrafficManager()"
