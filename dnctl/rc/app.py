"""`dnctl rc ...` — RESTCONF subcommand group (from user-restconf-mcp).

Thin Typer front-end over the lifted RESTCONF tools in
:mod:`dnctl.rc.tools`. Paths are slash-separated YANG segments with the
list-key shorthand (``a/b/list=key/c``); the lifted core applies the
per-endpoint module-name and URI-style quirks before the HTTP call.
Auth/verify are per-endpoint (from the endpoints registry), so the
SSH-style ``--user``/``--password`` flags don't apply here.
"""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from dnctl.core import confirm, options as O
from dnctl.rc.tools.devices import (
    restconf_list_devices,
    restconf_list_endpoints,
    restconf_resolve,
)
from dnctl.rc.tools.diag import restconf_ping, restconf_yang_library
from dnctl.rc.tools.mount import (
    restconf_mount_add,
    restconf_mount_remove,
    restconf_mount_status,
)
from dnctl.rc.tools.rw import (
    restconf_delete,
    restconf_enumerate_keys,
    restconf_get,
    restconf_get_url,
    restconf_patch,
    restconf_post,
    restconf_put,
)

app = typer.Typer(no_args_is_help=True, help="RESTCONF get / write / mount against DNOS devices via ODL.")
mount_app = typer.Typer(no_args_is_help=True, help="Manage ODL device mounts.")
app.add_typer(mount_app, name="mount")

Endpoint = Annotated[Optional[str], typer.Option("--endpoint", help="RESTCONF endpoint alias (default: device's mount).")]
Mount = Annotated[Optional[str], typer.Option("--mount-name", help="Mount name on the endpoint.")]
Style = Annotated[Optional[str], typer.Option("--style", help="URI style: rfc8040 | legacy.")]


def _writer(fn, kind: str, path: str, body: Optional[str], file: Optional[str], c, endpoint, mount_name, style):
    if not confirm.ensure(f"restconf {kind} {path}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    payload = O.rc_payload(O.read_body(body, file, c))
    O.finish(
        O.call(fn, c, segments=path, payload=payload, endpoint=endpoint, mount_name=mount_name, style=style, confirm=True),
        c,
    )


@app.command()
def get(
    path: Annotated[str, typer.Argument(help="Slash-separated YANG path (a/b/list=key/c).")],
    datastore: Annotated[str, typer.Option("--datastore", help="operational | config.")] = "operational",
    endpoint: Endpoint = None, mount_name: Mount = None, style: Style = None,
    device: O.Device = None, timeout: O.Timeout = None, as_json: O.Json = False,
):
    """RESTCONF GET a resource."""
    c = O.build_ctx(device=device, timeout=timeout, as_json=as_json)
    O.finish(O.call(restconf_get, c, segments=path, datastore=datastore, endpoint=endpoint, mount_name=mount_name, style=style), c)


@app.command("get-url")
def get_url(
    url: Annotated[str, typer.Argument(help="Absolute RESTCONF URL.")],
    endpoint: Annotated[str, typer.Option("--endpoint", help="Endpoint alias to borrow auth/base from.")] = "odl-lab1",
    accept: Annotated[str, typer.Option("--accept", help="Accept header.")] = "application/json",
    timeout: O.Timeout = None, as_json: O.Json = False,
):
    """RESTCONF GET an arbitrary URL (escape hatch)."""
    c = O.build_ctx(timeout=timeout, as_json=as_json)
    O.finish(O.call(restconf_get_url, c, endpoint=endpoint, url=url, accept=accept), c)


@app.command()
def post(
    path: Annotated[str, typer.Argument(help="Slash-separated YANG path.")],
    body: Annotated[Optional[str], typer.Argument(help="JSON/XML body, or '-' for stdin.")] = None,
    file: Annotated[Optional[str], typer.Option("--file", "-f", help="Read body from a file.")] = None,
    endpoint: Endpoint = None, mount_name: Mount = None, style: Style = None,
    device: O.Device = None, timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """RESTCONF POST (create child) (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device=device, timeout=timeout, as_json=as_json, yes=yes)
    _writer(restconf_post, "post", path, body, file, c, endpoint, mount_name, style)


@app.command()
def put(
    path: Annotated[str, typer.Argument(help="Slash-separated YANG path.")],
    body: Annotated[Optional[str], typer.Argument(help="JSON/XML body, or '-' for stdin.")] = None,
    file: Annotated[Optional[str], typer.Option("--file", "-f", help="Read body from a file.")] = None,
    endpoint: Endpoint = None, mount_name: Mount = None, style: Style = None,
    device: O.Device = None, timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """RESTCONF PUT (replace resource) (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device=device, timeout=timeout, as_json=as_json, yes=yes)
    _writer(restconf_put, "put", path, body, file, c, endpoint, mount_name, style)


@app.command()
def patch(
    path: Annotated[str, typer.Argument(help="Slash-separated YANG path.")],
    body: Annotated[Optional[str], typer.Argument(help="JSON/XML body, or '-' for stdin.")] = None,
    file: Annotated[Optional[str], typer.Option("--file", "-f", help="Read body from a file.")] = None,
    endpoint: Endpoint = None, mount_name: Mount = None, style: Style = None,
    device: O.Device = None, timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """RESTCONF PATCH (merge resource) (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device=device, timeout=timeout, as_json=as_json, yes=yes)
    _writer(restconf_patch, "patch", path, body, file, c, endpoint, mount_name, style)


@app.command()
def delete(
    path: Annotated[str, typer.Argument(help="Slash-separated YANG path.")],
    endpoint: Endpoint = None, mount_name: Mount = None, style: Style = None,
    device: O.Device = None, timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """RESTCONF DELETE a resource (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device=device, timeout=timeout, as_json=as_json, yes=yes)
    if not confirm.ensure(f"restconf delete {path}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(restconf_delete, c, segments=path, endpoint=endpoint, mount_name=mount_name, style=style, confirm=True), c)


@app.command("enumerate-keys")
def enumerate_keys(
    list_path: Annotated[str, typer.Argument(help="Parent path of the YANG list.")],
    key_field: Annotated[Optional[str], typer.Option("--key-field", help="Override the key leaf.")] = None,
    endpoint: Endpoint = None, mount_name: Mount = None,
    device: O.Device = None, timeout: O.Timeout = None, as_json: O.Json = False,
):
    """List the keys present under a YANG list."""
    c = O.build_ctx(device=device, timeout=timeout, as_json=as_json)
    O.finish(O.call(restconf_enumerate_keys, c, list_segments=list_path, key_field=key_field, endpoint=endpoint, mount_name=mount_name), c)


@app.command()
def resolve(
    device: Annotated[str, typer.Argument(help="Device alias to resolve to its endpoint/mount.")],
    as_json: O.Json = False,
):
    """Resolve a device alias to its endpoint + mount."""
    c = O.build_ctx(as_json=as_json)
    O.finish(restconf_resolve(device=device), c)


@app.command()
def endpoints(as_json: O.Json = False):
    """List configured RESTCONF endpoints."""
    c = O.build_ctx(as_json=as_json)
    O.finish(restconf_list_endpoints(), c)


@app.command("yang-library")
def yang_library(
    endpoint: Annotated[str, typer.Option("--endpoint", help="Endpoint alias.")] = "odl-lab1",
    mount: Annotated[Optional[str], typer.Option("--mount", help="Mount name.")] = None,
    name_contains: Annotated[Optional[str], typer.Option("--name-contains", help="Filter modules by name.")] = None,
    as_json: O.Json = False,
):
    """List YANG library modules on an endpoint/mount."""
    c = O.build_ctx(as_json=as_json)
    O.finish(restconf_yang_library(endpoint=endpoint, mount=mount, name_contains=name_contains), c)


@app.command()
def ping(
    endpoint: Annotated[str, typer.Option("--endpoint", help="Endpoint alias.")] = "odl-lab1",
    as_json: O.Json = False,
):
    """Liveness check against a RESTCONF endpoint."""
    c = O.build_ctx(as_json=as_json)
    O.finish(restconf_ping(endpoint=endpoint), c)


@app.command()
def devices(as_json: O.Json = False):
    """List devices known to the registry."""
    c = O.build_ctx(as_json=as_json)
    O.finish(restconf_list_devices(), c)


@mount_app.command("add")
def mount_add(
    device: Annotated[str, typer.Argument(help="Device alias to mount.")],
    endpoint: Annotated[str, typer.Option("--endpoint", help="Endpoint alias.")] = "odl-lab1",
    mount_name: Mount = None,
    netconf_port: Annotated[int, typer.Option("--netconf-port", help="Device NETCONF port.")] = 830,
    no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Create an ODL device mount (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(as_json=as_json, yes=yes, no_verify_mgmt0=no_verify_mgmt0)
    if not confirm.ensure(f"restconf mount add {device} on {endpoint}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    kw = {"device": device, "endpoint": endpoint, "netconf_port": netconf_port,
          "verify_mgmt0": not no_verify_mgmt0}
    if mount_name:
        kw["mount_name"] = mount_name
    O.finish(restconf_mount_add(**kw), c)


@mount_app.command("remove")
def mount_remove(
    mount_name: Annotated[str, typer.Argument(help="Mount name to remove.")],
    endpoint: Annotated[str, typer.Option("--endpoint", help="Endpoint alias.")] = "odl-lab1",
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Remove an ODL device mount (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(as_json=as_json, yes=yes)
    if not confirm.ensure(f"restconf mount remove {mount_name} on {endpoint}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(restconf_mount_remove(mount_name=mount_name, endpoint=endpoint), c)


@mount_app.command("status")
def mount_status(
    mount_name: Annotated[Optional[str], typer.Argument(help="Mount name or device alias (or use --device).")] = None,
    endpoint: Annotated[str, typer.Option("--endpoint", help="Endpoint alias.")] = "odl-lab1",
    device: O.Device = None, as_json: O.Json = False,
):
    """Show ODL mount status."""
    c = O.build_ctx(device=device, as_json=as_json)
    kw = {"endpoint": endpoint}
    if mount_name:
        kw["mount_name"] = mount_name
    if device:
        kw["device"] = device
    O.finish(restconf_mount_status(**kw), c)
