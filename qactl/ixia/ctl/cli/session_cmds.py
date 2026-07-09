"""``qactl.ixia.ctl session ...`` — session + config lifecycle.

connect / sessions / describe / chassis / vports / wait-vports / configs /
new / load / save / apply / clear-stats.

Tool functions are imported lazily inside each handler so that building
the parser (and ``--help``) never needs ``ixnetwork-restpy`` installed.
"""

from __future__ import annotations

import argparse

from qactl.ixia.ctl.core.output import emit
from qactl.ixia.ctl.cli.common import confirm_or_exit, primary_timeout


def _wait_ms(args: argparse.Namespace, default_ms: int) -> int:
    """Resolve a wait deadline in ms: ``--timeout`` (s) wins, else ms flag."""
    if getattr(args, "timeout", None) is not None:
        return int(args.timeout) * 1000
    return int(getattr(args, "wait_timeout_ms", default_ms))


# Windows lab default — same default the ixia_load_config / list_configs
# tools use. Not a clone path; safe to bake in as a convenience default.
DEFAULT_CONFIG_FOLDER = r"C:\Users\dn\Desktop\ixia"


def _is_bare_filename(path: str) -> bool:
    """True if ``path`` is a plain filename with no directory component.

    The paths here are Windows paths handled on a (Linux) client, so we
    can't lean on ``os.path``. A bare name has no path separator
    (``\\`` or ``/``) and no drive / UNC qualifier (``:`` or a leading
    ``\\\\``). Anything else is treated as already-qualified and passed
    through untouched.
    """
    if not path:
        return False
    return not ("\\" in path or "/" in path or ":" in path)


def _resolve_config_path(path: str, folder: str) -> str:
    """Resolve a bare config name against ``folder``; pass paths through.

    Mirrors what ``session configs`` lists: a name copied straight out of
    ``configs`` resolves against the same folder it was listed from,
    instead of IxNetwork's own default config dir.
    """
    if _is_bare_filename(path):
        return f"{folder.rstrip(chr(92))}\\{path}"
    return path


def _connect(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.diag import ixia_connect_check
    env = ixia_connect_check(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _sessions(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.diag import ixia_list_sessions
    env = ixia_list_sessions(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _describe(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.inspect import (
        DEFAULT_DESCRIBE_TIMEOUT_S, ixia_describe_session,
    )
    env = ixia_describe_session(
        host=args.host, port=args.port, user=args.user,
        include_route_counts=not args.no_route_counts,
        include_traffic=not args.no_traffic,
        timeout_s=primary_timeout(args, DEFAULT_DESCRIBE_TIMEOUT_S),
    )
    return emit(env, as_json=args.json)


def _chassis(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.topology import ixia_list_chassis
    env = ixia_list_chassis(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _vports(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.topology import ixia_list_vports
    env = ixia_list_vports(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def _wait_vports(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.run import ixia_wait_vports_ready
    timeout_ms = args.timeout_ms
    if args.timeout is not None:
        timeout_ms = int(args.timeout) * 1000
    env = ixia_wait_vports_ready(
        host=args.host, port=args.port, user=args.user,
        timeout_ms=timeout_ms,
        only_vport_names=args.only_vport_name or None,
        only_vport_hrefs=args.only_vport_href or None,
    )
    return emit(env, as_json=args.json)


def _assign(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="assign_port",
        action=(
            f"assign chassis port {args.port_spec} to a vport"
            + (" and connect it" if args.connect else "")
            + (" (forcing ownership)" if args.force else "")
            + "."
        ),
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.ports import DEFAULT_CONNECT_WAIT_MS, ixia_assign_port
    env = ixia_assign_port(
        host=args.host, port=args.port, user=args.user,
        port_spec=args.port_spec, name=args.name,
        connect=args.connect, wait=args.wait,
        wait_timeout_ms=_wait_ms(args, DEFAULT_CONNECT_WAIT_MS),
        force=args.force, confirm=True,
    )
    return emit(env, as_json=args.json)


def _connect_ports(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="connect_ports",
        action=f"connect vport {args.vport!r}.",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.ports import DEFAULT_CONNECT_WAIT_MS, ixia_connect_ports
    env = ixia_connect_ports(
        host=args.host, port=args.port, user=args.user,
        vport=args.vport, wait=args.wait,
        wait_timeout_ms=_wait_ms(args, DEFAULT_CONNECT_WAIT_MS),
        confirm=True,
    )
    return emit(env, as_json=args.json)


def _release(args: argparse.Namespace) -> int:
    target = args.port_spec or args.vport or "?"
    rc = confirm_or_exit(
        args, kind="release_port",
        action=(
            f"release {target}"
            + (" and delete its vport" if args.delete else "")
            + "."
        ),
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.ports import ixia_release_port
    env = ixia_release_port(
        host=args.host, port=args.port, user=args.user,
        port_spec=args.port_spec, vport=args.vport,
        delete=args.delete, confirm=True,
    )
    return emit(env, as_json=args.json)


def _configs(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.config import ixia_list_configs
    env = ixia_list_configs(
        host=args.host, folder=args.folder,
        ssh_alias=args.ssh_alias,
        timeout_s=primary_timeout(args, 10),
    )
    return emit(env, as_json=args.json)


def _new(args: argparse.Namespace) -> int:
    rc = confirm_or_exit(
        args, kind="new_config",
        action="ixia new config: wipes ALL topologies / DGs / traffic / "
               "stat views in the current session.",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.config import ixia_new_config
    env = ixia_new_config(
        host=args.host, port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


def _load(args: argparse.Namespace) -> int:
    server_path = _resolve_config_path(args.file, args.folder)
    rc = confirm_or_exit(
        args, kind="load_config",
        action=f"load config {server_path!r}: overwrites the current session "
               "config (vport ownership preserved).",
    )
    if rc is not None:
        return rc
    from qactl.ixia.tools.config import ixia_load_config
    env = ixia_load_config(
        host=args.host, server_path=server_path,
        port=args.port, user=args.user, confirm=True,
        wait_for_vports_ms=args.wait_for_vports_ms,
    )
    return emit(env, as_json=args.json)


def _save(args: argparse.Namespace) -> int:
    # Save is not in the spec's --yes list (it writes a file, doesn't tear
    # down session state) — proceed without a gate, passing confirm=True.
    from qactl.ixia.tools.config import ixia_save_config
    env = ixia_save_config(
        host=args.host, server_path=args.file,
        port=args.port, user=args.user, confirm=True,
    )
    return emit(env, as_json=args.json)


def _apply(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.run import ixia_apply_changes
    env = ixia_apply_changes(
        host=args.host, port=args.port, user=args.user,
        timeout_s=primary_timeout(args, 60),
    )
    return emit(env, as_json=args.json)


def _clear_stats(args: argparse.Namespace) -> int:
    from qactl.ixia.tools.run import ixia_clear_stats
    env = ixia_clear_stats(host=args.host, port=args.port, user=args.user)
    return emit(env, as_json=args.json)


def register(subparsers, parent: argparse.ArgumentParser) -> None:
    grp = subparsers.add_parser(
        "session", help="session + config lifecycle",
    )
    sub = grp.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "connect", parents=[parent],
        help="cheap reachability probe; prints the session id to reuse",
    ).set_defaults(func=_connect)

    sub.add_parser(
        "sessions", parents=[parent],
        help="list IxNetwork sessions on the API server",
    ).set_defaults(func=_sessions)

    d = sub.add_parser(
        "describe", parents=[parent],
        help="one-call snapshot of the whole session",
    )
    d.add_argument("--no-route-counts", action="store_true",
                   help="skip per-peer cumulative route counts (faster; "
                        "the route-count stat view is the usual hang)")
    d.add_argument("--no-traffic", action="store_true",
                   help="omit the traffic-item summary")
    d.set_defaults(func=_describe)

    sub.add_parser(
        "chassis", parents=[parent],
        help="list chassis + physical ports the API server knows",
    ).set_defaults(func=_chassis)

    sub.add_parser(
        "vports", parents=[parent], help="list virtual ports",
    ).set_defaults(func=_vports)

    w = sub.add_parser(
        "wait-vports", parents=[parent],
        help="block until assigned vports are connectedLinkUp + up",
    )
    w.add_argument("--timeout-ms", type=int, default=60_000,
                   help="hard deadline in ms (default 60000)")
    w.add_argument("--only-vport-name", action="append", default=[],
                   metavar="NAME", help="restrict wait to these vport names")
    w.add_argument("--only-vport-href", action="append", default=[],
                   metavar="HREF", help="restrict wait to these vport hrefs")
    w.set_defaults(func=_wait_vports)

    a = sub.add_parser(
        "assign", parents=[parent],
        help="claim chassis:card:port onto a vport (create/rebind + connect)",
    )
    a.add_argument("--chassis-port", dest="port_spec", required=True,
                   metavar="CHASSIS:CARD:PORT",
                   help="physical port to claim, e.g. 100.64.0.56:10:5")
    a.add_argument("--name", default=None,
                   help="vport name to create or rebind (default derived "
                        "from the port spec)")
    a.add_argument("--no-connect", dest="connect", action="store_false",
                   help="assign the location only; skip ConnectPorts")
    a.add_argument("--wait", action="store_true",
                   help="after connect, block until connectedLinkUp + up")
    a.add_argument("--wait-timeout-ms", type=int, default=60_000,
                   help="deadline for --wait in ms (default 60000; "
                        "--timeout SECONDS overrides)")
    a.add_argument("--force", action="store_true",
                   help="seize the port even if owned by another client")
    a.set_defaults(func=_assign, connect=True)

    cp = sub.add_parser(
        "connect-ports", parents=[parent],
        help="ConnectPort an already-assigned vport (by name or href)",
    )
    cp.add_argument("--vport", required=True, metavar="NAME|HREF",
                    help="vport to connect")
    cp.add_argument("--wait", action="store_true",
                    help="block until connectedLinkUp + up")
    cp.add_argument("--wait-timeout-ms", type=int, default=60_000,
                    help="deadline for --wait in ms (default 60000; "
                         "--timeout SECONDS overrides)")
    cp.set_defaults(func=_connect_ports)

    r = sub.add_parser(
        "release", parents=[parent],
        help="drop ownership of a port (optionally delete its vport)",
    )
    r.add_argument("--chassis-port", dest="port_spec", default=None,
                   metavar="CHASSIS:CARD:PORT",
                   help="physical port to release (matched via AssignedTo)")
    r.add_argument("--vport", default=None, metavar="NAME|HREF",
                   help="vport to release (alternative to --port)")
    r.add_argument("--delete", action="store_true",
                   help="also delete the vport object")
    r.set_defaults(func=_release)

    c = sub.add_parser(
        "configs", parents=[parent],
        help="list .ixncfg files on the API server (via SSH)",
    )
    c.add_argument("--folder", default=DEFAULT_CONFIG_FOLDER,
                   help=f"Windows folder to enumerate (default "
                        f"{DEFAULT_CONFIG_FOLDER})")
    c.add_argument("--ssh-alias", default=None,
                   help="SSH target if it differs from --host")
    c.set_defaults(func=_configs)

    sub.add_parser(
        "new", parents=[parent],
        help="clear the current session config (--yes; wipes current)",
    ).set_defaults(func=_new)

    ld = sub.add_parser(
        "load", parents=[parent],
        help="load an .ixncfg from the API server (--yes; wipes current)",
    )
    ld.add_argument(
        "file",
        help="config to load on the API server: an absolute path, or a "
             "bare filename from `session configs` (resolved against "
             "--folder)",
    )
    ld.add_argument("--folder", default=DEFAULT_CONFIG_FOLDER,
                    help=f"folder a bare filename is resolved against "
                         f"(default {DEFAULT_CONFIG_FOLDER}; must match "
                         f"`session configs --folder`). Ignored when `file` "
                         f"is already a path.")
    ld.add_argument("--wait-for-vports-ms", type=int, default=60_000,
                    help="post-load vport-readiness wait in ms (default "
                         "60000; 0 disables)")
    ld.set_defaults(func=_load)

    sv = sub.add_parser(
        "save", parents=[parent],
        help="save the session config to the API server",
    )
    sv.add_argument("file", help="absolute path on the API server filesystem")
    sv.set_defaults(func=_save)

    sub.add_parser(
        "apply", parents=[parent],
        help="push pending NGPF edits (Apply Changes)",
    ).set_defaults(func=_apply)

    sub.add_parser(
        "clear-stats", parents=[parent], help="clear all statistics counters",
    ).set_defaults(func=_clear_stats)
