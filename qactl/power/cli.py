"""``qactl power {status|on|off|cycle} [<device>] [--pdu H --outlet N]``.

Resolves a device name/serial to its PDU outlet(s) via Device42 (a dual-PSU
box has two — all are acted on), or targets one outlet with ``--pdu``/
``--outlet``. ``status`` is read-only; ``on`` / ``off`` / ``cycle`` switch
power and are gated behind ``--yes``.
"""

from __future__ import annotations

import argparse

from qactl.core.envelope import error_envelope
from qactl.core.output import emit
from qactl.power import tools


def _needs_yes(args) -> bool:
    return not getattr(args, "yes", False)


def _confirm_required(kind: str, what: str):
    return error_envelope(
        f"{what} is destructive — re-run with --yes to confirm.",
        kind=kind, status="confirmation_required")


def _status(args):
    return emit(tools.power_status(args.query, pdu=args.pdu, outlet=args.outlet),
                as_json=args.json)


def _on(args):
    if _needs_yes(args):
        return emit(_confirm_required("power_on", "powering the outlet ON"),
                    as_json=args.json)
    return emit(tools.power_set(True, args.query, pdu=args.pdu, outlet=args.outlet),
                as_json=args.json)


def _off(args):
    if _needs_yes(args):
        return emit(_confirm_required("power_off", "powering the outlet OFF"),
                    as_json=args.json)
    return emit(tools.power_set(False, args.query, pdu=args.pdu, outlet=args.outlet),
                as_json=args.json)


def _cycle(args):
    if _needs_yes(args):
        return emit(_confirm_required("power_cycle", "power-cycling the device"),
                    as_json=args.json)
    return emit(tools.power_cycle(args.query, pdu=args.pdu, outlet=args.outlet),
                as_json=args.json)


def _add_target(p: argparse.ArgumentParser) -> None:
    p.add_argument("query", nargs="?", help="device name or serial (PDU/outlet "
                                            "resolved from Device42)")
    p.add_argument("--pdu", help="PDU host (manual; e.g. RA01-PDU-B10-1) — "
                                 "bypass the Device42 lookup")
    p.add_argument("--outlet", type=int, help="outlet number (manual)")


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser(
        "power", help="PDU outlet control: status / on / off / cycle")
    sub = grp.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", parents=[parent],
                       help="outlet on/off state (read-only)")
    _add_target(s); s.set_defaults(func=_status)

    on = sub.add_parser("on", parents=[parent], help="power the outlet(s) ON (--yes)")
    _add_target(on); on.set_defaults(func=_on)

    off = sub.add_parser("off", parents=[parent], help="power the outlet(s) OFF (--yes)")
    _add_target(off); off.set_defaults(func=_off)

    cy = sub.add_parser("cycle", parents=[parent],
                        help="power-cycle: off -> pause -> on, all feeds (--yes)")
    _add_target(cy); cy.set_defaults(func=_cycle)
