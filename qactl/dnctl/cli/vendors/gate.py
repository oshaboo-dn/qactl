"""Capability gate: turn an unsupported (vendor, tool) into a clean error.

A tool function decorated with :func:`requires(capability)` checks the
target device's vendor before running: if that vendor's plugin doesn't
support the capability, the tool short-circuits with a structured
``not implemented`` envelope (non-zero exit) instead of trying to drive
a DNOS-shaped exchange against a Cisco / Juniper box. DNOS supports
every capability, so DNOS tools are never gated.

The decorator preserves the wrapped function's signature (via
``functools.wraps``) so both the Typer CLI (``O.call`` inspects the
signature) and FastMCP (``mcp.tool()`` introspects it) keep working
unchanged. It reads ``device`` / ``host`` from the call's keyword
arguments — every surface invokes tools by keyword.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Dict, Optional

from qactl.dnctl.cli.core.envelope import make_response
from qactl.dnctl.cli.core.logging import log_request
from qactl.dnctl.cli.vendors.registry import plugin_for_device


def unsupported_response(
    vendor: str,
    capability: str,
    device: Optional[str],
    host: Optional[str],
    command: str = "",
) -> Dict[str, Any]:
    """Structured ``not implemented for this vendor`` envelope (status=error)."""
    return make_response(
        status="error",
        device=device,
        host=host or "",
        command=command,
        errors=[
            f"'{capability}' is not implemented for vendor {vendor!r}. "
            f"cli-mcp currently supports only read-only 'show' on "
            f"{vendor} devices; the full tool surface is DNOS-only."
        ],
        next_actions=[
            f"Run a read-only `qactl cli show '<command>' -d <device>` on this "
            f"{vendor} device, or target a DNOS device for this operation."
        ],
        unsupported=True,
        vendor=vendor,
        capability=capability,
    )


def requires(capability: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a tool so it only runs when the device's vendor supports it.

    On an unsupported (vendor, capability) pair the wrapped tool returns
    the :func:`unsupported_response` envelope without touching the device.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            device = kwargs.get("device")
            host = kwargs.get("host")
            plugin = plugin_for_device(device, host)
            if not plugin.supports(capability):
                response = unsupported_response(
                    plugin.name, capability, device, host,
                    command=str(kwargs.get("command") or ""),
                )
                log_request(
                    fn.__name__,
                    {"device": device, "host": host, "capability": capability},
                    response,
                )
                return response
            return fn(*args, **kwargs)

        return wrapper

    return decorator
