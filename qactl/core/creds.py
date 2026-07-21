"""Runtime credential resolution — no secrets ever live in this repo.

Each domain reads its credentials from the environment at call time.
Tokens are local to the user (shell env, a sourced file, a secrets
manager, CI secrets, …) and are never committed or baked into source.

Atlassian (one token covers both Jira and Confluence — same site):
    ATLASSIAN_EMAIL       required   account email
    ATLASSIAN_API_TOKEN   required   token from id.atlassian.com
    ATLASSIAN_BASE_URL    optional   default https://drivenets.atlassian.net

Jenkins:
    JENKINS_USER          required   Jenkins user id
    JENKINS_API_TOKEN     required   Jenkins API token
    JENKINS_URL           optional   default https://jenkins.dev.drivenets.net

Arista EOS (SSH; host is always given per command):
    ARISTA_USER           optional   default admin
    ARISTA_PASSWORD       optional   default empty

Device42 CMDB (read-only inventory / rack lookup):
    DEVICE42_ENDPOINT     required   DOQL query URL, e.g.
                                     https://device42.dev.drivenets.net/services/data/v1.0/query/
    DEVICE42_AUTH         required   full Basic-auth header value,
                                     i.e. "Basic <base64(user:pass)>"

Each resolver raises :class:`CredentialError` (a ``ValueError``) listing
the missing variables; the CLI turns that into a ``bad_argument``
envelope with a ``next_actions`` hint instead of a traceback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple


ATLASSIAN_DEFAULT_BASE_URL = "https://drivenets.atlassian.net"
JENKINS_DEFAULT_URL = "https://jenkins.dev.drivenets.net"

# The lab's Device42 + console-server + PDU credentials live in ~/.console_env
# (a `KEY=value` / `export KEY=value` file), the same file the legacy `console`
# tool sources. Interactive shells don't necessarily export it, so the Device42
# / console-server resolvers source it lazily with setdefault semantics — a
# value already in the environment always wins, and nothing is printed.
_CONSOLE_ENV_LOADED = False


def _load_console_env(path: str = "~/.console_env") -> None:
    """Source ``export KEY=value`` lines from ~/.console_env into os.environ.

    Idempotent and best-effort: a missing/unreadable file is ignored, and an
    env var already set is never overwritten (``setdefault``)."""
    global _CONSOLE_ENV_LOADED
    if _CONSOLE_ENV_LOADED:
        return
    _CONSOLE_ENV_LOADED = True
    try:
        with open(os.path.expanduser(path)) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


class CredentialError(ValueError):
    """Raised when required credentials are absent from the environment."""


def _missing(pairs: List[Tuple[str, str]]) -> List[str]:
    return [name for name, value in pairs if not value]


@dataclass
class AtlassianConfig:
    base_url: str
    email: str
    api_token: str

    @classmethod
    def resolve(
        cls,
        *,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> "AtlassianConfig":
        email = (email or os.environ.get("ATLASSIAN_EMAIL") or "").strip()
        api_token = (api_token or os.environ.get("ATLASSIAN_API_TOKEN") or "").strip()
        base_url = (
            base_url or os.environ.get("ATLASSIAN_BASE_URL") or ATLASSIAN_DEFAULT_BASE_URL
        ).rstrip("/")
        missing = _missing([("ATLASSIAN_EMAIL", email), ("ATLASSIAN_API_TOKEN", api_token)])
        if missing:
            raise CredentialError(
                f"Missing Atlassian credentials in the environment: "
                f"{', '.join(missing)}. Export them (one token serves both "
                f"Jira and Confluence): "
                f"export ATLASSIAN_EMAIL=you@example.com "
                f"ATLASSIAN_API_TOKEN=ATATT3x... "
                f"(optional ATLASSIAN_BASE_URL, default {ATLASSIAN_DEFAULT_BASE_URL})."
            )
        return cls(base_url=base_url, email=email, api_token=api_token)


@dataclass
class JenkinsConfig:
    url: str
    user: str
    token: str

    @classmethod
    def resolve(
        cls,
        *,
        user: Optional[str] = None,
        token: Optional[str] = None,
        url: Optional[str] = None,
    ) -> "JenkinsConfig":
        user = (user or os.environ.get("JENKINS_USER") or "").strip()
        token = (token or os.environ.get("JENKINS_API_TOKEN") or "").strip()
        url = (url or os.environ.get("JENKINS_URL") or JENKINS_DEFAULT_URL).rstrip("/")
        missing = _missing([("JENKINS_USER", user), ("JENKINS_API_TOKEN", token)])
        if missing:
            raise CredentialError(
                f"Missing Jenkins credentials in the environment: "
                f"{', '.join(missing)}. Export them: "
                f"export JENKINS_USER=<your-id> JENKINS_API_TOKEN=<token> "
                f"(optional JENKINS_URL, default {JENKINS_DEFAULT_URL})."
            )
        return cls(url=url, user=user, token=token)


ARISTA_DEFAULT_USER = "admin"


@dataclass
class AristaConfig:
    host: str
    user: str
    password: str
    port: int = 22

    @classmethod
    def resolve(
        cls,
        host: str,
        *,
        user: Optional[str] = None,
        password: Optional[str] = None,
        port: Optional[int] = None,
    ) -> "AristaConfig":
        host = (host or "").strip()
        if not host:
            raise CredentialError("An Arista host (name or IP) is required.")
        user = (user or os.environ.get("ARISTA_USER") or ARISTA_DEFAULT_USER).strip()
        # Empty is allowed — a wrong/absent password surfaces as an SSH
        # auth failure with an env-var hint, not here.
        password = password if password is not None else os.environ.get("ARISTA_PASSWORD", "")
        return cls(host=host, user=user, password=password, port=int(port or 22))


CONSOLE_SERVER_DEFAULT_USER = "dn"


@dataclass
class ConsoleServerConfig:
    """Terminal-server (console server) SSH login for interactive connect.

    A wrong/absent password surfaces as an SSH auth failure at connect time
    (with an env-var hint), not here — mirroring :class:`AristaConfig`.

        CONSOLE_CS_USER       optional   default 'dn'
        CONSOLE_CS_PASSWORD   optional   default empty
    """

    user: str
    password: str

    @classmethod
    def resolve(
        cls,
        *,
        user: Optional[str] = None,
        password: Optional[str] = None,
    ) -> "ConsoleServerConfig":
        _load_console_env()
        user = (user or os.environ.get("CONSOLE_CS_USER") or
                CONSOLE_SERVER_DEFAULT_USER).strip()
        password = (password if password is not None
                    else os.environ.get("CONSOLE_CS_PASSWORD", ""))
        return cls(user=user, password=password)


PDU_DEFAULT_USER = "dn"
# Hosts whose CLI speaks the APC-style ``olOn/olOff/olStatus`` dialect; every
# other PDU defaults to the ``dev outlet 1 <n> …`` dialect. Keyed by the
# rack-form name (``pdu-<rack>-<n>``) so it matches both the legacy names and
# the post-migration ``{Site}{NN}-PDU-{RACK}-{N}`` names (normalized the same
# way). Overridable via a JSON file at $QACTL_PDU_CLI_CONFIG / $PDU_CLI_CONFIG_PATH
# ({"ol": [...], "dev_outlet": [...]}).
PDU_OL_DIALECT_HOSTS = frozenset({"pdu-b10-1", "m-wb-power101b"})


@dataclass
class PduConfig:
    """PDU SSH login + per-host CLI dialect map for outlet power control.

    Two passwords (primary + alt) are tried in order, matching the legacy
    console tool. A wrong/absent password surfaces as an SSH auth failure at
    connect time.

        CONSOLE_PDU_USER          optional   default 'dn'
        CONSOLE_PDU_PASSWORD      optional   primary password
        CONSOLE_PDU_PASSWORD_ALT  optional   fallback password
        QACTL_PDU_CLI_CONFIG /
        PDU_CLI_CONFIG_PATH       optional   JSON dialect map override
    """

    user: str
    password: str
    password_alt: str
    ol_hosts: frozenset

    @classmethod
    def resolve(
        cls,
        *,
        user: Optional[str] = None,
        password: Optional[str] = None,
        password_alt: Optional[str] = None,
    ) -> "PduConfig":
        _load_console_env()
        user = (user or os.environ.get("CONSOLE_PDU_USER") or PDU_DEFAULT_USER).strip()
        password = (password if password is not None
                    else os.environ.get("CONSOLE_PDU_PASSWORD", ""))
        password_alt = (password_alt if password_alt is not None
                        else os.environ.get("CONSOLE_PDU_PASSWORD_ALT", ""))
        return cls(user=user, password=password, password_alt=password_alt,
                   ol_hosts=_load_pdu_ol_hosts())


def _pdu_rack_key(name: str) -> str:
    """Normalize a PDU name to its rack-form key ``pdu-<rack>-<n>``.

    Handles both the new ``{Site}{NN}-PDU-{RACK}-{N}`` scheme (take the part
    after ``-PDU-``) and the legacy ``pdu-…`` / bare names — so the dialect map
    keeps matching across the hostname migration."""
    s = (name or "").strip().lower()
    if "-pdu-" in s:
        s = "pdu-" + s.split("-pdu-", 1)[1]
    elif not s.startswith("pdu-") and not s.startswith("m-wb-power"):
        s = "pdu-" + s
    return s


def _load_pdu_ol_hosts() -> frozenset:
    path = os.environ.get("QACTL_PDU_CLI_CONFIG") or os.environ.get("PDU_CLI_CONFIG_PATH")
    if path:
        try:
            import json

            with open(os.path.expanduser(path)) as f:
                cfg = json.load(f)
            return frozenset(_pdu_rack_key(h) for h in cfg.get("ol", []))
        except (OSError, ValueError, TypeError):
            pass
    return PDU_OL_DIALECT_HOSTS


@dataclass
class Device42Config:
    """Device42 CMDB access — DOQL query endpoint + REST base, one Basic-auth header.

    ``endpoint`` is the DOQL query URL (``.../services/data/v1.0/query/``);
    ``rest_base`` is the scheme+host derived from it (the REST API lives at
    ``<rest_base>/api/1.0/``). The same ``auth`` header serves both. The lab
    Device42 uses a self-signed cert, so TLS verification is off by default.
    """

    endpoint: str
    rest_base: str
    auth: str
    verify_tls: bool = False

    @classmethod
    def resolve(
        cls,
        *,
        endpoint: Optional[str] = None,
        auth: Optional[str] = None,
    ) -> "Device42Config":
        _load_console_env()
        endpoint = (endpoint or os.environ.get("DEVICE42_ENDPOINT") or "").strip()
        auth = (auth or os.environ.get("DEVICE42_AUTH") or "").strip()
        missing = _missing([("DEVICE42_ENDPOINT", endpoint), ("DEVICE42_AUTH", auth)])
        if missing:
            raise CredentialError(
                f"Missing Device42 credentials in the environment: "
                f"{', '.join(missing)}. Export them (they live in ~/.console_env): "
                f"export DEVICE42_ENDPOINT="
                f"https://device42.example.net/services/data/v1.0/query/ "
                f"DEVICE42_AUTH='Basic <base64(user:pass)>'."
            )
        # Derive scheme://host from the DOQL endpoint for the REST base.
        from urllib.parse import urlsplit

        parts = urlsplit(endpoint)
        rest_base = f"{parts.scheme}://{parts.netloc}"
        return cls(endpoint=endpoint, rest_base=rest_base, auth=auth)
