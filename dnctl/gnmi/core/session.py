"""gNMI session / connection helpers.

Mirrors the spirit of `netconf-mcp/dnctl.nc.core/session.py` but adapted for
gRPC: pygnmi's ``gNMIclient`` is itself a context manager so we don't pool
TCP sessions here — we just translate the agent's input
(``device``/``host``/``user``/``password``/``tls_mode``) into the right
constructor arguments. Every connection uses the single lab account
(``DEFAULT_USER`` / ``DEFAULT_PASSWORD``); there is no auth-failure fallback.

TLS modes:

- ``insecure``    — plaintext gRPC, ``insecure=True``. Tested on `cl`.
- ``skip_verify`` — TLS with ``skip_verify=True``. Tested on `sa` (server
                    cert without SAN; client validates nothing).
- ``verify_ca``   — TLS with ``path_cert=<ca.pem>``. Pinned CA only, no
                    client cert. (Future, once we have a sa CA bundle.)
- ``mtls``        — full mTLS: ``path_cert=<ca>``, ``path_key=<client.key>``,
                    ``override=<client.crt>``. Requires all three files.
                    (Future.)

Device map I/O is delegated to ``dnctl.core.devices`` — the canonical map
lives at ``<repo-root>/devices/devices_mgmt0.json`` and is shared with
every other MCP in the monorepo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pygnmi.client import gNMIclient

from dnctl.core import credentials as _creds
from dnctl.core import devices as _devices


DEFAULT_USER = _creds.DEFAULT_USER
DEFAULT_PASSWORD = _creds.DEFAULT_PASSWORD
DEFAULT_PORT = 50051
DEFAULT_TIMEOUT_S = 15

_GRPC_OPTIONS = [
    ("grpc.max_receive_message_length", 32 * 1024 * 1024),
]

VALID_TLS_MODES = ("insecure", "skip_verify", "verify_ca", "mtls")


@dataclass
class Resolved:
    host: str
    port: int
    device: Optional[str] = None


def resolve_host(device: Optional[str], host: Optional[str]) -> Resolved:
    """Resolve a device alias to mgmt0; or pass a host through verbatim."""
    if host and not device:
        return Resolved(host=host, port=DEFAULT_PORT)
    if not device:
        raise ValueError("Provide device= or host=")
    entry = _devices.get_device_entry(device)
    if entry is None:
        raise ValueError(
            f"Unknown device alias '{device}' in "
            f"{_devices.default_device_map_path()}"
        )
    mgmt0 = _devices.resolve_mgmt0(device)
    if not mgmt0:
        raise ValueError(
            f"Device '{device}' has no mgmt0 in "
            f"{_devices.default_device_map_path()}"
        )
    return Resolved(host=mgmt0, port=DEFAULT_PORT, device=device)


def _client_kwargs(
    *,
    target: tuple,
    user: str,
    password: str,
    tls_mode: str,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    ca_file: Optional[str] = None,
) -> dict:
    """Render the kwargs for ``gNMIclient(...)`` for a given TLS mode."""
    kw: dict = {
        "target": target,
        "username": user,
        "password": password,
        "grpc_options": _GRPC_OPTIONS,
        "debug": False,
    }
    if tls_mode == "insecure":
        kw["insecure"] = True
    elif tls_mode == "skip_verify":
        kw["skip_verify"] = True
    elif tls_mode == "verify_ca":
        if not ca_file:
            raise ValueError("tls_mode=verify_ca requires ca_file")
        kw["path_cert"] = ca_file
    elif tls_mode == "mtls":
        if not (ca_file and cert_file and key_file):
            raise ValueError(
                "tls_mode=mtls requires ca_file + cert_file + key_file"
            )
        kw["path_cert"] = ca_file
        kw["override"] = cert_file
        kw["path_key"] = key_file
    else:
        raise ValueError(
            f"Unknown tls_mode={tls_mode!r}; valid: {VALID_TLS_MODES}"
        )
    return kw


def open_client(
    *,
    device: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    tls_mode: str = "insecure",
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    ca_file: Optional[str] = None,
) -> tuple[gNMIclient, Resolved, str]:
    """Open a gNMIclient with the single lab account.

    Returns (client, resolved, final_user). The caller is responsible
    for ``client.__enter__()`` / ``__exit__`` since some paths need to
    inspect attributes before opening the channel.
    """
    resolved = resolve_host(device, host)
    target = (resolved.host, port or resolved.port)
    final_user = user if user is not None else DEFAULT_USER
    final_pw = password if password is not None else DEFAULT_PASSWORD

    base_kwargs = _client_kwargs(
        target=target, user=final_user, password=final_pw,
        tls_mode=tls_mode, cert_file=cert_file, key_file=key_file,
        ca_file=ca_file,
    )
    client = gNMIclient(**base_kwargs)
    return client, resolved, final_user
