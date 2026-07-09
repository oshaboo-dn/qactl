"""`dnctl cli ...` — SSH→DNOS CLI subcommand group (from user-cli-mcp).

Thin Typer front-end over the lifted CLI tools in
:mod:`dnctl.cli.tools`. Reads/discovery are safe; config / recovery /
deploy / restore / tar-load are destructive and gated by ``--yes``.
"""

from __future__ import annotations

import json as _json
import time as _time
from typing import Annotated, List, Optional

import typer

from qactl.dnctl.core import confirm, options as O
from qactl.dnctl.cli.tools.backup import backup_device, list_backups, read_backup, restore_device
from qactl.dnctl.cli.tools.clear import clear as clear_tool
from qactl.dnctl.cli.tools.cores import get_core_backtrace, list_cores
from qactl.dnctl.cli.tools.devices import list_devices, manage_device
from qactl.dnctl.cli.tools.discovery import (
    cli_config_crawler,
    cli_crawler,
    cmd_help,
    cmd_search,
    show_config,
    show_system,
)
from qactl.dnctl.cli.tools.edit import (
    edit_config,
    edit_config_check,
    edit_config_compare,
    load_override_factory_default,
    rollback_config,
)
from qactl.dnctl.cli.tools.capture import capture_devices
from qactl.dnctl.cli.tools.gitcommit import get_gitcommit
from qactl.dnctl.cli.tools.interfaces import interfaces as interfaces_tool
from qactl.dnctl.cli.tools.log_read import get_accounting, get_netconf_accounting, get_system_events
from qactl.dnctl.cli.core.events import DEFAULT_SEVERITY as _DEFAULT_SEVERITY
from qactl.dnctl.cli.tools.monitor import monitor_reset, monitor_tick
from qactl.dnctl.cli.tools.ping import run_ping_ipv4
from qactl.dnctl.cli.tools.probe import run_probe
from qactl.dnctl.cli.tools.raw import run_raw
from qactl.dnctl.cli.core.shell_exec import is_read_only_shell
from qactl.dnctl.cli.tools.shell import run_ncm_cli, run_shell
from qactl.dnctl.cli.tools.restart import (
    kill_9_ncc_process,
    request_system_container_restart,
    request_system_ncc_switchover,
    request_system_process_restart,
    request_system_restart,
    request_system_restart_nce,
)
from qactl.dnctl.cli.tools.tarload import (
    get_tar_load_job,
    request_system_pre_check,
    request_system_tar_load,
)
from qactl.dnctl.cli.tools.techsupport import (
    create_techsupport,
    get_techsupport_job,
    list_techsupports,
    upload_techsupport,
)
from qactl.dnctl.cli.tools.templates import (
    render_config,
    scale_deploy,
    template_get,
    template_list,
)
from qactl.dnctl.cli.tools.traces import get_trace, list_traces

app = typer.Typer(no_args_is_help=True, help="SSH→DNOS CLI: show / config / backup / recovery.")
template_app = typer.Typer(no_args_is_help=True, help="Manage jinja templates.")
device_app = typer.Typer(no_args_is_help=True, help="Manage the device registry.")
ts_app = typer.Typer(no_args_is_help=True, help="Tech-support bundles.")
tarload_app = typer.Typer(no_args_is_help=True, help="Stage upgrade image tars (target-stack).")
backup_app = typer.Typer(no_args_is_help=True, help="Config backups (create / list / read / restore).")
restart_app = typer.Typer(no_args_is_help=True, help="Restart system / node / container / process.")
monitor_app = typer.Typer(no_args_is_help=True, help="Event collector (system-events → dedupe → Slack).")
core_app = typer.Typer(no_args_is_help=True, help="Core dumps (list bundles / extract a backtrace).")
app.add_typer(template_app, name="template")
app.add_typer(device_app, name="device")
app.add_typer(ts_app, name="techsupport")
app.add_typer(tarload_app, name="tar-load")
app.add_typer(backup_app, name="backup")
app.add_typer(restart_app, name="restart")
app.add_typer(monitor_app, name="monitor")
app.add_typer(core_app, name="core")


# --- reads / discovery -----------------------------------------------------


@app.command()
def show(
    cmd: Annotated[List[str], typer.Argument(help="DNOS show command words.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
    log: O.Log = None,
):
    """Run an operational `show` command."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    from qactl.dnctl.cli.tools.discovery import show as show_tool
    O.finish(O.call(show_tool, c, command=" ".join(cmd)), c)


@app.command("show-config")
def show_config_cmd(
    cmd: Annotated[List[str], typer.Argument(help="DNOS `show config` command words.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
    log: O.Log = None,
):
    """Run a configuration `show config` command."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    O.finish(O.call(show_config, c, command=" ".join(cmd)), c)


@app.command()
def system(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
    log: O.Log = None,
):
    """Topology + version snapshot (call first for system/restart tasks)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    O.finish(O.call(show_system, c), c)


@app.command()
def interfaces(
    interface: Annotated[Optional[str], typer.Argument(help="Single interface to filter to (e.g. ge400-7/0/8.6). Omit for all.")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
    log: O.Log = None,
):
    """Aggregated per-interface view: state + description + LLDP + IGP in one call."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    O.finish(O.call(interfaces_tool, c, interface=interface), c)


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
    command: Annotated[List[str], typer.Argument(
        help="Canonical DNOS command line (keep <placeholder> tokens intact, "
             "as 'cli search' emits them).")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Full help for a specific DNOS command line.

    COMMAND must be the canonical command string with <placeholder> tokens
    intact — exactly as 'qactl cli search' emits it (e.g. 'configure protocols
    bgp <bgp> neighbor <neighbor> bfd strict-mode hold-time <hold_time>'). A
    concrete/instantiated path (real AS/IP) does NOT error: DNOS silently falls
    back to the nearest ancestor doc (status ok, exit 0) — the envelope then
    sets partial_match=true and warns. Re-run with the canonical form.
    """
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
def probe(
    prefixes: Annotated[List[str], typer.Argument(help="Command-line prefix(es), each typed WITHOUT Enter (one probe per argument, all on ONE channel; the line is wiped with Ctrl-U between probes). Keep a trailing space to enumerate children ('... bfd '); omit it to act on the partial last token ('... bfd str'). Quote to preserve spacing.")],
    key: Annotated[str, typer.Option("--key", help="Keystroke injected after the prefix: '?' (context help) or 'tab' (completion; per-step line_buffer carries the completed line).")] = "?",
    config: Annotated[bool, typer.Option("--config", help="Probe the configure-mode grammar (channel enters 'configure' first, leaves via 'end'; the candidate is never touched).")] = False,
    prompt_timeout: Annotated[Optional[float], typer.Option("--prompt-timeout", help="Seconds to coax a CLI prompt out of a fresh channel (slow/odd boxes). Overrides DNCTL_CLI_PROMPT_TIMEOUT.")] = None,
    banner_wait: Annotated[Optional[float], typer.Option("--banner-wait", help="Per-drain settle window while detecting the prompt. Overrides DNCTL_CLI_BANNER_WAIT.")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Keystroke probe (TAB / ?) on a command prefix WITHOUT submitting it.

    For interactive CLI-discoverability tests: types the prefix, injects a
    single keystroke — never Enter — and returns what the CLI painted (the
    context-help block for '?', the completed line buffer for tab). The
    line is cleared with Ctrl-U after every probe, so nothing is ever
    executed: read-only by construction, no --yes needed.
    """
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(
        O.call(run_probe, c, prefixes=prefixes, key=key, config_mode=config,
               prompt_timeout=prompt_timeout, banner_wait=banner_wait),
        c,
    )


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
    log: O.Log = None,
):
    """Read the CLI accounting log."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
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
    log: O.Log = None,
):
    """Read the NETCONF accounting log."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
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
    log: O.Log = None,
):
    """Read the system events log."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    O.finish(O.call(get_system_events, c, **_log_filters(tail, since, until, grep, grep_exclude, ignore_case)), c)


@monitor_app.command("tick")
def monitor_tick_cmd(
    device: Annotated[Optional[List[str]], typer.Option("--device", "-d", help="Device(s) to poll (repeatable). Default: every registered device.")] = None,
    severity: Annotated[str, typer.Option("--severity", help="Min syslog severity that alerts on its own (emerg|alert|crit|err|warning|notice|info|debug).")] = _DEFAULT_SEVERITY,
    match: Annotated[Optional[List[str]], typer.Option("--match", help="Extra substring that marks an event alert-worthy below the severity threshold (repeatable).")] = None,
    exclude: Annotated[Optional[List[str]], typer.Option("--exclude", help="Substring that vetoes an event (repeatable).")] = None,
    default_rules: Annotated[bool, typer.Option("--default-rules/--no-default-rules", help="Include the built-in interesting-code list (BGP, link-down, crash, ...).")] = True,
    lookback: Annotated[str, typer.Option("--lookback", help="First-tick lookback window when a device has no cursor yet.")] = "15m",
    notify: Annotated[str, typer.Option("--notify", help="Slack channel/@user to post new alerts to (side-effecting — needs --yes).")] = "",
    max_events: Annotated[int, typer.Option("--max-events", help="Cap on new alerts surfaced/notified per device per tick.")] = 200,
    links: Annotated[bool, typer.Option("--links/--no-links", help="Also detect interface up/down via gNMI oper-status (snapshot diff).")] = True,
    gnmi_tls_mode: Annotated[str, typer.Option("--gnmi-tls-mode", help="TLS mode for the gNMI link read (insecure | skip_verify | verify_ca | mtls).")] = "skip_verify",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview only: parse + report, don't notify or advance the cursor.")] = False,
    user: O.User = None, password: O.Password = None, timeout: O.Timeout = None,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Run one event-collection tick over the fleet (system-events + gNMI links → dedupe → alert).

    For each device, reads its system-events log since the last tick (a
    persisted per-device cursor; first run uses --lookback) and — unless
    --no-links — the interface oper-status over gNMI, keeps the alert-worthy
    events (severity threshold OR an interesting code/message substring),
    dedupes against earlier ticks, and reports the new ones. Run it on a
    cron/loop, or use `monitor watch`.

    Read-only by default. Posting to Slack with --notify is side-effecting
    and requires --yes (skipped under --dry-run).
    """
    c = O.build_ctx(None, None, user, password, None, timeout, True, as_json, yes)
    if notify and not dry_run and not confirm.ensure(
        f"monitor tick --notify {notify}", yes=c.yes, as_json=c.json,
    ):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(
            monitor_tick, c,
            devices=device, severity=severity, match=match, exclude=exclude,
            use_default_rules=default_rules, lookback=lookback,
            notify_slack=notify, max_events_per_device=max_events,
            links=links, gnmi_tls_mode=gnmi_tls_mode, dry_run=dry_run,
        ),
        c,
    )


@monitor_app.command("watch")
def monitor_watch_cmd(
    device: Annotated[Optional[List[str]], typer.Option("--device", "-d", help="Device(s) to poll (repeatable). Default: every registered device.")] = None,
    interval: Annotated[float, typer.Option("--interval", help="Seconds to sleep between ticks.")] = 60.0,
    count: Annotated[int, typer.Option("--count", help="Number of ticks to run (0 = run forever until interrupted).")] = 0,
    severity: Annotated[str, typer.Option("--severity", help="Min syslog severity that alerts on its own.")] = _DEFAULT_SEVERITY,
    match: Annotated[Optional[List[str]], typer.Option("--match", help="Extra substring that marks an event alert-worthy (repeatable).")] = None,
    exclude: Annotated[Optional[List[str]], typer.Option("--exclude", help="Substring that vetoes an event (repeatable).")] = None,
    default_rules: Annotated[bool, typer.Option("--default-rules/--no-default-rules", help="Include the built-in interesting-code list.")] = True,
    lookback: Annotated[str, typer.Option("--lookback", help="First-tick lookback window when a device has no cursor yet.")] = "15m",
    notify: Annotated[str, typer.Option("--notify", help="Slack channel/@user to post new alerts to (side-effecting — needs --yes).")] = "",
    max_events: Annotated[int, typer.Option("--max-events", help="Cap on new alerts surfaced/notified per device per tick.")] = 200,
    links: Annotated[bool, typer.Option("--links/--no-links", help="Also detect interface up/down via gNMI oper-status.")] = True,
    gnmi_tls_mode: Annotated[str, typer.Option("--gnmi-tls-mode", help="TLS mode for the gNMI link read.")] = "skip_verify",
    user: O.User = None, password: O.Password = None, timeout: O.Timeout = None,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Run `monitor tick` in a loop (foreground collector daemon).

    Calls a tick every --interval seconds (--count times, or forever).
    With --json each tick prints its envelope as one JSON line (JSONL);
    otherwise a concise per-tick status line. Stop with Ctrl-C.

    --notify is side-effecting and requires --yes.
    """
    c = O.build_ctx(None, None, user, password, None, timeout, True, as_json, yes)
    if notify and not confirm.ensure(
        f"monitor watch --notify {notify}", yes=c.yes, as_json=c.json,
    ):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    i = 0
    try:
        while count <= 0 or i < count:
            env = O.call(
                monitor_tick, c,
                devices=device, severity=severity, match=match, exclude=exclude,
                use_default_rules=default_rules, lookback=lookback,
                notify_slack=notify, max_events_per_device=max_events,
                links=links, gnmi_tls_mode=gnmi_tls_mode, dry_run=False,
            )
            if c.json:
                typer.echo(_json.dumps(env))
            else:
                ts = _time.strftime("%H:%M:%S")
                typer.echo(
                    f"[{ts}] tick {i + 1}: {env.get('new_event_count', 0)} new, "
                    f"{env.get('notified', 0)} notified, status={env.get('status')}"
                )
            i += 1
            if count <= 0 or i < count:
                _time.sleep(max(0.0, interval))
    except KeyboardInterrupt:
        typer.echo("monitor watch stopped." if not c.json else _json.dumps({"status": "ok", "stopped": True, "ticks": i}))


@monitor_app.command("reset")
def monitor_reset_cmd(
    device: Annotated[Optional[List[str]], typer.Option("--device", "-d", help="Device(s) to clear (repeatable). Default: all.")] = None,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Clear collector memory (cursor + dedupe + link snapshot).

    Destructive (drops saved progress) — requires --yes. After a reset the
    next tick re-baselines from --lookback.
    """
    c = O.build_ctx(None, None, None, None, None, None, True, as_json, yes)
    target = ", ".join(device) if device else "ALL devices"
    if not confirm.ensure(f"monitor reset {target}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(monitor_reset, c, devices=device), c)


@core_app.command("list")
def core_list(
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
    log: O.Log = None,
):
    """List core-dump bundles on the device (`show file core list`, parsed)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    O.finish(O.call(list_cores, c), c)


@core_app.command("bt")
def core_bt(
    full_name: Annotated[str, typer.Argument(help="Full bundle name as printed by `core list`, e.g. routing_engine/core-bgpd.cpid-199103.sig-6.2026-07-02.11-46-37.tar.")],
    all_threads: Annotated[bool, typer.Option("--all-threads", help="Also run 'thread apply all bt' (full dump in stdout).")] = False,
    keep: Annotated[bool, typer.Option("--keep", help="Keep the scratch workdir on the device for manual digging.")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
    log: O.Log = None,
):
    """Extract a core bundle's backtrace via gdb on the device (writes device scratch — needs --yes).

    v1 supports routing_engine cores only (bgpd & friends). Extracts the
    tar into a scratch workdir, reads the crashed binary from the bundle's
    process.info, runs `gdb -batch -ex bt` (debuginfod disabled), greps
    the bundled stderr log for the assert line, then removes the workdir
    (--keep leaves it).
    """
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    if not confirm.ensure(f"core bt {full_name} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(get_core_backtrace, c, full_name=full_name,
               all_threads=all_threads, keep=keep, confirm=True),
        c,
    )


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
    log: O.Log = None,
):
    """List trace files."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
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
    log: O.Log = None,
):
    """Read a trace file with filters."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
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
    log: O.Log = None,
):
    """Run `ping` from the device."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
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
    log: O.Log = None,
):
    """Run one or a sequence of Linux commands via `run start shell`, then exit.

    Read-only inspection commands (grep / ps / cat / ldd / find ... — no
    redirection, command substitution, or write flags) run WITHOUT --yes.
    Anything that could write keeps the destructive gate and needs --yes.
    """
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    if not is_read_only_shell(commands):
        if not confirm.ensure(f"run start shell on {c.device or c.host}", yes=c.yes, as_json=c.json):
            raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(run_shell, c, commands=commands, ncc=ncc, ncp=ncp, ncm=ncm,
               container=container, continue_on_error=continue_on_error),
        c,
    )


@app.command()
def capture(
    device: Annotated[Optional[List[str]], typer.Option("--device", "-d", help="Device(s) to capture on (repeatable). Runs concurrently, one pcap per device.")] = None,
    mode: Annotated[str, typer.Option("--mode", help="Capture mode: routing (control-plane / routing-engine, default) or datapath (NCP wbox-cli).")] = "routing",
    duration: Annotated[str, typer.Option("--duration", help="Capture seconds, or 'inf'/'0' for as-long-as-possible (clamped, since a one-shot capture can't be Ctrl+C'd).")] = "30",
    name: Annotated[str, typer.Option("--name", help="pcap filename prefix; final name is <prefix>_<device>_<YYYYmmdd_HHMMSS>.pcap.")] = "capture",
    bpf: Annotated[Optional[str], typer.Option("--filter", help="BPF filter, e.g. 'host 1.2.3.4'. routing: applied ON THE DEVICE so the raw pcap lands already scoped. datapath: applied locally after download (writes a sibling *_filtered.pcap, keeps the raw). Recommended on every routing capture — otherwise it grabs the whole control plane.")] = None,
    iface: Annotated[str, typer.Option("--iface", help="routing mode only: tcpdump interface inside inband_ns (default 'any'). 'any' double-counts each packet across netns legs (dup-ACKs in Wireshark); pin the sub-if (e.g. g07008.0009 for ge400-7/0/8.9) for exactly one copy per packet = what the CPU sent/received.")] = "any",
    ncp: Annotated[Optional[str], typer.Option("--ncp", help="datapath NCP override; auto-detected from port-mirroring config when unset (falls back to 0).")] = None,
    host: O.Host = None, user: O.User = None, password: O.Password = None,
    timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """Capture packets on DNOS device(s); land one pcap per device on this host (DESTRUCTIVE — needs --yes).

    Two modes:

    - routing (default): a `timeout`-bounded tcpdump in the routing-engine
      container's inband_ns — captures in-band control-plane traffic
      (BGP/179, ISIS, LDP, ICMP, ...). No device config or physical setup.
      Note: the default `-i any` double-counts each packet across netns
      legs; pass `--iface <sub-if>` (e.g. g07008.0009) for a single clean
      copy per packet. (BFD is NCP-offloaded and not seen here.)
    - datapath: the NCP wbox-cli pcap engine, with a /tmp free-space
      preflight and a size cap. LAB PREREQUISITE (not automated): datapath
      capture needs a physical loop cable (or a DNAAS mirror chain)
      steering datapath packets into the capture; with no wiring the pcap
      opens but stays empty (surfaced as a warning).

    The pcap egresses straight to THIS host over the device→local-sftp path
    (same endpoint `cli backup` uses — run `qactl setup --check-local-sftp`
    to verify it) — no external hop. Writes device /tmp (and datapath
    toggles wbox-cli state), so it's gated by --yes.
    """
    c = O.build_ctx(None, host, user, password, None, timeout, True, as_json, yes)
    tgt = ", ".join(device) if device else (host or "?")
    if not confirm.ensure(f"cli capture --mode {mode} on {tgt}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(capture_devices, c, devices=device, mode=mode, duration=duration,
               name=name, bpf_filter=bpf, iface=iface, ncp=ncp),
        c,
    )


@app.command("ncm-cli")
def ncm_cli(
    commands: Annotated[List[str], typer.Argument(help="NCM CLI command(s) to run in order, e.g. 'show lldp neighbors' or 'configure' 'interface eth 0/5' 'shutdown'.")],
    ncm: Annotated[str, typer.Option("--ncm", help="Target NCM: A0 | B0 | ...")],
    answer: Annotated[str, typer.Option("--answer", help="Reply sent to a nested interactive confirm ([y/n]: / [yes/no]:), e.g. for 'copy running-config startup-config'. Default 'y'; pass 'n' to decline.")] = "y",
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Drive the NCM switch's nested (ICOS-style) CLI via `run start shell ncm`, then exit (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"run start shell ncm {ncm} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(run_ncm_cli, c, commands=commands, ncm=ncm, answer=answer), c)


@app.command("raw")
def raw(
    lines: Annotated[List[str], typer.Argument(help="Raw CLI line(s) sent verbatim, in order, on ONE channel. Each argument is one line (configure-mode lines need a preceding 'configure' line in the same call).")],
    continue_on_error: Annotated[bool, typer.Option("--continue-on-error", help="Run every line even after one errors (default: abort on first error).")] = False,
    answer_confirm: Annotated[Optional[str], typer.Option("--answer-confirm", help="Auto-answer interactive (yes/no)/[y/n] confirms a line raises with this reply, e.g. 'yes' for 'request system target-stack load'. Without it a confirming line times out — a follow-up 'yes' line can't answer it.")] = None,
    prompt_timeout: Annotated[Optional[float], typer.Option("--prompt-timeout", help="Seconds to coax a CLI prompt out of a fresh channel (slow/odd boxes, e.g. DNAAS-LEAF-B13). Overrides DNCTL_CLI_PROMPT_TIMEOUT.")] = None,
    banner_wait: Annotated[Optional[float], typer.Option("--banner-wait", help="Per-drain settle window while detecting the prompt. Overrides DNCTL_CLI_BANNER_WAIT.")] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Send raw CLI line(s) on one channel + return the full transcript (escape hatch; DESTRUCTIVE — needs --yes).

    For flows the structured show / show-config / config / shell tools don't
    cover. Lines run verbatim in order on one ephemeral channel; --json
    carries a per-line `steps` transcript. Interactive (yes/no) confirms
    need --answer-confirm yes (each line waits for the CLI prompt, so a
    'yes' line can't answer them). Tune prompt detection per-call with
    --prompt-timeout / --banner-wait.
    """
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"raw cli on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(run_raw, c, lines=lines, stop_on_error=not continue_on_error,
               answer_confirm=answer_confirm,
               prompt_timeout=prompt_timeout, banner_wait=banner_wait),
        c,
    )


@app.command("clear")
def clear(
    command: Annotated[List[str], typer.Argument(help="Operational clear command, e.g. 'clear arp' or 'clear bgp neighbor 1.2.3.4 soft in'.")],
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Run an operational `clear ...` command on the device (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    cmd = " ".join(command).strip()
    if not confirm.ensure(f"{cmd} on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(O.call(clear_tool, c, command=cmd), c)


# --- config (destructive) --------------------------------------------------


@app.command()
def config(
    statements: Annotated[List[str], typer.Argument(help="Configure-mode statements.")],
    check: Annotated[bool, typer.Option("--check", help="Dry-run via 'commit check' — no commit, not destructive (no --yes needed).")] = False,
    compare: Annotated[bool, typer.Option("--compare", help="Show the candidate-vs-running diff ('show config compare') without committing — not destructive (no --yes needed).")] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
    log: O.Log = None,
):
    """Apply configure-mode statements + commit; --check dry-runs, --compare previews the diff (both non-destructive; apply needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes, log)
    if compare:
        O.finish(O.call(edit_config_compare, c, statements=statements), c)
        return
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
    include: Annotated[Optional[List[str]], typer.Option(
        "--include",
        help=(
            "Extra info-types to bundle: basic | core-dumps | "
            "journal-files. Repeatable or comma-separated."
        ),
    )] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Create a tech-support bundle (long-running job)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    # block=True: the CLI process is the worker. Run the generate +
    # upload to completion in-process; a daemon thread would die when
    # the command returns, aborting the job mid-flight (issue #17).
    O.finish(O.call(create_techsupport, c, name=name, include=include, block=True), c)


@ts_app.command("upload")
def techsupport_upload(
    file: Annotated[str, typer.Argument(
        help="Tech-support filename on the device (as shown by "
             "'show system tech-support status').",
    )],
    name: Annotated[Optional[str], typer.Option(
        "--name",
        help="Bundle name for the dnftp filename (default: derived from FILE).",
    )] = None,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Upload an existing on-device tech-support file to dnftp (managed creds)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    O.finish(O.call(upload_techsupport, c, file=file, name=name), c)


@ts_app.command("show")
def techsupport_show(
    job_id: Annotated[Optional[str], typer.Argument(help="Job id (or use --device).")] = None,
    device: O.Device = None, as_json: O.Json = False,
):
    """Poll a tech-support job."""
    c = O.build_ctx(device=device, as_json=as_json)
    O.finish(O.call(get_techsupport_job, c, job_id=job_id), c)


@ts_app.command("list")
def techsupport_list(
    device: O.Device = None,
    limit: Annotated[int, typer.Option("--limit", help="Max bundles to return (newest first).")] = 100,
    as_json: O.Json = False,
):
    """List tech-support bundles stored on dnftp (optionally filtered by -d)."""
    c = O.build_ctx(device=device, as_json=as_json)
    O.finish(O.call(list_techsupports, c, limit=limit), c)


# --- tar-load --------------------------------------------------------------


@tarload_app.command("start")
def tar_load_start(
    jenkins_url: Annotated[str, typer.Argument(help="Jenkins artifact URL of the tar image.")],
    no_pre_check: Annotated[bool, typer.Option("--no-pre-check")] = False,
    component: Annotated[Optional[List[str]], typer.Option(
        "--component", "-c",
        help=(
            "Component(s) to load: baseos | dnos | gi. Repeatable. "
            "Omit to load all available (base_os optional, dnos+gi "
            "required). When given, only the listed components are "
            "loaded (in baseos->dnos->gi order) and each is required."
        ),
    )] = None,
    no_wait: Annotated[bool, typer.Option(
        "--no-wait",
        help=(
            "Kick off the load and return immediately with the job "
            "handle; a session-detached local worker keeps driving the "
            "on-device loads strictly serially (survives shell "
            "timeouts). Poll with 'tar-load show <job_id>' or "
            "'tar-load show -d <device>'."
        ),
    )] = False,
    device: O.Device = None, host: O.Host = None, user: O.User = None,
    password: O.Password = None, port: O.Port = None, timeout: O.Timeout = None,
    no_verify: O.NoVerify = True, as_json: O.Json = False, yes: O.Yes = False,
):
    """Load a system image tar (DESTRUCTIVE — needs --yes)."""
    c = O.build_ctx(device, host, user, password, port, timeout, no_verify, as_json, yes)
    if not confirm.ensure(f"tar-load on {c.device or c.host}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    # block=True: the CLI process is the worker. Run the load to
    # completion in-line and return the terminal envelope (issue #17).
    # --no-wait flips that to detach=True: the worker forks into a
    # session-detached child that persists live progress, and the
    # kickoff envelope returns immediately (issue #76).
    # confirm=True: the --yes gate above already confirmed; the tool's
    # own confirm gate would otherwise short-circuit to a dry-run.
    # components: None (no -c given) keeps the load-all default; a
    # non-empty list restricts + hard-requires the listed components.
    O.finish(
        O.call(
            request_system_tar_load, c,
            jenkins_url=jenkins_url, pre_check=not no_pre_check,
            components=component or None,
            confirm=True, block=not no_wait, detach=no_wait,
        ),
        c,
    )


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
    O.finish(O.call(request_system_pre_check, c, block=True), c)


# --- device registry -------------------------------------------------------


@device_app.command("list")
def device_list(as_json: O.Json = False):
    """List devices known to the registry."""
    O.finish(list_devices(), O.build_ctx(as_json=as_json))


@device_app.command("add")
def device_add(
    name: Annotated[str, typer.Argument(help="Registry name for this device (the name you choose). Becomes the key; independent of the chassis System Name.")],
    host: Annotated[Optional[str], typer.Option("--host", help="SSH host / IP / SN to probe. Defaults to NAME.")] = None,
    alias: Annotated[Optional[List[str]], typer.Option("--alias", help="Secondary nickname to attach (repeatable), e.g. --alias cl.")] = None,
    rack: Annotated[Optional[str], typer.Option("--rack", help="Manual rack override (e.g. B13). Default: auto-discover via LLDP.")] = None,
    no_discover: Annotated[bool, typer.Option("--no-discover", help="Skip LLDP location auto-discovery (rack/mgmt-switch/fabric-leaf).")] = False,
    vendor: Annotated[str, typer.Option("--vendor", help="Device vendor: dnos (default), cisco, juniper, or arista. Non-DNOS devices skip the DNOS probe + initial backup.")] = "dnos",
    user: O.User = None, password: O.Password = None,
    timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """Probe a chassis and add it to the registry (DESTRUCTIVE — needs --yes).

    The registry key is NAME — the name you choose — and is independent of
    the chassis's configured System Name (so renaming the box never
    orphans its registry entry). The chassis System Name is captured as
    metadata only. The SSH probe targets --host, defaulting to NAME.

    Attach secondary nicknames inline with one or more --alias flags
    (e.g. `device add HCL --host 100.64.10.252 --alias cl`); `-d cl` then
    reaches the same box. More nicknames can be added later with
    `device alias`.

    Physical location (rack / mgmt switch / fabric leaf) is auto-discovered
    from `show lldp neighbors` and stored on the entry; pass --rack to
    override the rack or --no-discover to skip the LLDP probe entirely.

    --vendor defaults to dnos. Pass --vendor cisco/juniper/arista for a
    non-DNOS box: the DNOS probe and initial backup are skipped and the
    device is recorded for inventory only.
    """
    c = O.build_ctx(user=user, password=password, timeout=timeout, as_json=as_json, yes=yes)
    if not confirm.ensure(f"device add {name}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(
            manage_device, c, operation="add", name=name, sn=host or name,
            aliases=alias, rack=rack, discover=not no_discover, vendor=vendor,
        ),
        c,
    )


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


@device_app.command("name-check")
def device_name_check(
    name: Annotated[str, typer.Argument(help="Device to check (registry name or alias).")],
    sync: Annotated[bool, typer.Option("--sync", help="Adopt the chassis System Name by renaming the registry key (mutating — needs --yes).")] = False,
    keep_old_alias: Annotated[bool, typer.Option("--keep-old-alias/--drop-old-alias", help="On --sync, keep the old name as a secondary alias (default: keep).")] = True,
    user: O.User = None, password: O.Password = None,
    timeout: O.Timeout = None, as_json: O.Json = False, yes: O.Yes = False,
):
    """Check the registry name vs the chassis System Name (optionally --sync).

    Read-only by default: SSH-probes the device and reports whether its
    registry name still matches the chassis System Name (`in_sync`). Pass
    --sync to adopt the chassis name — renames the registry key in place,
    keeping the old name as a secondary alias — which mutates the registry
    and therefore needs --yes.
    """
    c = O.build_ctx(user=user, password=password, timeout=timeout, as_json=as_json, yes=yes)
    if sync and not confirm.ensure(f"device name-check {name} --sync", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        O.call(
            manage_device, c, operation="name-check", name=name,
            sync=sync, keep_old_alias=keep_old_alias,
        ),
        c,
    )


@device_app.command("rename")
def device_rename(
    old: Annotated[str, typer.Argument(help="Current (stale) device name.")],
    new: Annotated[str, typer.Argument(help="New canonical name (the chassis System Name).")],
    keep_old_alias: Annotated[bool, typer.Option("--keep-old-alias/--drop-old-alias", help="Keep the old name as a secondary alias (default: keep).")] = True,
    as_json: O.Json = False, yes: O.Yes = False,
):
    """Rename a device's canonical key in place (DESTRUCTIVE — needs --yes).

    Use when a chassis's System Name changed: moves the whole registry
    entry (creds / expected_sns / history) with no re-probe. The old
    name stays a secondary alias unless --drop-old-alias is given.
    """
    c = O.build_ctx(as_json=as_json, yes=yes)
    if not confirm.ensure(f"device rename {old} -> {new}", yes=c.yes, as_json=c.json):
        raise typer.Exit(confirm.REFUSAL_EXIT)
    O.finish(
        manage_device(
            operation="rename", name=old, new_name=new,
            keep_old_alias=keep_old_alias,
        ),
        c,
    )


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
