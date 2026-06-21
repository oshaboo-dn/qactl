"""`dnctl cli ...` — SSH→DNOS CLI subcommand group (from user-cli-mcp).

Thin Typer front-end over the lifted CLI tools in
:mod:`dnctl.cli.tools`. Reads/discovery are safe; config / recovery /
deploy / restore / tar-load are destructive and gated by ``--yes``.
"""

from __future__ import annotations

from typing import Annotated, List, Optional

import typer

from dnctl.core import confirm, options as O
from dnctl.cli.tools.backup import backup_device, list_backups, read_backup, restore_device
from dnctl.cli.tools.clear import clear as clear_tool
from dnctl.cli.tools.devices import list_devices, manage_device
from dnctl.cli.tools.discovery import (
    cli_config_crawler,
    cli_crawler,
    cmd_help,
    cmd_search,
    show_config,
    show_system,
)
from dnctl.cli.tools.edit import (
    edit_config,
    edit_config_check,
    load_override_factory_default,
    rollback_config,
)
from dnctl.cli.tools.gitcommit import get_gitcommit
from dnctl.cli.tools.log_read import get_accounting, get_netconf_accounting, get_system_events
from dnctl.cli.tools.ping import run_ping_ipv4
from dnctl.cli.tools.shell import run_ncm_cli, run_shell
from dnctl.cli.tools.restart import (
    kill_9_ncc_process,
    request_system_container_restart,
    request_system_ncc_switchover,
    request_system_process_restart,
    request_system_restart,
    request_system_restart_nce,
)
from dnctl.cli.tools.tarload import (
    get_tar_load_job,
    request_system_pre_check,
    request_system_tar_load,
)
from dnctl.cli.tools.techsupport import create_techsupport, get_techsupport_job
from dnctl.cli.tools.templates import (
    render_config,
    scale_deploy,
    template_get,
    template_list,
)
from dnctl.cli.tools.traces import get_trace, list_traces

app = typer.Typer(no_args_is_help=True, help="SSH→DNOS CLI: show / config / backup / recovery.")
template_app = typer.Typer(no_args_is_help=True, help="Manage jinja templates.")
device_app = typer.Typer(no_args_is_help=True, help="Manage the device registry.")
ts_app = typer.Typer(no_args_is_help=True, help="Tech-support bundles.")
tarload_app = typer.Typer(no_args_is_help=True, help="Stage upgrade image tars (target-stack).")
backup_app = typer.Typer(no_args_is_help=True, help="Config backups (create / list / read / restore).")
restart_app = typer.Typer(no_args_is_help=True, help="Restart system / node / container / process.")
app.add_typer(template_app, name="template")
app.add_typer(device_app, name="device")
app.add_typer(ts_app, name="techsupport")
app.add_typer(tarload_app, name="tar-load")
app.add_typer(backup_app, name="backup")
app.add_typer(restart_app, name="restart")


# --- reads / discovery -----------------------------------------------------


@app.command()
def show(
    cmd: Annotated[List[str], typer.Argument(help="DNOS show command words.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Run an operational `show` command."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    from dnctl.cli.tools.discovery import show as show_tool
    O.finish(O.call(show_tool, c, command=" ".join(cmd)), c)


@app.command("show-config")
def show_config_cmd(
    cmd: Annotated[List[str], typer.Argument(help="DNOS `show config` command words.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Run a configuration `show config` command."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(show_config, c, command=" ".join(cmd)), c)


@app.command()
def system(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Topology + version snapshot (call first for system/restart tasks)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(show_system, c), c)


@app.command()
def search(
    scope: Annotated[str, typer.Argument(help="show | show_config | configure | clear | request | run | set | unset | all-commands.")],
    words: Annotated[List[str], typer.Argument(help="Keywords to match.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Search a DNOS command tree for commands matching keywords."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(cmd_search, c, scope=scope, words=words), c)


@app.command("help")
def help_cmd(
    command: Annotated[List[str], typer.Argument(help="DNOS command line to get help for.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Full help for a specific DNOS command line."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(cmd_help, c, command=" ".join(command)), c)


@app.command()
def crawl(
    path: Annotated[str, typer.Argument(help="CLI tree path prefix (empty = root).")] = "",
    config: Annotated[bool, typer.Option("--config", help="Crawl the configure-mode tree.")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Walk the operational (or configure-mode) CLI tree one level."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    fn = cli_config_crawler if config else cli_crawler
    O.finish(O.call(fn, c, path=path), c)


@app.command()
def gitcommit(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Report the device's running git commit / build."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(get_gitcommit, c), c)


def _log_filters(tail, since, until, grep, grep_exclude, ignore_case):
    return {
        "tail_lines": tail, "since": since, "until": until,
        "grep": grep, "grep_exclude": grep_exclude, "grep_ignore_case": ignore_case,
    }


@app.command()
def accounting(
    tail: Annotated[Optional[int], typer.Option("--tail", help="Last N lines.")] = 500,
    since: Annotated[Optional[str], typer.Option("--since")] = None,
    until: Annotated[Optional[str], typer.Option("--until")] = None,
    grep: Annotated[Optional[str], typer.Option("--grep")] = None,
    grep_exclude: Annotated[Optional[str], typer.Option("--grep-exclude")] = None,
    ignore_case: Annotated[bool, typer.Option("--ignore-case")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Read the CLI accounting log."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(get_accounting, c, **_log_filters(tail, since, until, grep, grep_exclude, ignore_case)), c)


@app.command("netconf-accounting")
def netconf_accounting(
    tail: Annotated[Optional[int], typer.Option("--tail", help="Last N lines.")] = 500,
    since: Annotated[Optional[str], typer.Option("--since")] = None,
    until: Annotated[Optional[str], typer.Option("--until")] = None,
    grep: Annotated[Optional[str], typer.Option("--grep")] = None,
    grep_exclude: Annotated[Optional[str], typer.Option("--grep-exclude")] = None,
    ignore_case: Annotated[bool, typer.Option("--ignore-case")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Read the NETCONF accounting log."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(get_netconf_accounting, c, **_log_filters(tail, since, until, grep, grep_exclude, ignore_case)), c)


@app.command()
def events(
    tail: Annotated[Optional[int], typer.Option("--tail", help="Last N lines.")] = 500,
    since: Annotated[Optional[str], typer.Option("--since")] = None,
    until: Annotated[Optional[str], typer.Option("--until")] = None,
    grep: Annotated[Optional[str], typer.Option("--grep")] = None,
    grep_exclude: Annotated[Optional[str], typer.Option("--grep-exclude")] = None,
    ignore_case: Annotated[bool, typer.Option("--ignore-case")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Read the system events log."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(get_system_events, c, **_log_filters(tail, since, until, grep, grep_exclude, ignore_case)), c)


@app.command()
def traces(
    target: Annotated[Optional[str], typer.Option("--target", help="bgp | isis | zebra | fibmgr | wb_agent.")] = None,
    component: Annotated[Optional[str], typer.Option("--component")] = None,
    max_entries: Annotated[int, typer.Option("--max-entries")] = 200,
    no_rotated: Annotated[bool, typer.Option("--no-rotated", help="Exclude rotated .gz files.")] = False,
    ncc: Annotated[Optional[str], typer.Option("--ncc")] = None,
    ncp: Annotated[Optional[str], typer.Option("--ncp")] = None,
    container: Annotated[Optional[str], typer.Option("--container")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """List trace files."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(list_traces, c, target=target, component=component, max_entries=max_entries,
                    include_rotated=not no_rotated, ncc=ncc, ncp=ncp, container=container), c)


@app.command()
def trace(
    name: Annotated[Optional[str], typer.Argument(help="Trace filename (from `traces`).")] = None,
    target: Annotated[Optional[str], typer.Option("--target")] = None,
    tail: Annotated[Optional[int], typer.Option("--tail")] = 500,
    since: Annotated[Optional[str], typer.Option("--since")] = None,
    until: Annotated[Optional[str], typer.Option("--until")] = None,
    grep: Annotated[Optional[str], typer.Option("--grep")] = None,
    grep_exclude: Annotated[Optional[str], typer.Option("--grep-exclude")] = None,
    ignore_case: Annotated[bool, typer.Option("--ignore-case")] = False,
    level: Annotated[Optional[str], typer.Option("--level", help="ERROR | WARNING | INFO | DEBUG.")] = None,
    live_only: Annotated[bool, typer.Option("--live-only")] = False,
    count_only: Annotated[bool, typer.Option("--count-only")] = False,
    ncc: Annotated[Optional[str], typer.Option("--ncc")] = None,
    ncp: Annotated[Optional[str], typer.Option("--ncp")] = None,
    container: Annotated[Optional[str], typer.Option("--container")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Read a trace file with filters."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(get_trace, c, name=name, target=target, tail_lines=tail, since=since, until=until,
                    grep=grep, grep_exclude=grep_exclude, grep_ignore_case=ignore_case, level=level,
                    live_only=live_only, count_only=count_only, ncc=ncc, ncp=ncp, container=container), c)


@app.command()
def ping(
    dest: Annotated[str, typer.Argument(help="Destination IPv4 address.")],
    count: Annotated[Optional[int], typer.Option("--count")] = None,
    size: Annotated[Optional[int], typer.Option("--size")] = None,
    vrf: Annotated[Optional[str], typer.Option("--vrf")] = None,
    source_interface: Annotated[Optional[str], typer.Option("--source-interface")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Run `ping` from the device."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(run_ping_ipv4, c, dest=dest, count=count, size=size, vrf=vrf, source_interface=source_interface), c)


@app.command()
def shell(
    commands: Annotated[List[str], typer.Argument(help="Linux command(s) to run in `run start shell`. Each argument is one command; chained with && (or ; with --continue-on-error).")],
    ncc: Annotated[Optional[str], typer.Option("--ncc", help="Target NCC: 0 | 1 | active.")] = None,
    ncp: Annotated[Optional[str], typer.Option("--ncp", help="Target NCP: 0..191 | bfd-master.")] = None,
    ncm: Annotated[Optional[str], typer.Option("--ncm", help="Target NCM: A0 | B0 | ...")] = None,
    container: Annotated[Optional[str], typer.Option("--container", help="Target container on the selected NCC.")] = None,
    continue_on_error: Annotated[bool, typer.Option("--continue-on-error", help="Chain commands with ';' instead of '&&'.")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Run one or a sequence of Linux commands via `run start shell`, then exit (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"run start shell on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(run_shell, c, commands=commands, ncc=ncc, ncp=ncp, ncm=ncm,
               container=container, continue_on_error=continue_on_error),
        c,
    )


@app.command("ncm-cli")
def ncm_cli(
    commands: Annotated[List[str], typer.Argument(help="NCM CLI command(s) to run in order, e.g. 'show lldp neighbors' or 'configure' 'interface eth 0/5' 'shutdown'.")],
    ncm: Annotated[str, typer.Option("--ncm", help="Target NCM: A0 | B0 | ...")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Drive the NCM switch's nested (ICOS-style) CLI via `run start shell ncm`, then exit (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"run start shell ncm {ncm} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(run_ncm_cli, c, commands=commands, ncm=ncm), c)


# --- config (destructive) --------------------------------------------------


@app.command()
def config(
    statements: Annotated[List[str], typer.Argument(help="Configure-mode statements.")],
    check: Annotated[bool, typer.Option("--check", help="Dry-run via 'commit check' — no commit, not destructive (no --yes needed).")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Apply configure-mode statements + commit; --check dry-runs instead (DESTRUCTIVE without --check — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if check:
        O.finish(O.call(edit_config_check, c, statements=statements), c)
        return
    if not confirm.ensure(f"edit-config on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(edit_config, c, statements=statements), c)


@app.command()
def rollback(
    rollback_id: Annotated[int, typer.Argument(help="Rollback checkpoint id.")] = 1,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Roll the running config back to a checkpoint (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"rollback {rollback_id} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(rollback_config, c, rollback_id=rollback_id), c)


# --- templates / scale -----------------------------------------------------


@template_app.command("list")
def template_list_cmd(as_json: O.Json = False):
    """List jinja templates."""
    O.finish(template_list(), O.build_ctx(as_json=as_json))


@template_app.command("get")
def template_get_cmd(
    name: Annotated[str, typer.Argument(help="Template name.")],
    as_json: O.Json = False,
):
    """Show one jinja template."""
    O.finish(template_get(name=name), O.build_ctx(as_json=as_json))


@app.command("render")
def render_cmd(
    name: Annotated[Optional[str], typer.Option("--name", help="Saved template name.")] = None,
    content_inline: Annotated[Optional[str], typer.Option("--content", help="Inline Jinja2 template body.")] = None,
    template_file: Annotated[Optional[str], typer.Option("--template-file", help="Jinja2 template file ('-' = stdin).")] = None,
    vars_inline: Annotated[Optional[str], typer.Option("--vars", help="Inline YAML vars.")] = None,
    vars_file: Annotated[Optional[str], typer.Option("--vars-file", help="YAML vars file.")] = None,
    script_inline: Annotated[Optional[str], typer.Option("--script", help="Inline python generator (prints YAML).")] = None,
    script_file: Annotated[Optional[str], typer.Option("--script-file", help="Python generator file.")] = None,
    out_file: Annotated[Optional[str], typer.Option("--out", help="Write the rendered config to this path.")] = None,
    exec_timeout: Annotated[float, typer.Option("--exec-timeout", help="Generator wall-clock budget (s).")] = 30.0,
    as_json: O.Json = False,
):
    """Render a template into a local config (file / stdout); no device traffic.

    Template: --name (saved) or --content/--template-file (inline). Vars:
    --vars/--vars-file (YAML) or --script/--script-file (python generator).
    With no vars it's a preflight that reports declared variables.
    Pipe stdout into 'scale-deploy -', or save with --out.
    """
    c = O.build_ctx(as_json=as_json)
    content = O.read_body(content_inline, template_file, c, required=False)
    vars_yaml = O.read_body(vars_inline, vars_file, c, required=False)
    python_script = O.read_body(script_inline, script_file, c, required=False)
    O.finish(
        render_config(
            name=name, content=content, vars_yaml=vars_yaml,
            python_script=python_script, out_file=out_file,
            exec_timeout=exec_timeout,
        ),
        c,
    )


@app.command("scale-deploy")
def scale_deploy_cmd(
    rendered_file: Annotated[str, typer.Argument(help="Rendered .cli file to push ('-' = stdin).")],
    log: Annotated[Optional[str], typer.Option("--log", help="Commit annotation.")] = None,
    check: Annotated[bool, typer.Option("--check", help="Commit-check only, don't commit (not destructive).")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Deploy a rendered config file (or '-' for stdin); --check dry-runs instead (DESTRUCTIVE without --check — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    deploy = not check
    label = "stdin" if rendered_file == "-" else rendered_file
    if deploy and not confirm.ensure(f"scale-deploy {label} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    if rendered_file == "-":
        text = O.read_body("-", None, c, required=True)
        O.finish(O.call(scale_deploy, c, rendered_text=text, log=log, deploy=deploy), c)
    else:
        O.finish(O.call(scale_deploy, c, rendered_file=rendered_file, log=log, deploy=deploy), c)


# --- backup / restore ------------------------------------------------------


@backup_app.command("create")
def backup_create(
    description: Annotated[Optional[str], typer.Option("--description")] = None,
    bucket: Annotated[Optional[str], typer.Option("--bucket")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Back up a device's config to this host."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(backup_device, c, description=description, bucket=bucket), c)


@backup_app.command("list")
def backup_list(
    bucket: Annotated[Optional[str], typer.Option("--bucket")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 100,
    device: O.Device = None, as_json: O.Json = False,
):
    """List stored CLI backups."""
    c = O.build_ctx(device=device, as_json=as_json)
    O.finish(O.call(list_backups, c, bucket=bucket, limit=limit), c)


@backup_app.command("read")
def backup_read(
    filename: Annotated[str, typer.Argument(help="Backup filename.")],
    device: Annotated[str, typer.Option("--device", "-d", help="Device alias the backup belongs to.")] = ...,
    bucket: Annotated[Optional[str], typer.Option("--bucket")] = None,
    as_json: O.Json = False,
):
    """Read a stored CLI backup."""
    c = O.build_ctx(device=device, as_json=as_json)
    kw = {"filename": filename, "device": device}
    if bucket:
        kw["bucket"] = bucket
    O.finish(read_backup(**kw), c)


@backup_app.command("restore")
def backup_restore(
    filename: Annotated[str, typer.Argument(help="Backup filename to restore.")],
    bucket: Annotated[Optional[str], typer.Option("--bucket")] = None,
    mode: Annotated[str, typer.Option("--mode", help="override | merge (default: override).")] = "override",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Restore a backup onto a device (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not c.device:
        O.finish({"status": "error", "errors": ["--device is required for cli restore"]}, c)
    if not confirm.ensure(f"restore {filename} ({mode}) onto {c.device}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(restore_device, c, filename=filename, bucket=bucket, mode=mode, confirm=True), c)


# --- techsupport -----------------------------------------------------------


@ts_app.command("create")
def techsupport_create(
    name: Annotated[str, typer.Argument(help="Tech-support bundle name.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Create a tech-support bundle (long-running job)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(create_techsupport, c, name=name), c)


@ts_app.command("show")
def techsupport_show(
    job_id: Annotated[Optional[str], typer.Argument(help="Job id (or use --device).")] = None,
    device: O.Device = None, as_json: O.Json = False,
):
    """Poll a tech-support job."""
    c = O.build_ctx(device=device, as_json=as_json)
    O.finish(O.call(get_techsupport_job, c, job_id=job_id), c)


# --- tar-load --------------------------------------------------------------


@tarload_app.command("start")
def tar_load_start(
    jenkins_url: Annotated[str, typer.Argument(help="Jenkins artifact URL of the tar image.")],
    no_pre_check: Annotated[bool, typer.Option("--no-pre-check")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Load a system image tar (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"tar-load on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(request_system_tar_load, c, jenkins_url=jenkins_url, pre_check=not no_pre_check), c)


@tarload_app.command("show")
def tar_load_show(
    job_id: Annotated[Optional[str], typer.Argument(help="Job id (or use --device).")] = None,
    device: O.Device = None, as_json: O.Json = False,
):
    """Poll a tar-load job."""
    c = O.build_ctx(device=device, as_json=as_json)
    O.finish(O.call(get_tar_load_job, c, job_id=job_id), c)


@tarload_app.command("pre-check")
def tar_load_pre_check(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Run the pre-upgrade system pre-check (read-only)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(request_system_pre_check, c), c)


# --- device registry -------------------------------------------------------


@device_app.command("list")
def device_list(as_json: O.Json = False):
    """List devices known to the registry."""
    O.finish(list_devices(), O.build_ctx(as_json=as_json))


@device_app.command("add")
def device_add(
    name: Annotated[str, typer.Argument(help="New device alias.")],
    sn: Annotated[Optional[str], typer.Option("--sn", help="Serial / SSH host to probe.")] = None,
    user: O.User = None, password: O.Password = None,
    timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """Probe a chassis and add it to the registry (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(user=user, password=password, timeout=timeout, as_json=as_json, yes=yes)
    if not confirm.ensure(f"device add {name}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(manage_device, c, operation="add", name=name, sn=sn), c)


@device_app.command("remove")
def device_remove(
    name: Annotated[str, typer.Argument(help="Device alias to remove.")],
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Remove a device from the registry (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(as_json=as_json, yes=yes)
    if not confirm.ensure(f"device remove {name}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(manage_device(operation="remove", name=name), c)


@device_app.command("refresh")
def device_refresh(
    name: Annotated[str, typer.Argument(help="Device alias to re-probe.")],
    user: O.User = None, password: O.Password = None,
    timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """Re-probe a device and refresh its registry entry (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(user=user, password=password, timeout=timeout, as_json=as_json, yes=yes)
    if not confirm.ensure(f"device refresh {name}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(manage_device, c, operation="refresh", name=name), c)


@device_app.command("alias")
def device_alias(
    name: Annotated[str, typer.Argument(help="Canonical device (chassis System Name).")],
    alias: Annotated[str, typer.Argument(help="Secondary nickname to attach.")],
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Attach a secondary nickname to a device (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(as_json=as_json, yes=yes)
    if not confirm.ensure(f"device alias {alias} -> {name}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(manage_device(operation="alias", name=name, alias=alias), c)


@device_app.command("unalias")
def device_unalias(
    alias: Annotated[str, typer.Argument(help="Secondary nickname to detach.")],
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Detach a secondary nickname from its device (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(as_json=as_json, yes=yes)
    if not confirm.ensure(f"device unalias {alias}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(manage_device(operation="unalias", alias=alias), c)


# --- recovery (all destructive) --------------------------------------------


@app.command("factory-default")
def factory_default(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Load override factory-default (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"factory-default on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(load_override_factory_default, c), c)


@restart_app.command("system")
def restart_system(
    mode: Annotated[str, typer.Option("--mode", help="cold | warm | recovery (default: cold).")] = "cold",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Restart the whole system (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"system restart ({mode}) on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(request_system_restart, c, mode=mode, confirm=True), c)


@restart_app.command("nce")
def restart_nce(
    node_role: Annotated[str, typer.Argument(help="ncc | ncp | ncm | ncf.")],
    node_id: Annotated[str, typer.Argument(help="Node id.")],
    mode: Annotated[str, typer.Option("--mode", help="cold | warm | force.")] = "cold",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Restart a single node element (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"restart-nce {node_role} {node_id} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(request_system_restart_nce, c, node_role=node_role, node_id=node_id, mode=mode, confirm=True), c)


@restart_app.command("container")
def restart_container(
    node_role: Annotated[str, typer.Argument(help="ncc | ncp | ncm | ncf.")],
    node_id: Annotated[str, typer.Argument(help="Node id.")],
    container_name: Annotated[str, typer.Argument(help="Container name.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Restart a container on a node (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"restart-container {node_role} {node_id} {container_name}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(request_system_container_restart, c, node_role=node_role, node_id=node_id, container_name=container_name, confirm=True), c)


@restart_app.command("process")
def restart_process(
    node_role: Annotated[str, typer.Argument(help="ncc | ncp | ncf.")],
    node_id: Annotated[str, typer.Argument(help="Node id.")],
    container_name: Annotated[str, typer.Argument(help="Container name.")],
    process_name: Annotated[str, typer.Argument(help="Process name.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Restart a process in a container (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"restart-process {process_name} in {container_name}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(request_system_process_restart, c, node_role=node_role, node_id=node_id,
                    container_name=container_name, process_name=process_name, confirm=True), c)


@app.command()
def switchover(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Trigger an NCC switchover (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"ncc switchover on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(request_system_ncc_switchover, c, confirm=True), c)


@app.command()
def kill9(
    process: Annotated[str, typer.Argument(help="bgpd | zebra | fibmgrd.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """kill -9 a routing-engine process (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"kill -9 {process} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(kill_9_ncc_process, c, process=process), c)
