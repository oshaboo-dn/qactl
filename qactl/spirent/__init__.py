"""Spirent TestCenter support for qactl — the STC-REST traffic-generator group.

The sibling of :mod:`qactl.ixia`: where ``qactl ixia`` drives an IxNetwork
REST API server via ``ixnetwork-restpy``, ``qactl spirent`` drives a Spirent
TestCenter **REST server** (labserver) via the ``stcrestclient`` package —
plain HTTP, no containers, no OTG adapter.

Sub-packages (mirror ``qactl.ixia`` layer-for-layer):
  qactl.spirent.client  — low-level STC REST session (wraps ``stcrestclient``)
  qactl.spirent.core    — response envelope + reattach-first session cache
  qactl.spirent.tools   — high-level tool ops returning envelopes
  qactl.spirent.ctl     — the argparse CLI front (``qactl spirent ...``)

Status: **scaffold** (2026-07-16). Only the session/connection surface is
wired (``session connect`` / ``sessions`` / ``describe`` / ``info``); ports,
config-load, traffic, and protocol authoring land once the physical Spirent
port is cabled. See ``qactl/spirent/README.md``.
"""
