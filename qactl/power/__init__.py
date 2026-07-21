"""``qactl power`` — PDU outlet control (status / on / off / cycle).

A device action, not a CMDB read: the PDU + outlet come from Device42 behind
the scenes (or given manually), then the outlet is switched on the PDU over
SSH. Powering an outlet off/on/cycle is destructive and gated behind ``--yes``;
``status`` is read-only.
"""
