"""`dnctl gnmi ...` — gNMI subcommand group (from user-gnmi-mcp).

Thin Typer front-end over the lifted gNMI tool functions in
:mod:`dnctl.gnmi.tools`. Defaults match what DNOS actually accepts
(``encoding=json``, ``datatype=all``, ``tls_mode=insecure``) so the
agent's existing mental model carries over unchanged.
"""

from __future__ import annotations

import json as _json
from typing import Annotated, Any, List, Optional, Tuple

import typer

from qactl.dnctl.core import options as O
from qactl.dnctl.core import confirm
from qactl.dnctl.gnmi.tools.devices import gnmi_list_devices
from qactl.dnctl.gnmi.tools.diag import gnmi_capabilities, gnmi_ping
from qactl.dnctl.gnmi.tools.rw import (
    gnmi_enumerate_keys,
    gnmi_get,
    gnmi_get_many,
    gnmi_set,
)
from qactl.dnctl.gnmi.tools.subscribe import gnmi_subscribe

app = typer.Typer(no_args_is_help=True, help="gNMI get / set / enumerate against DNOS devices.")

TlsMode = Annotated[str, typer.Option("--tls-mode", help="insecure | skip_verify | verify_ca | mtls.")]
Encoding = Annotated[str, typer.Option("--encoding", help="json | proto.")]
Datatype = Annotated[str, typer.Option("--datatype", help="all (the only datatype DNOS honours).")]


def _parse_assign(spec: str) -> Tuple[str, Any]:
    """Split an ``xpath=value`` op spec into ``(path, parsed_val)``.

    The split happens at the first ``=`` that is **not** inside a list-key
    predicate, so ``ncps/ncp[ncp-id=0]/admin-state=up`` yields path
    ``ncps/ncp[ncp-id=0]/admin-state`` and value ``up``. The value is parsed
    as JSON when possible, else kept as a string.
    """
    depth = 0
    cut = -1
    for i, ch in enumerate(spec):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0:
            cut = i
            break
    if cut < 0:
        raise ValueError(f"expected 'xpath=value', got {spec!r}")
    path = spec[:cut].strip()
    if not path:
        raise ValueError(f"empty xpath in {spec!r}")
    raw = spec[cut + 1:]
    try:
        return path, _json.loads(raw)
    except ValueError:
        return path, raw


@app.command()
def get(
    path: Annotated[str, typer.Argument(help="gNMI xpath; keyed lists need [k=v].")],
    tls_mode: TlsMode = "insecure",
    encoding: Encoding = "json",
    datatype: Datatype = "all",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Single-path gNMI Get."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(gnmi_get, c, path=path, tls_mode=tls_mode, encoding=encoding, datatype=datatype), c)


@app.command("get-many")
def get_many(
    paths: Annotated[List[str], typer.Argument(help="One or more gNMI xpaths.")],
    one_call: Annotated[bool, typer.Option("--one-call", help="Send all paths in a single Get RPC.")] = False,
    tls_mode: TlsMode = "insecure",
    encoding: Encoding = "json",
    datatype: Datatype = "all",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Multiple gNMI Gets against one device (paced, or one_call)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(gnmi_get_many, c, paths=paths, one_call=one_call, tls_mode=tls_mode, encoding=encoding, datatype=datatype), c)


@app.command()
def set(
    path: Annotated[Optional[str], typer.Argument(help="Shorthand single-update xpath (pair with VAL).")] = None,
    val: Annotated[Optional[str], typer.Argument(help="Value for the shorthand PATH (JSON if parseable, else string).")] = None,
    update: Annotated[Optional[List[str]], typer.Option("--update", help="Merge op 'xpath=value' (repeatable).")] = None,
    replace: Annotated[Optional[List[str]], typer.Option("--replace", help="Replace op 'xpath=value' (repeatable).")] = None,
    delete: Annotated[Optional[List[str]], typer.Option("--delete", help="Delete xpath (repeatable).")] = None,
    tls_mode: TlsMode = "insecure",
    encoding: Encoding = "json",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Atomic gNMI Set — update / replace / delete in one RPC (DESTRUCTIVE — needs --yes).

    The three op kinds are repeatable and freely mixed; the server applies
    them in a single atomic Set. The positional PATH VAL is shorthand for a
    single update. List paths need their key predicate
    (``ncps/ncp[ncp-id=0]/...``). Examples::

        gnmi set system/.../leaf true -y
        gnmi set --update a/b=1 --replace c/d='{"x":1}' --delete e/f -y
    """
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    deletes = list(delete or [])
    try:
        upd = [{"path": p, "val": v} for p, v in (_parse_assign(s) for s in (update or []))]
        rep = [{"path": p, "val": v} for p, v in (_parse_assign(s) for s in (replace or []))]
    except ValueError as exc:
        O.finish({"status": "error", "errors": [str(exc)]}, c)
        return

    if path is not None:
        if val is None:
            O.finish({"status": "error", "errors": ["positional PATH needs a VAL; or use --update/--replace/--delete"]}, c)
            return
        try:
            parsed = _json.loads(val)
        except ValueError:
            parsed = val
        upd.append({"path": path, "val": parsed})

    if not (upd or rep or deletes):
        O.finish({"status": "error", "errors": ["nothing to set; provide PATH VAL or --update/--replace/--delete"]}, c)
        return

    op_count = len(upd) + len(rep) + len(deletes)
    label = f"gnmi set ({op_count} op{'' if op_count == 1 else 's'}) on {c.device or c.host}"
    if not confirm.ensure(label, yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(
            gnmi_set, c, confirm=True, tls_mode=tls_mode, encoding=encoding,
            update=upd or None, replace=rep or None, delete=deletes or None,
        ),
        c,
    )


@app.command("enumerate-keys")
def enumerate_keys(
    list_path: Annotated[str, typer.Argument(help="Parent path of the keyed list.")],
    tls_mode: TlsMode = "insecure",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Discover which list keys exist at a parent path."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(gnmi_enumerate_keys, c, list_path=list_path, tls_mode=tls_mode), c)


@app.command()
def subscribe(
    paths: Annotated[List[str], typer.Argument(help="One or more gNMI xpaths to subscribe (keyed lists need [k=v]).")],
    mode: Annotated[str, typer.Option("--mode", help="on_change | sample | target_defined.")] = "on_change",
    on_change: Annotated[bool, typer.Option("--on-change", help="Shorthand for --mode on_change.")] = False,
    sample_interval: Annotated[float, typer.Option("--sample-interval", help="Seconds between samples (sample mode).")] = 10.0,
    duration: Annotated[float, typer.Option("--duration", help="Capture window in seconds (call returns after this).")] = 30.0,
    max_updates: Annotated[int, typer.Option("--max-updates", help="Stop after N events (0 = no cap, time-bounded only).")] = 0,
    heartbeat: Annotated[float, typer.Option("--heartbeat", help="ON_CHANGE heartbeat seconds (0 = off).")] = 0.0,
    tls_mode: TlsMode = "insecure",
    encoding: Encoding = "json",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Bounded gNMI Subscribe (STREAM) — push-native event capture.

    Opens an on-change subscription and collects telemetry until
    --duration elapses or --max-updates is hit, then emits one envelope.
    This is the device primitive behind "tell me when BGP goes down"; a
    workspace-level collector loops it to feed an event spool / Slack.

    Examples::

        gnmi subscribe bgp/.../neighbor[neighbor-address=10.0.0.1]/state -d cl
        gnmi subscribe interfaces/interface/state/oper-status --duration 60 --json
        gnmi subscribe components/.../temperature --mode sample --sample-interval 5
    """
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    eff_mode = "on_change" if on_change else mode
    O.finish(
        O.call(
            gnmi_subscribe, c, paths=paths, mode=eff_mode,
            sample_interval_s=sample_interval, duration_s=duration,
            max_updates=max_updates, heartbeat_s=heartbeat,
            tls_mode=tls_mode, encoding=encoding,
        ),
        c,
    )


@app.command()
def capabilities(
    name_contains: Annotated[Optional[str], typer.Option("--name-contains", help="Filter advertised modules.")] = None,
    tls_mode: TlsMode = "insecure",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """gNMI Capabilities (advertised models / encodings)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(gnmi_capabilities, c, name_contains=name_contains, tls_mode=tls_mode), c)


@app.command()
def ping(
    tls_mode: TlsMode = "insecure",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Liveness check (open a gNMI channel)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(gnmi_ping, c, tls_mode=tls_mode), c)


@app.command()
def devices(as_json: O.Json = False):
    """List devices known to the registry."""
    c = O.build_ctx(as_json=as_json)
    O.finish(gnmi_list_devices(), c)
