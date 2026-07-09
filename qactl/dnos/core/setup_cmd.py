"""``qactl setup`` — write credentials / keys / dnftp config to disk.

``qactl`` ships no secrets in source (see :mod:`qactl.core.credentials`
and :mod:`qactl.core.config`). This command is how a user supplies them
once, into ``~/.config/qactl/config.toml`` (mode 0600). Values can also
be supplied per-run via env vars or ``--user`` / ``--password`` flags;
the config file is just the persistent layer.

Usage::

    qactl setup                    # interactive wizard
    qactl setup --password ...     # non-interactive: set only what's passed
    qactl setup --password -       # secret-safe: read the password from stdin
                                   # (pipe), or a hidden prompt on a TTY
    qactl setup --device jun-rt02 --user ... --password ...
                                   # per-device creds ([devices."<name>"])
    qactl setup --show             # print resolved sources (secrets redacted)
    qactl setup --check-local-sftp # verify the local backup/restore SFTP endpoint
    qactl setup --path             # print the config-file path
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import typer

from qactl.dnos.core import config as _config


# (env var, [section].key, built-in default, is_secret, prompt label)
_Setting = Tuple[str, str, str, Optional[str], bool, str]

SETTINGS: List[_Setting] = [
    ("QACTL_USER", "auth", "user", "dnroot", False, "SSH / API user"),
    ("QACTL_PASSWORD", "auth", "password", "dnroot", True, "Password"),
    ("QACTL_SSH_KEY", "auth", "ssh_key", None, False, "SSH private-key path (blank to skip)"),
    ("QACTL_DNFTP_HOST", "dnftp", "host", "dnftp", False, "dnftp host"),
    ("QACTL_DNFTP_USER", "dnftp", "user", "dn", False, "dnftp user"),
    ("QACTL_DNFTP_PASSWORD", "dnftp", "password", None, True, "dnftp password (blank to skip)"),
    ("QACTL_DNFTP_VRF", "dnftp", "vrf", "mgmt0", False, "dnftp VRF"),
    ("QACTL_LOCAL_SFTP_HOST", "local", "host", None, False, "Local SFTP host the device uploads to (blank = auto FQDN)"),
    ("QACTL_LOCAL_SFTP_USER", "local", "user", None, False, "Local SFTP user (blank = current OS user)"),
    ("QACTL_LOCAL_SFTP_PASSWORD", "local", "password", None, True, "Local SFTP password for config backups (blank to skip)"),
    ("QACTL_LOCAL_SFTP_VRF", "local", "vrf", "mgmt0", False, "Local SFTP VRF"),
    ("QACTL_LOCAL_SFTP_PORT", "local", "port", "22", False, "Local SFTP/SSH port the device connects to"),
]

_SECTION_ORDER = ["auth", "dnftp", "local"]


def _toml_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _emit_toml(data: Dict[str, Dict[str, object]]) -> str:
    """Serialise the two-level config: scalar keys plus one nesting level
    of sub-tables (``[devices."<name>"]``) whose bodies are flat."""
    lines: List[str] = []
    sections = [s for s in _SECTION_ORDER if data.get(s)]
    sections += [s for s in data if s not in _SECTION_ORDER and data.get(s)]
    for sec in sections:
        scalars = {k: v for k, v in data[sec].items() if not isinstance(v, dict)}
        tables = {k: v for k, v in data[sec].items() if isinstance(v, dict)}
        if scalars or not tables:
            lines.append(f"[{sec}]")
            for key, val in scalars.items():
                lines.append(f'{key} = "{_toml_escape(str(val))}"')
            lines.append("")
        for name, body in tables.items():
            lines.append(f'[{sec}."{_toml_escape(name)}"]')
            for key, val in body.items():
                lines.append(f'{key} = "{_toml_escape(str(val))}"')
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_config(values: Dict[str, Dict[str, object]]) -> str:
    """Merge ``values`` into the existing config file and write it 0600.

    An empty-string value deletes the key (and an emptied sub-table is
    dropped), matching the interactive wizard's "blank clears" contract.
    """
    merged: Dict[str, Dict[str, object]] = {}
    for sec, body in _config.load_config().items():
        if isinstance(body, dict):
            merged[sec] = {
                k: (
                    {ik: str(iv) for ik, iv in v.items()}
                    if isinstance(v, dict)
                    else str(v)
                )
                for k, v in body.items()
            }
    for sec, body in values.items():
        merged.setdefault(sec, {})
        for k, v in body.items():
            if isinstance(v, dict):
                inner = merged[sec].get(k)
                if not isinstance(inner, dict):
                    inner = {}
                merged[sec][k] = inner
                for ik, iv in v.items():
                    if iv == "":
                        inner.pop(ik, None)
                    else:
                        inner[ik] = iv
                if not inner:
                    merged[sec].pop(k, None)
            elif v == "":
                merged[sec].pop(k, None)
            else:
                merged[sec][k] = v
    merged = {s: b for s, b in merged.items() if b}

    path = _config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_emit_toml(merged), encoding="utf-8")
    os.chmod(path, 0o600)
    _config.load_config.cache_clear()
    return str(path)


def _resolve_secret(
    value: Optional[str], label: str, stdin_state: Dict[str, str],
) -> Optional[str]:
    """Expand a secret flag passed as ``-`` so it never lands on argv.

    On a TTY the value is taken from a hidden prompt; otherwise it is read
    from stdin (one trailing newline stripped). Only one flag per invocation
    can consume stdin, and an empty stdin read is an error — clearing a key
    stays explicit (``--password ""``).
    """
    if value != "-":
        return value
    if sys.stdin.isatty():
        return typer.prompt(label, hide_input=True)
    if stdin_state:
        typer.echo(
            f"only one secret flag can read stdin ('-'); "
            f"both {stdin_state['owner']} and {label} asked for it."
        )
        raise typer.Exit(code=2)
    stdin_state["owner"] = label
    data = sys.stdin.read()
    if data.endswith("\n"):
        data = data[:-1]
        if data.endswith("\r"):
            data = data[:-1]
    if not data:
        typer.echo(f"{label}: '-' given but stdin was empty.")
        raise typer.Exit(code=2)
    return data


def _redact(value: Optional[str], is_secret: bool) -> Optional[str]:
    if value is None:
        return None
    if not is_secret:
        return value
    return "***set***"


def _show(as_json: bool) -> None:
    rows = []
    for env_key, section, key, default, is_secret, _label in SETTINGS:
        source = _config.resolved_source(env_key, section, key, default)
        value = _config.resolve(env_key, section, key, default)
        rows.append({
            "setting": f"[{section}].{key}",
            "env": env_key,
            "source": source,
            "value": _redact(value, is_secret),
        })
    devices_cfg = _config.load_config().get("devices")
    if isinstance(devices_cfg, dict):
        for name in sorted(devices_cfg):
            body = devices_cfg[name]
            if not isinstance(body, dict):
                continue
            for key in sorted(body):
                setting = f'[devices."{name}"].{key}'
                rows.append({
                    "setting": setting,
                    "env": "-",
                    "source": f"config:{setting}",
                    "value": _redact(str(body[key]), key == "password"),
                })
    if as_json:
        typer.echo(json.dumps(
            {"config_path": str(_config.config_path()), "settings": rows},
            indent=2,
        ))
        return
    typer.echo(f"config: {_config.config_path()}")
    width = max(len(r["setting"]) for r in rows)
    for r in rows:
        val = r["value"] if r["value"] is not None else "(unset)"
        typer.echo(f"  {r['setting']:<{width}}  {r['source']:<24}  {val}")


def _check_local_sftp(as_json: bool) -> None:
    """Self-check the local SFTP endpoint the device dials back into.

    Restore (and backup) drive the device to SFTP to *this* host, so the
    two preconditions the tool can verify locally are: (1) ``[local].password``
    is set (the device needs it at the prompt) and (2) an sshd/SFTP server
    is actually listening at the resolved ``host:port``. Reports both, plus
    the device-side reachability check the agent still has to run, and exits
    non-zero if either local precondition fails so callers can gate on it.
    """
    from qactl.dnos.core import local_sftp

    s = local_sftp.resolve_local_sftp()
    reachable, detail = local_sftp.probe_endpoint(s.host, s.port)

    checks = [
        {
            "check": "password_configured",
            "ok": s.password_set,
            "detail": (
                "[local].password is set"
                if s.password_set
                else "[local].password is unset — the device can't authenticate "
                "to our sshd. Run `qactl setup` and set the local SFTP password."
            ),
        },
        {
            "check": "endpoint_listening",
            "ok": reachable,
            "detail": detail
            + (
                ""
                if reachable
                else " — start/enable sshd on this host (or fix "
                "[local].host/port) so the device can download the config."
            ),
        },
    ]
    ok = all(c["ok"] for c in checks)
    device_hint = (
        f"From the lab device, confirm it can route to us in the backup VRF: "
        f"run_ping_ipv4 dest={s.host} vrf={s.vrf} (this check runs on the "
        f"agent host and can't see the device's routing table)."
    )

    if as_json:
        typer.echo(json.dumps(
            {
                "status": "ok" if ok else "error",
                "endpoint": {
                    "host": s.host,
                    "host_source": s.host_source,
                    "user": s.user,
                    "port": s.port,
                    "vrf": s.vrf,
                    "password_set": s.password_set,
                },
                "checks": checks,
                "device_reachability_hint": device_hint,
            },
            indent=2,
        ))
    else:
        typer.echo(f"local SFTP endpoint: {s.user}@{s.host}:{s.port} (vrf {s.vrf})")
        for c in checks:
            mark = "ok " if c["ok"] else "FAIL"
            typer.echo(f"  [{mark}] {c['check']}: {c['detail']}")
        typer.echo(f"  note: {device_hint}")
    if not ok:
        raise typer.Exit(code=1)


def setup(
    show: bool = typer.Option(False, "--show", help="Print resolved config sources (secrets redacted)."),
    check_local_sftp: bool = typer.Option(False, "--check-local-sftp", help="Verify the local SFTP endpoint used for backup/restore is configured and listening."),
    path: bool = typer.Option(False, "--path", help="Print the config-file path and exit."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output (with --show)."),
    user: Optional[str] = typer.Option(None, "--user", help="SSH / API user."),
    password: Optional[str] = typer.Option(None, "--password", help="Password ('-' reads stdin, or a hidden prompt on a TTY)."),
    device: Optional[str] = typer.Option(None, "--device", help="Scope --user/--password to this registry device ([devices.\"<name>\"]) instead of the global [auth]. Empty value clears the key."),
    ssh_key: Optional[str] = typer.Option(None, "--ssh-key", help="SSH private-key path."),
    dnftp_host: Optional[str] = typer.Option(None, "--dnftp-host", help="dnftp host."),
    dnftp_user: Optional[str] = typer.Option(None, "--dnftp-user", help="dnftp user."),
    dnftp_password: Optional[str] = typer.Option(None, "--dnftp-password", help="dnftp password ('-' reads stdin, or a hidden prompt on a TTY)."),
    dnftp_vrf: Optional[str] = typer.Option(None, "--dnftp-vrf", help="dnftp VRF."),
    local_sftp_host: Optional[str] = typer.Option(None, "--local-sftp-host", help="Local SFTP host the device uploads to (blank = auto FQDN)."),
    local_sftp_user: Optional[str] = typer.Option(None, "--local-sftp-user", help="Local SFTP user (blank = current OS user)."),
    local_sftp_password: Optional[str] = typer.Option(None, "--local-sftp-password", help="Local SFTP password for config backups ('-' reads stdin, or a hidden prompt on a TTY)."),
    local_sftp_vrf: Optional[str] = typer.Option(None, "--local-sftp-vrf", help="Local SFTP VRF."),
    local_sftp_port: Optional[str] = typer.Option(None, "--local-sftp-port", help="Local SFTP/SSH port the device connects to."),
) -> None:
    """Write credentials / keys / dnftp / local config, or inspect what's resolved."""
    if path:
        typer.echo(str(_config.config_path()))
        return
    if show:
        _show(as_json)
        return
    if check_local_sftp:
        _check_local_sftp(as_json)
        return

    stdin_state: Dict[str, str] = {}
    password = _resolve_secret(password, "Password", stdin_state)
    dnftp_password = _resolve_secret(dnftp_password, "dnftp password", stdin_state)
    local_sftp_password = _resolve_secret(
        local_sftp_password, "Local SFTP password", stdin_state,
    )

    if device:
        # Per-device creds (vendor boxes etc.) — see qactl.core.credentials.
        dev_vals: Dict[str, str] = {}
        if user is not None:
            dev_vals["user"] = user
        if password is not None:
            dev_vals["password"] = password
        if not dev_vals:
            typer.echo("--device needs --user and/or --password (empty value clears).")
            raise typer.Exit(code=2)
        from qactl.dnos.core import devices as _devices

        canonical = _devices.resolve_canonical(device)
        if canonical is None:
            typer.echo(
                f"note: '{device}' is not in the device registry — storing "
                f"creds under that name anyway (check for a typo)."
            )
            canonical = device
        written = _write_config({"devices": {canonical: dev_vals}})
        typer.echo(f"Wrote {written} ([devices.\"{canonical}\"])")
        return

    flag_map = {
        ("auth", "user"): user,
        ("auth", "password"): password,
        ("auth", "ssh_key"): ssh_key,
        ("dnftp", "host"): dnftp_host,
        ("dnftp", "user"): dnftp_user,
        ("dnftp", "password"): dnftp_password,
        ("dnftp", "vrf"): dnftp_vrf,
        ("local", "host"): local_sftp_host,
        ("local", "user"): local_sftp_user,
        ("local", "password"): local_sftp_password,
        ("local", "vrf"): local_sftp_vrf,
        ("local", "port"): local_sftp_port,
    }
    provided = {k: v for k, v in flag_map.items() if v is not None}

    values: Dict[str, Dict[str, str]] = {}
    if provided:
        # Non-interactive: only touch what was passed.
        for (section, key), val in provided.items():
            values.setdefault(section, {})[key] = val
    else:
        # Interactive wizard. Current resolved value is the prompt default;
        # secrets are entered hidden and never echoed back as defaults.
        typer.echo(f"Writing {_config.config_path()} (mode 0600). Blank keeps current.\n")
        for env_key, section, key, default, is_secret, label in SETTINGS:
            current = _config.resolve(env_key, section, key, default)
            if is_secret:
                entered = typer.prompt(
                    label, default="", hide_input=True, show_default=False,
                )
                if entered != "":
                    values.setdefault(section, {})[key] = entered
            else:
                entered = typer.prompt(label, default=current or "")
                if entered != (current or ""):
                    values.setdefault(section, {})[key] = entered

    if not values:
        typer.echo("Nothing to write.")
        return
    written = _write_config(values)
    typer.echo(f"Wrote {written}")
