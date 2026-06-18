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
