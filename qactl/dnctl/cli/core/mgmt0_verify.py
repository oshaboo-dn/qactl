"""Live mgmt0 verification for northbound sessions (issue #71).

The canonical map's cached ``mgmt0`` can silently go stale: the IP gets
re-assigned to another box that still answers NETCONF/gNMI, so the
northbound session opens "fine" while talking to the wrong chassis
(observed on Hybrid-CL, SW-277917). SN verification inside the NETCONF
session didn't catch it — the wrong box answered, and an empty
``expected_sns`` was auto-adopted from it.

:func:`verify_device_mgmt0` closes that hole: it asks the chassis itself
for its CURRENT mgmt0 over the CLI transport pool (``show interfaces
management`` via the ``expected_sns`` SSH hosts, which resolve
independently of the cached IP), compares it to the cached value, and on
mismatch refreshes the map and hands back the live address. nc / gnmi /
rc call this before opening a session — the cli group still owns every
SSH interaction; those groups never touch the wire themselves. The probe
authenticates exactly like ``dnctl cli -d <device>`` does: creds resolve
through :func:`~dnctl.core.credentials.resolve_device_credentials`
(per-device > per-vendor > global) keyed on the canonical alias.

Verification is mandatory by default: when the chassis can't confirm the
address, callers must refuse to proceed (:func:`require_verified`) —
connecting to an unverified cached IP is the exact failure mode this
module exists to prevent. ``--no-verify-mgmt0`` is the explicit opt-out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from qactl.dnctl.core import devices as _devices
from qactl.dnctl.core.cli_probe import parse_mgmt0_ipv4
from qactl.dnctl.core.credentials import resolve_device_credentials

from .registry import transport_registry
from .session import (
    DEFAULT_CMD_TIMEOUT,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    reload_device_hosts,
    run_once,
)


class Mgmt0UnverifiedError(RuntimeError):
    """The chassis could not confirm the cached mgmt0 and the caller
    refuses to connect to it unverified (issue #71)."""


@dataclass
class Mgmt0Verification:
    """Outcome of a live mgmt0 check for one device.

    ``address`` is what the caller should connect to: the live mgmt0 when
    the chassis answered, otherwise the cached one. ``verified`` means the
    chassis itself confirmed ``address``; ``refreshed`` means the canonical
    map was rewritten because the cached value was stale. ``warnings`` are
    envelope-ready strings (stale-refresh notice, unverified notice, ...).
    """

    device: str
    address: Optional[str]
    cached: Optional[str]
    live: Optional[str] = None
    probed_host: Optional[str] = None
    verified: bool = False
    refreshed: bool = False
    warnings: List[str] = field(default_factory=list)


# Per-process memo so a burst of northbound calls against the same device
# (e.g. gnmi get_many looping one Get per path) pays for ONE CLI probe, not
# one per call. Keyed by (canonical alias, map file); entries expire after
# ``ttl`` seconds so long-running processes still re-verify.
_recent: Dict[Tuple[str, Optional[str]], Tuple[float, Mgmt0Verification]] = {}


def verify_device_mgmt0(
    device: str,
    *,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: float = DEFAULT_CMD_TIMEOUT,
    map_file: Optional[str] = None,
    ttl: float = 60.0,
) -> Mgmt0Verification:
    """Resolve the device's CURRENT mgmt0 from the chassis and reconcile the map.

    SSH-probes each ``expected_sns`` host in turn (they resolve via DNS
    independently of the cached IP) and parses ``show interfaces
    management``. First host that yields a parseable mgmt0 wins. Creds
    resolve exactly like a ``dnctl cli -d <device>`` call: per-device >
    per-vendor > global, keyed on the canonical alias (issue #71 — the
    probe must authenticate with the same creds the cli group uses).

    - live == cached: ``verified=True``, nothing written.
    - live != cached: the map's ``mgmt0`` is refreshed to the live value,
      ``address`` becomes the live value, and a warning names both.
    - no SN host answered / none recorded: ``verified=False`` and
      ``address`` stays the cached value with an UNVERIFIED warning —
      callers should refuse to connect (:func:`require_verified`).

    Never raises for reachability/parse problems; only truly unexpected
    errors (e.g. a corrupt map write) surface as warnings too.

    A verification is memoized per process for ``ttl`` seconds (pass
    ``ttl=0`` to force a fresh probe).
    """
    canonical = _devices.resolve_canonical(device, map_file) or device
    memo_key = (canonical, map_file)
    if ttl > 0:
        hit = _recent.get(memo_key)
        if hit and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
    outcome = _verify_uncached(
        canonical, device, user=user, password=password,
        timeout=timeout, map_file=map_file,
    )
    _recent[memo_key] = (time.monotonic(), outcome)
    return outcome


def _verify_uncached(
    canonical: str,
    device: str,
    *,
    user: str,
    password: str,
    timeout: float,
    map_file: Optional[str],
) -> Mgmt0Verification:
    entry = _devices.get_device_entry(canonical, map_file) or {}
    cached = entry.get("mgmt0") if isinstance(entry.get("mgmt0"), str) else None
    outcome = Mgmt0Verification(device=canonical, address=cached, cached=cached)

    if not entry:
        outcome.warnings.append(
            f"device '{device}' is not registered in the canonical map; "
            f"cannot verify its mgmt0 via CLI."
        )
        return outcome

    raw_sns = entry.get("expected_sns")
    sns = (
        [s.strip() for s in raw_sns if isinstance(s, str) and s.strip()]
        if isinstance(raw_sns, list) else []
    )
    if not sns:
        outcome.warnings.append(
            f"cannot verify cached mgmt0={cached!r} for '{canonical}' — no "
            f"expected_sns recorded; run `dnctl cli device add {canonical} "
            f"--sn <ssh-host>` so the chassis can be CLI-probed. "
            f"The cached address is UNVERIFIED."
        )
        return outcome

    # Same cred layering a `dnctl cli -d <device>` session gets (per-device
    # > per-vendor > global), keyed on the canonical alias. run_once below
    # is called host-only, so it can't do this lookup itself (issue #71:
    # the probe used to fall through to empty creds and fail auth while
    # the cli group authenticated fine).
    user, password = resolve_device_credentials(canonical, user, password)

    failures: List[str] = []
    for candidate in sns:
        try:
            inv = run_once(
                registry=transport_registry,
                device=None, host=candidate, user=user, password=password,
                command="show interfaces management", timeout=timeout,
            )
            live = parse_mgmt0_ipv4(inv.output)
        except Exception as exc:  # noqa: BLE001 - try the next SN host
            failures.append(f"{candidate}: {type(exc).__name__}: {exc}")
            continue
        if live:
            outcome.live = live
            outcome.probed_host = candidate
            break
        failures.append(
            f"{candidate}: `show interfaces management` yielded no "
            f"parseable mgmt0 IPv4"
        )

    if outcome.live is None:
        outcome.warnings.append(
            f"could not verify cached mgmt0={cached!r} for '{canonical}' — "
            f"CLI probe of {sns} failed ({'; '.join(failures)}). "
            f"The cached address is UNVERIFIED."
        )
        return outcome

    if outcome.live == cached:
        outcome.verified = True
        return outcome

    # Stale (or missing) cached mgmt0 — adopt the chassis-reported address.
    try:
        _devices.update_device(canonical, map_file, mgmt0=outcome.live)
        reload_device_hosts()
        outcome.refreshed = True
    except Exception as exc:  # noqa: BLE001 - still use the live address
        outcome.warnings.append(
            f"failed to write refreshed mgmt0 to the canonical map: "
            f"{type(exc).__name__}: {exc}"
        )
    outcome.address = outcome.live
    outcome.verified = True
    outcome.warnings.append(
        f"cached mgmt0={cached!r} for '{canonical}' is stale — the chassis "
        f"(CLI `show interfaces management` via {outcome.probed_host}) "
        f"reports mgmt0={outcome.live!r}; "
        + ("registry refreshed, " if outcome.refreshed else "")
        + f"connecting to {outcome.live!r}."
    )
    return outcome


def require_verified(outcome: Mgmt0Verification) -> Mgmt0Verification:
    """Fail-hard gate: return ``outcome`` only if the chassis confirmed it.

    An unverified cached mgmt0 is exactly the failure mode issue #71 is
    about (the IP re-assigned to another box that still answers), so
    warn-and-proceed is not an option for the northbound groups. Raises
    :class:`Mgmt0UnverifiedError` carrying the probe's own diagnostics and
    the two ways out: fix the probe, or opt out explicitly.
    """
    if outcome.verified:
        return outcome
    detail = " ".join(outcome.warnings) or "the CLI probe produced no result."
    raise Mgmt0UnverifiedError(
        f"refusing to connect to unverified mgmt0={outcome.address!r} for "
        f"'{outcome.device}': {detail} Fix the CLI probe (device creds via "
        f"`qactl setup` / SN hosts via `dnctl cli device add`) or pass "
        f"--no-verify-mgmt0 to use the cached address as-is."
    )


__all__ = [
    "Mgmt0UnverifiedError",
    "Mgmt0Verification",
    "require_verified",
    "verify_device_mgmt0",
]
