"""Minimal YANG cache for the NETCONF MCP.

The MCP does not generate or validate XML. This package only probes the
device's DNOS version and downloads the raw ``.yang`` modules advertised
by ``ietf-yang-library`` into ``yangs/<build>/<sub_build>/`` so the agent
can read them when it needs to.

Modules:

- :mod:`yang.bootstrap`   -- ``<get-schema>`` bulk fetch + ``_metadata.json``
- :mod:`yang._yang_io`    -- path + cached reader helpers
- :mod:`yang.send`        -- ``resolve_build_and_bootstrap`` (probe version + run bootstrap, cached per device)
"""
