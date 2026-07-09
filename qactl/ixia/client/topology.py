from __future__ import annotations

from typing import Any, Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from .session import IxiaSession

from ._helpers import read_multivalue
from .models import TopologySummary, IxiaNotFoundError, IxiaOperationError


class BgpProxy:
    """Fluent access to BGP peer properties on a device group."""

    def __init__(self, session: IxiaSession, dg: Any):
        self._session = session
        self._dg = dg
        self._peer: Any = None

    def _resolve_peer(self) -> Any:
        if self._peer is None:
            for eth in self._dg.Ethernet.find():
                for ipv4 in eth.Ipv4.find():
                    peers = ipv4.BgpIpv4Peer.find()
                    if peers:
                        self._peer = peers
                        return self._peer
            raise IxiaNotFoundError(f"No BGP peer found on DG {self._dg.Name!r}")
        return self._peer

    @property
    def dut_ip(self) -> Any:
        """Read BGP DUT IP. Returns unwrapped Python value."""
        peer = self._resolve_peer()
        return read_multivalue(peer.DutIp, self._session.ixn)

    @dut_ip.setter
    def dut_ip(self, value: Union[str, list[str]]) -> None:
        """Write BGP DUT IP. Auto-selects Single vs ValueList based on input type."""
        peer = self._resolve_peer()
        self._write_multivalue(peer.DutIp, value)

    @property
    def local_ip(self) -> Any:
        """Read BGP local IP (from the parent IPv4 stack)."""
        for eth in self._dg.Ethernet.find():
            for ipv4 in eth.Ipv4.find():
                return read_multivalue(ipv4.Address, self._session.ixn)
        return None

    @property
    def peers(self) -> list[Any]:
        """List all BGP peer objects in this DG (including children)."""
        result: list[Any] = []
        self._collect_peers(self._dg, result)
        return result

    def overlay(self, index: int, value: str) -> BgpProxy:
        """Set a per-device override on DUT IP. Overlay index is 1-based."""
        peer = self._resolve_peer()
        peer.DutIp.Overlay(index, value)
        return self

    def _write_multivalue(self, mv_obj: Any, value: Union[str, list[str]]) -> None:
        if isinstance(value, list):
            mv_obj.ValueList(value)
        else:
            mv_obj.Single(value)

    @staticmethod
    def _collect_peers(parent: Any, result: list[Any]) -> None:
        for eth in parent.Ethernet.find():
            for ipv4 in eth.Ipv4.find():
                for bgp in ipv4.BgpIpv4Peer.find():
                    result.append(bgp)
        for child in parent.DeviceGroup.find():
            BgpProxy._collect_peers(child, result)

    def __repr__(self) -> str:
        return f"BgpProxy(dg={self._dg.Name!r})"


class Ipv4Proxy:
    """Fluent access to IPv4 stack properties."""

    def __init__(self, session: IxiaSession, dg: Any):
        self._session = session
        self._dg = dg
        self._ipv4: Any = None

    def _resolve(self) -> Any:
        if self._ipv4 is None:
            for eth in self._dg.Ethernet.find():
                stacks = eth.Ipv4.find()
                if stacks:
                    self._ipv4 = stacks
                    return self._ipv4
            raise IxiaNotFoundError(f"No IPv4 stack found on DG {self._dg.Name!r}")
        return self._ipv4

    @property
    def address(self) -> Any:
        """Read IPv4 address. Returns unwrapped Python value."""
        return read_multivalue(self._resolve().Address, self._session.ixn)

    @address.setter
    def address(self, value: Union[str, list[str]]) -> None:
        """Write IPv4 address."""
        ipv4 = self._resolve()
        if isinstance(value, list):
            ipv4.Address.ValueList(value)
        else:
            ipv4.Address.Single(value)

    @property
    def gateway(self) -> Any:
        """Read IPv4 gateway. Returns unwrapped Python value."""
        return read_multivalue(self._resolve().GatewayIp, self._session.ixn)

    @gateway.setter
    def gateway(self, value: Union[str, list[str]]) -> None:
        """Write IPv4 gateway."""
        ipv4 = self._resolve()
        if isinstance(value, list):
            ipv4.GatewayIp.ValueList(value)
        else:
            ipv4.GatewayIp.Single(value)

    @property
    def prefix_length(self) -> Any:
        """Read IPv4 prefix length. Returns unwrapped Python value."""
        return read_multivalue(self._resolve().Prefix, self._session.ixn)

    @prefix_length.setter
    def prefix_length(self, value: Union[int, list[int]]) -> None:
        """Write IPv4 prefix length."""
        ipv4 = self._resolve()
        if isinstance(value, list):
            ipv4.Prefix.ValueList([str(v) for v in value])
        else:
            ipv4.Prefix.Single(str(value))

    def __repr__(self) -> str:
        return f"Ipv4Proxy(dg={self._dg.Name!r})"


class DeviceGroupProxy:
    """Fluent proxy for a device group. Phase 2 adds property reads (bgp.dut_ip, ipv4.address, etc.)."""

    def __init__(self, session: IxiaSession, dg: Any, topology_name: str = ""):
        self._session = session
        self._dg = dg
        self._topology_name = topology_name

    @property
    def name(self) -> str:
        return getattr(self._dg, "Name", "")

    @property
    def multiplier(self) -> int:
        return int(getattr(self._dg, "Multiplier", 1))

    @property
    def href(self) -> str:
        return getattr(self._dg, "href", "")

    @property
    def bgp(self) -> BgpProxy:
        """Access BGP peer properties on this DG."""
        return BgpProxy(self._session, self._dg)

    @property
    def ipv4(self) -> Ipv4Proxy:
        """Access IPv4 stack properties on this DG."""
        return Ipv4Proxy(self._session, self._dg)

    def add_network_group(
        self,
        name: str,
        ipv4_prefix: Optional[str] = None,
        ipv6_prefix: Optional[str] = None,
        count: int = 1,
    ) -> Any:
        """Create a network group under this DG."""
        try:
            ng = self._dg.NetworkGroup.add(Name=name, Multiplier=count)
            if ipv4_prefix:
                pool = ng.Ipv4PrefixPools.add()
                pool.NetworkAddress.Single(ipv4_prefix)
            if ipv6_prefix:
                pool = ng.Ipv6PrefixPools.add()
                pool.NetworkAddress.Single(ipv6_prefix)
            return ng
        except Exception as e:
            raise IxiaOperationError(f"Failed to add network group {name!r}: {e}") from e

    def start(self) -> None:
        """Start protocols on this DG only."""
        try:
            self._dg.Start()
        except Exception as e:
            raise IxiaOperationError(f"Failed to start DG {self.name!r}: {e}") from e

    def stop(self) -> None:
        """Stop protocols on this DG only."""
        try:
            self._dg.Stop()
        except Exception as e:
            raise IxiaOperationError(f"Failed to stop DG {self.name!r}: {e}") from e

    def __repr__(self) -> str:
        return f"DeviceGroupProxy({self.name!r}, multiplier={self.multiplier})"


class TopologyProxy:
    """Proxy for a topology that supports adding device groups."""

    def __init__(self, session: IxiaSession, topo: Any):
        self._session = session
        self._topo = topo

    @property
    def name(self) -> str:
        return getattr(self._topo, "Name", "")

    def add_device_group(self, name: str, multiplier: int = 1) -> DeviceGroupProxy:
        """Add a new device group to this topology."""
        try:
            dg = self._topo.DeviceGroup.add(Name=name, Multiplier=multiplier)
        except Exception as e:
            raise IxiaOperationError(
                f"Failed to add DG {name!r} to topology {self.name!r}: {e}"
            ) from e
        return DeviceGroupProxy(self._session, dg, topology_name=self.name)

    def delete(self) -> None:
        """Delete this topology from the session."""
        try:
            self._topo.remove()
        except Exception as e:
            raise IxiaOperationError(f"Failed to delete topology {self.name!r}: {e}") from e

    def __repr__(self) -> str:
        return f"TopologyProxy({self.name!r})"


class TopologyManager:
    """Topology and device group listing/search.

    Usage:
        s.topology.list()              # all topologies with DG names
        s.topology.find_dg("name")     # find specific DG across all topologies
    """

    def __init__(self, session: IxiaSession):
        self._session = session

    def list(self) -> list[TopologySummary]:
        """List all topologies with their ports and device group names."""
        ixn = self._session.ixn
        result = []
        for topo in ixn.Topology.find():
            ports = self._resolve_ports(ixn, topo)
            dg_names = self._collect_dg_names(topo)

            result.append(TopologySummary(
                name=topo.Name,
                ports=ports,
                device_groups=dg_names,
                href=getattr(topo, "href", ""),
            ))
        return result

    def create(self, name: str) -> TopologyProxy:
        """Create a new topology. Returns a proxy with .add_device_group()."""
        ixn = self._session.ixn
        try:
            topo = ixn.Topology.add(Name=name)
        except Exception as e:
            raise IxiaOperationError(f"Failed to create topology {name!r}: {e}") from e
        return TopologyProxy(self._session, topo)

    def scan(self) -> None:
        """Print full tree of topologies, device groups, and protocol stacks to terminal."""
        ixn = self._session.ixn
        for topo in ixn.Topology.find():
            print(f"Topology: {topo.Name}  href={topo.href}")
            self._scan_dgs(topo, indent="  ")

    def find_dg(self, name: str) -> Optional[DeviceGroupProxy]:
        """Find a device group by name across all topologies. Returns None if not found."""
        ixn = self._session.ixn
        for topo in ixn.Topology.find():
            dg = self._search_dg(topo, name)
            if dg is not None:
                return DeviceGroupProxy(self._session, dg, topology_name=topo.Name)
        return None

    def _scan_dgs(self, parent: Any, indent: str = "  ") -> None:
        """Recursively print DG tree with protocol stacks."""
        for dg in parent.DeviceGroup.find():
            mult = getattr(dg, "Multiplier", "?")
            print(f"{indent}DG: {dg.Name}  multiplier={mult}  href={dg.href}")
            for eth in dg.Ethernet.find():
                for ipv4 in eth.Ipv4.find():
                    addr = read_multivalue(ipv4.Address, self._session.ixn)
                    gw = read_multivalue(ipv4.GatewayIp, self._session.ixn)
                    print(f"{indent}  IPv4: addr={addr}  gw={gw}")
                    for bgp in ipv4.BgpIpv4Peer.find():
                        dut = read_multivalue(bgp.DutIp, self._session.ixn)
                        print(f"{indent}    BGP: {bgp.Name}  DutIp={dut}")
            self._scan_dgs(dg, indent + "  ")

    def _resolve_ports(self, ixn: Any, topo: Any) -> list[str]:
        """Resolve topology port refs to vport names.

        Topology.Ports may return vport hrefs rather than names.
        Falls back to raw ref strings if name resolution fails.
        """
        port_refs = getattr(topo, "Ports", [])
        if not port_refs:
            return []

        ports: list[str] = []
        try:
            vports = ixn.Vport.find()
        except Exception:
            return [str(ref) for ref in port_refs]

        vport_map: dict[str, str] = {}
        for v in vports:
            vport_map[getattr(v, "href", "")] = v.Name
            vport_map[v.Name] = v.Name

        for ref in port_refs:
            ref_str = str(ref)
            ports.append(vport_map.get(ref_str, ref_str))

        return ports

    def _search_dg(self, parent: Any, name: str) -> Any:
        """Recursively search for a DG by name in parent and its children."""
        for dg in parent.DeviceGroup.find():
            if dg.Name == name:
                return dg
            child = self._search_dg(dg, name)
            if child is not None:
                return child
        return None

    def _collect_dg_names(self, parent: Any) -> list[str]:
        """Recursively collect all DG names under a parent."""
        names = []
        for dg in parent.DeviceGroup.find():
            names.append(dg.Name)
            names.extend(self._collect_dg_names(dg))
        return names

    def __repr__(self) -> str:
        return "TopologyManager()"
