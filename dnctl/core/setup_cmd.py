"""``dnctl setup`` — write credentials / keys / dnftp config to disk.

``dnctl`` ships no secrets in source (see :mod:`dnctl.core.credentials`
and :mod:`dnctl.core.config`). This command is how a user supplies them
once, into ``~/.config/dnctl/config.toml`` (mode 0600). Values can also
be supplied per-run via env vars or ``--user`` / ``--password`` flags;
the config file is just the persistent layer.

Usage::

    dnctl setup                 # interactive wizard
    dnctl setup --password ...  # non-interactive: set only what's passed
    dnctl setup --show          # print resolved sources (secrets redacted)
    dnctl setup --path          # print the config-file path
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

import typer

from dnctl.core import config as _config


# (env var, [section].key, built-in default, is_secret, prompt label)
_Setting = Tuple[str, str, str, Optional[str], bool, str]

SETTINGS: List[_Setting] = [
    ("DNCTL_USER", "auth", "user", "dnroot", False, "SSH / API user"),
    ("DNCTL_PASSWORD", "auth", "password", "dnroot", True, "Password"),
    ("DNCTL_SSH_KEY", "auth", "ssh_key", None, False, "SSH private-key path (blank to skip)"),
    ("DNCTL_NETCONF_USER", "netconf", "user", "netconf", False, "NETCONF fallback user"),
    ("DNCTL_NETCONF_PASSWORD", "netconf", "password", None, True, "NETCONF fallback password (blank to skip)"),
    ("DNCTL_DNFTP_HOST", "dnftp", "host", "dnftp", False, "dnftp host"),
    ("DNCTL_DNFTP_USER", "dnftp", "user", "dn", False, "dnftp user"),
    ("DNCTL_DNFTP_PASSWORD", "dnftp", "password", None, True, "dnftp password (blank to skip)"),
    ("DNCTL_DNFTP_VRF", "dnftp", "vrf", "mgmt0", False, "dnftp VRF"),
]

_SECTION_ORDER = ["auth", "netconf", "dnftp"]


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _emit_toml(data: Dict[str, Dict[str, str]]) -> str:
    lines: List[str] = []
    sections = [s for s in _SECTION_ORDER if data.get(s)]
    sections += [s for s in data if s not in _SECTION_ORDER and data.get(s)]
    for sec in sections:
        lines.append(f"[{sec}]")
        for key, val in data[sec].items():
            lines.append(f'{key} = "{_toml_escape(val)}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_config(values: Dict[str, Dict[str, str]]) -> str:
    """Merge ``values`` into the existing config file and write it 0600."""
    merged: Dict[str, Dict[str, str]] = {}
    for sec, body in _config.load_config().items():
        if isinstance(body, dict):
            merged[sec] = {k: str(v) for k, v in body.items()}
    for sec, body in values.items():
        merged.setdefault(sec, {})
        for k, v in body.items():
            if v == "":
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


def setup(
    show: bool = typer.Option(False, "--show", help="Print resolved config sources (secrets redacted)."),
    path: bool = typer.Option(False, "--path", help="Print the config-file path and exit."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output (with --show)."),
    user: Optional[str] = typer.Option(None, "--user", help="SSH / API user."),
    password: Optional[str] = typer.Option(None, "--password", help="Password."),
    ssh_key: Optional[str] = typer.Option(None, "--ssh-key", help="SSH private-key path."),
    netconf_user: Optional[str] = typer.Option(None, "--netconf-user", help="NETCONF fallback user."),
    netconf_password: Optional[str] = typer.Option(None, "--netconf-password", help="NETCONF fallback password."),
    dnftp_host: Optional[str] = typer.Option(None, "--dnftp-host", help="dnftp host."),
    dnftp_user: Optional[str] = typer.Option(None, "--dnftp-user", help="dnftp user."),
    dnftp_password: Optional[str] = typer.Option(None, "--dnftp-password", help="dnftp password."),
    dnftp_vrf: Optional[str] = typer.Option(None, "--dnftp-vrf", help="dnftp VRF."),
) -> None:
    """Write credentials / keys / dnftp config, or inspect what's resolved."""
    if path:
        typer.echo(str(_config.config_path()))
        return
    if show:
        _show(as_json)
        return

    flag_map = {
        ("auth", "user"): user,
        ("auth", "password"): password,
        ("auth", "ssh_key"): ssh_key,
        ("netconf", "user"): netconf_user,
        ("netconf", "password"): netconf_password,
        ("dnftp", "host"): dnftp_host,
        ("dnftp", "user"): dnftp_user,
        ("dnftp", "password"): dnftp_password,
        ("dnftp", "vrf"): dnftp_vrf,
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
