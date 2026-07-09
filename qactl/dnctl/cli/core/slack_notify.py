"""Best-effort Slack notify via the dn-mcp-server slackbot.

Opt-in helper: long-running tools take a ``notify_slack=<channel>``
kwarg and call :func:`post` at kickoff (creates the thread) and at
terminal state (replies in the thread). An empty channel disables.

Failures are silent — they MUST NOT break the actual task. Errors
are returned in the result dict so the caller can stash them in
``job.warnings``.

The slackbot lives behind dn-mcp-server at
``http://ai-server:8000/mcp``. Auth is per-call via headers:

    X-Email-User:     oshaboo@drivenets.com
    X-SLACKBOT-TOOLS: enabled

Both can be overridden via env (``DN_MCP_URL``,
``SLACK_USER_EMAIL``).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.request
from typing import Any, Awaitable, Optional

# NOTE: the ``mcp`` SDK is an optional, lazily-imported dependency. Slack
# notification is a best-effort nicety for long-running jobs and dnctl is
# not an MCP client, so we do NOT want importing a tool module to hard-fail
# when ``mcp`` is absent. The import is deferred into :func:`_send`.


DN_MCP_URL = os.environ.get("DN_MCP_URL", "http://ai-server:8000/mcp")
SLACK_USER_EMAIL = os.environ.get("SLACK_USER_EMAIL", "oshaboo@drivenets.com")
# A Slack webhook (a classic incoming webhook ``https://hooks.slack.com/
# services/...`` OR a Workflow Builder trigger ``.../triggers/...`` whose
# workflow takes a ``text`` variable). When set, it is the preferred
# transport: a single self-contained credential bound to one channel, with no
# OAuth / MCP server / bot-in-channel dependency — the right fit for an
# unattended collector (``monitor watch``). The webhook posts to its own fixed
# channel, so the ``channel``/``@user`` arg is informational when a webhook is
# configured. Falls back to the MCP slackbot path when unset.
#
# We mirror the ``diva`` tool's slack adapter (same ``{"text": ...}`` body,
# any-2xx = success) and reuse its ``DIVA_SLACK_WEBHOOK_URL`` as a fallback so
# a single shared webhook serves both tools.
SLACK_WEBHOOK_URL = (
    os.environ.get("QACTL_SLACK_WEBHOOK_URL")
    or os.environ.get("DIVA_SLACK_WEBHOOK_URL")
    or ""
)
DEFAULT_TIMEOUT_S = 10.0


def post(
    channel: str,
    text: str,
    *,
    thread_ts: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Send one Slack message. Always returns; never raises.

    Args:
        channel: Slack channel name, ID, or ``@user``. Empty disables.
        text: message text (Slack mrkdwn ok).
        thread_ts: parent thread ts to reply in. ``None`` posts at
            top level and the returned ``ts`` can be used as the
            thread_ts for follow-ups.
        timeout_s: wall-clock cap for the HTTP roundtrip. Slack is
            usually <1 s; we cap at 10 s so a flaky network can't
            stall a worker thread for minutes.

    Returns:
        ``{"ok": bool, "ts": str|None, "error": str|None}``.
        On ``ok=False`` the caller typically appends ``error`` to
        ``job.warnings``.
    """
    if SLACK_WEBHOOK_URL:
        return _post_webhook(SLACK_WEBHOOK_URL, text, timeout_s)
    if not channel:
        return {"ok": False, "ts": None, "error": "no channel set"}
    try:
        return _run_blocking(_send(channel, text, thread_ts), timeout_s)
    except asyncio.TimeoutError:
        return {
            "ok": False, "ts": None,
            "error": f"slack notify timed out after {timeout_s}s",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "ts": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _post_webhook(url: str, text: str, timeout_s: float) -> dict[str, Any]:
    """Post ``text`` to a Slack webhook. Always returns; never raises.

    Sends ``{"text": ...}`` and treats **any HTTP 2xx as success** — matching
    the ``diva`` slack adapter, so the same URL works whether it's a classic
    incoming webhook (``.../services/...``, body ``ok``) or a Workflow Builder
    trigger (``.../triggers/...``, JSON ack body). No auth header, no MCP, no
    ``ts`` (webhooks don't return a message timestamp).
    """
    try:
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            payload = resp.read().decode("utf-8", "replace").strip()
        if 200 <= status < 300:
            return {"ok": True, "ts": None, "error": None}
        return {
            "ok": False, "ts": None,
            "error": f"slack webhook returned {status}: {payload[:200]}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "ts": None, "error": f"{type(exc).__name__}: {exc}"}


def _run_blocking(coro: Awaitable[Any], timeout_s: float) -> Any:
    """Drive an async coroutine to completion from sync code, regardless
    of whether the calling thread already has a running event loop.

    Why this exists: FastMCP runs sync ``@mcp.tool()`` functions
    *directly* inside its async dispatcher (see
    ``mcp/server/fastmcp/utilities/func_metadata.py``
    ``call_fn_with_arg_validation`` — the sync branch is plain
    ``return fn(**args)``). That means a tool like
    ``request_system_tar_load`` runs on the server's event-loop thread
    *with the loop running*, and a naive ``asyncio.run(...)`` inside
    the tool blows up with::

        RuntimeError: asyncio.run() cannot be called from a running
        event loop

    Background workers (``_tar_load_worker``, ``_techsupport_worker``)
    live on plain ``threading.Thread`` instances with no loop, so they
    don't hit this — but we still route them through here so
    terminal-state notifies are robust by construction.

    Strategy:
        * No running loop on this thread → ``asyncio.run`` directly.
        * Running loop on this thread → spin up a short-lived worker
          thread with its own fresh loop (``asyncio.run`` inside) and
          join it. We can't use ``run_coroutine_threadsafe`` against
          the caller's loop because the caller's tool code is itself
          *blocking* that loop, so the future would never complete
          (deadlock).
    """
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return asyncio.run(asyncio.wait_for(coro, timeout=timeout_s))
    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["v"] = asyncio.run(asyncio.wait_for(coro, timeout=timeout_s))
        except BaseException as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=_runner, name="slack-notify", daemon=True)
    t.start()
    # Inner ``asyncio.wait_for`` should fire first; the +5 s is just a
    # belt-and-suspenders cap so a wedged worker can't pin the caller.
    t.join(timeout=timeout_s + 5.0)
    if t.is_alive():
        raise asyncio.TimeoutError()
    if "e" in box:
        raise box["e"]
    return box["v"]


def _route(channel: str, text: str, thread_ts: Optional[str]) -> tuple[str, dict[str, Any]]:
    """Pick the slackbot tool + args for a destination.

    A leading ``@`` means "DM this user" — that needs the user-resolving
    tool ``slackbot_slack_send_msg_to_user``. ``slackbot_slack_send_msg``
    only takes a real channel name/ID and would silently target a
    non-existent "@name" channel.
    """
    target = channel.strip()
    if target.startswith("@"):
        tool = "slackbot_slack_send_msg_to_user"
        args: dict[str, Any] = {
            "username_or_display_name": target[1:],
            "message_content": text,
        }
    else:
        tool = "slackbot_slack_send_msg"
        args = {"channel": target, "message_content": text}
    if thread_ts:
        args["thread_ts"] = thread_ts
    return tool, args


async def _send(
    channel: str, text: str, thread_ts: Optional[str],
) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {
        "X-Email-User": SLACK_USER_EMAIL,
        "X-SLACKBOT-TOOLS": "enabled",
    }
    tool, args = _route(channel, text, thread_ts)
    async with streamablehttp_client(DN_MCP_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            result = await s.call_tool(tool, args)
    return _extract_ok(result)


def _extract_ok(result: Any) -> dict[str, Any]:
    """Decide success and pull ``ts`` out of the slackbot result envelope.

    The slackbot returns a JSON payload like
    ``{"success": true, "details": {"timestamp": "..."}}`` on success and
    ``{"success": false, "error": "user_not_found", ...}`` on failure.
    Different MCP SDK versions surface it in either ``structuredContent``
    or ``content[].text``, so we try both — and we HONOR an explicit
    failure rather than reporting every non-raising call as delivered.
    """
    payload: Optional[dict[str, Any]] = None
    raw_text: Optional[str] = None

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        inner = structured.get("result", structured)
        if isinstance(inner, dict):
            payload = inner
        elif isinstance(inner, str):
            raw_text = inner

    if payload is None:
        if raw_text is None:
            for c in (getattr(result, "content", None) or []):
                t = getattr(c, "text", None)
                if isinstance(t, str):
                    raw_text = t
                    break
        if isinstance(raw_text, str):
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = None

    # No parseable payload — can't prove failure, so don't cry wolf; but we
    # also can't prove a ts. Treat as best-effort success (legacy behavior).
    if not isinstance(payload, dict):
        return {"ok": True, "ts": None, "error": None}

    # Explicit failure flags from the slackbot.
    if payload.get("success") is False or payload.get("ok") is False:
        err = (
            payload.get("error")
            or payload.get("message")
            or "slack send failed"
        )
        return {"ok": False, "ts": None, "error": str(err)}
    if isinstance(payload.get("error"), str) and payload["error"]:
        return {"ok": False, "ts": None, "error": payload["error"]}

    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    ts = (
        details.get("timestamp")
        or details.get("ts")
        or payload.get("ts")
        or payload.get("message_ts")
        or msg.get("ts")
    )
    return {"ok": True, "ts": ts if isinstance(ts, str) else None, "error": None}
