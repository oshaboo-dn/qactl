"""``qactl d42 ...`` — read-only Device42 CMDB lookups.

Device42 is the lab's authoritative CMDB; this group reads it live so the
hostname migration to ``{Site}{NN}-{ROLE}-{RACK}`` can't leave us on stale
cached names. Every lookup takes a device **name or serial**. All commands
are read-only, so nothing here takes the ``--yes`` gate.

Not covered yet: serial-console lookup — the Device42 console-port data is
free-text and inconsistent (the console tool parses it with a strict regex
and falls back to a manually maintained CSV), so it needs a careful follow-up.
"""

from __future__ import annotations

import argparse

from qactl.core.output import emit
from qactl.device42 import tools


def _device(args):
    return emit(tools.d42_device(args.query), as_json=args.json)


def _rack(args):
    return emit(tools.d42_rack(args.query), as_json=args.json)


def _power(args):
    return emit(tools.d42_power(args.query), as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser(
        "d42", help="Device42 CMDB (read-only: device inventory / rack lookup)")
    sub = grp.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("device", parents=[parent],
                       help="device inventory + owner, by name or serial")
    d.add_argument("query", help="device name or serial (e.g. WDY1A17P0001A or a hostname)")
    d.set_defaults(func=_device)

    r = sub.add_parser("rack", parents=[parent],
                       help="physical placement: rack / row / room / building / U")
    r.add_argument("query", help="device name or serial")
    r.set_defaults(func=_rack)

    p = sub.add_parser("power", parents=[parent],
                       help="PDU power feed(s): pdu / outlet / model (read-only)")
    p.add_argument("query", help="device name or serial")
    p.set_defaults(func=_power)
