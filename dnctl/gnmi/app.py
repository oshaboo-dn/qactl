"""`dnctl gnmi ...` — gNMI subcommand group (from user-gnmi-mcp).

Thin Typer front-end over the lifted gNMI tool functions in
:mod:`dnctl.gnmi.tools`. Defaults match what DNOS actually accepts
(``encoding=json``, ``datatype=all``, ``tls_mode=insecure``) so the
agent's existing mental model carries over unchanged.
"""

from __future__ import annotations

import json as _json
from typing import Annotated, List, Optional

import typer

from dnctl.core import options as O
from dnctl.core import confirm
from dnctl.gnmi.tools.devices import gnmi_list_devices
from dnctl.gnmi.tools.diag import gnmi_capabilities, gnmi_ping
from dnctl.gnmi.tools.rw import (
    gnmi_enumerate_keys,
    gnmi_get,
    gnmi_get_many,
    gnmi_set,
)

app = typer.Typer(no_args_is_help=True, help="gNMI get / set / enumerate against DNOS devices.")

TlsMode = Annotated[str, typer.Option("--tls-mode", help="insecure | skip_verify | verify_ca | mtls.")]
Encoding = Annotated[str, typer.Option("--encoding", help="json | proto.")]
Datatype = Annotated[str, typer.Option("--datatype", help="all (the only datatype DNOS honours).")]


@app.command()
def get(
    path: Annotated[str, typer.Argument(help="gNMI xpath; keyed lists need [k=v].")],
    tls_mode: TlsMode = "insecure",
    encoding: Encoding = "json",
    datatype: Datatype = "all",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Single-path gNMI Get."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
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
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Multiple gNMI Gets against one device (paced, or one_call)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(gnmi_get_many, c, paths=paths, one_call=one_call, tls_mode=tls_mode, encoding=encoding, datatype=datatype), c)


@app.command()
def set(
    path: Annotated[str, typer.Argument(help="gNMI xpath to set.")],
    val: Annotated[str, typer.Argument(help="Value (parsed as JSON if possible, else string).")],
    replace: Annotated[bool, typer.Option("--replace", help="Use replace instead of update.")] = False,
    tls_mode: TlsMode = "insecure",
    encoding: Encoding = "json",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Atomic gNMI Set (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"gnmi set {path}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    try:
        parsed = _json.loads(val)
    except ValueError:
        parsed = val
    entry = [{"path": path, "val": parsed}]
    kw = {"replace": entry} if replace else {"update": entry}
    O.finish(O.call(gnmi_set, c, confirm=True, tls_mode=tls_mode, encoding=encoding, **kw), c)


@app.command("enumerate-keys")
def enumerate_keys(
    list_path: Annotated[str, typer.Argument(help="Parent path of the keyed list.")],
    tls_mode: TlsMode = "insecure",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Discover which list keys exist at a parent path."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(gnmi_enumerate_keys, c, list_path=list_path, tls_mode=tls_mode), c)


@app.command()
def capabilities(
    name_contains: Annotated[Optional[str], typer.Option("--name-contains", help="Filter advertised modules.")] = None,
    tls_mode: TlsMode = "insecure",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """gNMI Capabilities (advertised models / encodings)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(gnmi_capabilities, c, name_contains=name_contains, tls_mode=tls_mode), c)


@app.command()
def ping(
    tls_mode: TlsMode = "insecure",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Liveness check (open a gNMI channel)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(gnmi_ping, c, tls_mode=tls_mode), c)


@app.command()
def devices(as_json: O.Json = False):
    """List devices known to the registry."""
    c = O.build_ctx(as_json=as_json)
    O.finish(gnmi_list_devices(), c)
