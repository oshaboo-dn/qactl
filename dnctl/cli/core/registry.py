"""Process-wide SSH transport pool for the CLI MCP server.

One ``TransportRegistry`` instance is shared by every tool that talks to
a device — it caches paramiko transports keyed by ``(device_or_host,
user)`` so we only re-auth on the first call per pair (and after idle
reaping). The ``atexit`` hook below closes any cached transports cleanly
when the server process exits.

Importers should refer to ``transport_registry`` (the singleton);
constructing a second ``TransportRegistry`` would break the caching
contract.
"""

from __future__ import annotations

import atexit

from dnctl.cli.core.session import TransportRegistry


transport_registry: TransportRegistry = TransportRegistry()
atexit.register(transport_registry.close_all)
