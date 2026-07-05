"""`dnctl nc ...` — NETCONF subcommand group.

Thin Typer front-end over the NETCONF tools in :mod:`dnctl.nc.tools`.
The caller supplies the full XML payload (inline, via ``--file``, or as
``-`` for stdin); the core handles the session, candidate commit /
discard-on-failure, and logging. Defaults are ``op=merge``,
``source=running``, ``port=830``, ``no_verify=True``, ``timeout=120``.
"""

from __future__ import annotations

from typing import Annotated, List, Optional

import typer

from dnctl.core import confirm, options as O
from dnctl.nc.tools.backup import (
    netconf_backup,
    netconf_diff,
    netconf_list_backups,
    netconf_read_backup,
    netconf_restore,
)
from dnctl.nc.tools.devices import netconf_list_devices
from dnctl.nc.tools.diag import netconf_capabilities, netconf_ping
from dnctl.nc.tools.lifecycle import (
    netconf_apply,
    netconf_discard_changes,
    netconf_rollback,
)
from dnctl.nc.tools.logs import netconf_extract_logs
from dnctl.nc.tools.rw import netconf_edit, netconf_get
from dnctl.nc.tools.yang import netconf_get_schema, netconf_yang_library

app = typer.Typer(no_args_is_help=True, help="NETCONF get / edit / backup / yang against DNOS devices.")

Body = Annotated[Optional[str], typer.Argument(help="XML payload inline, or '-' for stdin.")]
File = Annotated[Optional[str], typer.Option("--file", "-f", help="Read the XML payload from a file.")]


@app.command()
def get(
    xml: Body = None,
    file: File = None,
    oper: Annotated[bool, typer.Option("--oper", help="Use <get> (operational) instead of <get-config>.")] = False,
    source: Annotated[str, typer.Option("--source", help="Datastore for <get-config> (default: running).")] = "running",
    root: Annotated[str, typer.Option("--root", help="Filter root: auto (wrap dn-* under drivenets-top, send OpenConfig/IETF as-is) | dn-top | none.")] = "auto",
    out_file: Annotated[Optional[str], typer.Option("--out-file", help="Also write the full result XML to this path.")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Read config/operational data with a subtree-filter XML."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    body = O.read_body(xml, file, c)
    O.finish(O.call(netconf_get, c, xml=body, oper=oper, source=source, root=root, out_file=out_file), c)


@app.command()
def edit(
    xml: Body = None,
    file: File = None,
    op: Annotated[str, typer.Option("--op", help="merge | replace | remove | delete | create.")] = "merge",
    comment: Annotated[Optional[str], typer.Option("--comment", help="Commit comment.")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Atomic edit-config + commit (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    if not confirm.ensure(f"netconf edit (op={op}) on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    body = O.read_body(xml, file, c)
    O.finish(O.call(netconf_edit, c, xml=body, op=op, comment=comment), c)


@app.command()
def apply(
    files: Annotated[List[str], typer.Argument(help="Payload file(s) under the nc operations dir.")],
    operation_type: Annotated[str, typer.Option("--operation-type", help="edit | ...")] = "edit",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Apply payload file(s) as one edit-config + commit (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    if not confirm.ensure(f"netconf apply {files} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(netconf_apply, c, payload_files=files, operation_type=operation_type), c)


@app.command()
def diff(
    filename: Annotated[Optional[str], typer.Option("--filename", help="Backup filename to diff against.")] = None,
    bucket: Annotated[Optional[str], typer.Option("--bucket", help="Backup bucket.")] = None,
    subtree: Annotated[Optional[str], typer.Option("--subtree", help="Limit diff to a subtree filter XML.")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Diff running config against a backup (or candidate)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(netconf_diff, c, filename=filename, bucket=bucket, subtree=subtree), c)


@app.command()
def rollback(
    index: Annotated[int, typer.Argument(help="Rollback checkpoint index.")],
    no_commit: Annotated[bool, typer.Option("--no-commit", help="Stage the rollback without committing.")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Roll the candidate back to a checkpoint (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    if not confirm.ensure(f"netconf rollback {index} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(netconf_rollback, c, index=index, commit_after=not no_commit), c)


@app.command()
def discard(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Discard candidate changes (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    if not confirm.ensure(f"netconf discard-changes on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(netconf_discard_changes, c), c)


@app.command()
def backup(
    description: Annotated[Optional[str], typer.Option("--description", help="Backup description.")] = None,
    bucket: Annotated[Optional[str], typer.Option("--bucket", help="Backup bucket.")] = None,
    source: Annotated[str, typer.Option("--source", help="Datastore to snapshot (default: running).")] = "running",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Snapshot a device's config to the backup store."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(netconf_backup, c, description=description, bucket=bucket, source=source), c)


@app.command("list-backups")
def list_backups(
    bucket: Annotated[Optional[str], typer.Option("--bucket", help="Filter by bucket.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max entries.")] = 100,
    device: O.Device = None, as_json: O.Json = False,
):
    """List stored NETCONF backups."""
    c = O.build_ctx(device=device, as_json=as_json)
    O.finish(O.call(netconf_list_backups, c, bucket=bucket, limit=limit), c)


@app.command("read-backup")
def read_backup(
    filename: Annotated[str, typer.Argument(help="Backup filename.")],
    bucket: Annotated[Optional[str], typer.Option("--bucket", help="Backup bucket.")] = None,
    as_json: O.Json = False,
):
    """Read a stored NETCONF backup."""
    c = O.build_ctx(as_json=as_json)
    O.finish(netconf_read_backup(filename=filename, **({"bucket": bucket} if bucket else {})), c)


@app.command()
def restore(
    filename: Annotated[str, typer.Argument(help="Backup filename to restore.")],
    bucket: Annotated[Optional[str], typer.Option("--bucket", help="Backup bucket.")] = None,
    mode: Annotated[str, typer.Option("--mode", help="merge | override | none (default: merge). 'override' maps to NETCONF replace.")] = "merge",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Restore a backup onto a device (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    if not c.device:
        O.finish({"status": "error", "errors": ["--device is required for nc restore"]}, c)
    # Speak the cli group's vocabulary ("override") but hand the NETCONF
    # tool the default-operation term it expects ("replace").
    nc_mode = "replace" if mode == "override" else mode
    if not confirm.ensure(f"netconf restore {filename} ({mode}) onto {c.device}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(netconf_restore, c, filename=filename, bucket=bucket, mode=nc_mode, confirm=True), c)


@app.command()
def capabilities(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """List the device's advertised NETCONF capabilities."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(netconf_capabilities, c), c)


@app.command()
def schema(
    identifier: Annotated[str, typer.Argument(help="YANG module name (schema identifier).")],
    version: Annotated[str, typer.Option("--version", help="Schema version.")] = "",
    out_file: Annotated[Optional[str], typer.Option("--out-file", help="Write the .yang to this path.")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Fetch a YANG schema by name (<get-schema>)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(netconf_get_schema, c, identifier=identifier, version=version, out_file=out_file), c)


@app.command("yang-library")
def yang_library(
    name_contains: Annotated[Optional[str], typer.Option("--name-contains", help="Filter modules by name.")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """List the device's YANG library modules."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(netconf_yang_library, c, name_contains=name_contains), c)


@app.command()
def logs(
    date: Annotated[Optional[str], typer.Option("--date", help="YYYY-MM-DD (default: today).")] = None,
    device: O.Device = None, as_json: O.Json = False,
):
    """Extract the per-device NETCONF action log."""
    c = O.build_ctx(device=device, as_json=as_json)
    if not c.device:
        O.finish({"status": "error", "errors": ["--device is required for nc logs"]}, c)
    O.finish(netconf_extract_logs(device=c.device, **({"date": date} if date else {})), c)


@app.command()
def ping(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, no_verify_mgmt0: O.NoVerifyMgmt0 = False,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Liveness check (open a NETCONF session)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes,
                    no_verify_mgmt0=no_verify_mgmt0)
    O.finish(O.call(netconf_ping, c), c)


@app.command()
def devices(as_json: O.Json = False):
    """List devices known to the registry."""
    c = O.build_ctx(as_json=as_json)
    O.finish(netconf_list_devices(), c)
