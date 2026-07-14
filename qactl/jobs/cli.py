"""``qactl jobs ...`` — list / inspect every async job in one place.

Thin argparse front over :mod:`qactl.jobs.tools`. Read-only (no ``--yes``):

    qactl jobs list [--kind K] [--status S] [-d DEV] [--limit N]
    qactl jobs show [job_id] [-d DEV] [--kind K]
"""

from __future__ import annotations

import argparse

from qactl.core.output import emit
from qactl.jobs import tools

# Family labels a user can pass to --kind (kept in sync via JOB_FAMILIES).
from qactl.dnos.cli.core.job_store import JOB_FAMILIES

_KINDS = sorted(set(JOB_FAMILIES.values()))


def _list(args):
    return emit(tools.jobs_list(
        kind=args.kind, status=args.status, device=args.device, limit=args.limit,
    ), as_json=args.json)


def _show(args):
    return emit(tools.jobs_show(
        job_id=args.job_id, device=args.device, kind=args.kind,
    ), as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser(
        "jobs", help="list / inspect async jobs (tarload / techsupport / orc)")
    sub = grp.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list", parents=[parent],
                       help="list persisted jobs across families, newest-first")
    l.add_argument("--kind", default=None, metavar="{" + ",".join(_KINDS) + "}",
                   help="restrict to one family (default: all)")
    l.add_argument("--status", default=None,
                   help="only jobs with this status (ok / running / error / timeout / ...)")
    l.add_argument("-d", "--device", default=None, help="only jobs on this device")
    l.add_argument("--limit", type=int, default=50,
                   help="max rows to return (0 = unlimited; default: 50)")
    l.set_defaults(func=_list)

    s = sub.add_parser("show", parents=[parent],
                       help="full envelope for one job (by id, or latest with -d)")
    s.add_argument("job_id", nargs="?", default=None, help="job id (or use -d)")
    s.add_argument("-d", "--device", default=None,
                   help="look up the latest job on this device")
    s.add_argument("--kind", default=None, metavar="{" + ",".join(_KINDS) + "}",
                   help="restrict the lookup to one family")
    s.set_defaults(func=_show)
