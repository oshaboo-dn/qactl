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
        user = (user or os.environ.get("CONSOLE_CS_USER") or
                CONSOLE_SERVER_DEFAULT_USER).strip()
        password = (password if password is not None
                    else os.environ.get("CONSOLE_CS_PASSWORD", ""))
        return cls(user=user, password=password)


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
