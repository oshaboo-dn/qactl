"""``run_raw`` tool — send raw CLI line(s) on one channel, return the transcript.

The escape hatch for device interactions the structured tools don't model:
an arbitrary sequence of DNOS CLI lines run verbatim, in order, on a single
ephemeral channel, with the full per-step transcript handed back. Reuses the
same SSH transport pool, prompt detection, and envelope as every other CLI
tool — the only thing it adds is "no opinion about what the lines mean".

Prefer the purpose-built ``show`` / ``show-config`` / ``config`` / ``shell``
tools when they fit; reach for ``run_raw`` when they don't (an odd nested
flow, a one-off multi-step interaction, or a box whose prompt is slow/odd
enough to need a wider detection budget than the default).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from dnctl.cli.core.envelope import error_response
from dnctl.cli.core.errors import RUN_RAW_NEXT_ACTION
from dnctl.cli.core.runner import _run_raw_on_device
from dnctl.cli.core.session import DEFAULT_CMD_TIMEOUT, DEFAULT_PASSWORD, DEFAULT_USER
from dnctl.cli.vendors import CAP_RAW, requires


@requires(CAP_RAW)
def run_raw(
    lines: Union[str, List[str]],
    device: Optional[str] = None,
    host: Optional[str] = None,
    stop_on_error: bool = True,
    answer_confirm: Optional[str] = None,
    prompt_timeout: Optional[float] = None,
    banner_wait: Optional[float] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Send raw CLI line(s) in order on one channel; return the full transcript.

    Each line in ``lines`` is sent verbatim (newline-terminated) on the SAME
    ephemeral channel, so multi-step state holds across lines within the call
    (e.g. ``configure`` then a ``set ...`` then ``commit``) but dies with the
    channel afterwards. The response ``stdout`` is the human transcript and
    ``steps`` is the structured per-line outcome (``command`` / ``stdout`` /
    ``hit_prompt``).

    By default the sequence aborts on the first line DNOS flags as an error
    (``stop_on_error``); pass ``stop_on_error=False`` to run every line
    regardless. ``answer_confirm`` (``"yes"`` / ``"no"``) auto-answers any
    interactive ``(yes/no)?`` / ``[y/n]?`` confirm a line raises — without
    it a confirming line (e.g. ``request system target-stack load``) never
    paints the prompt and the call times out; a follow-up ``yes`` line
    cannot answer it, because each line waits for the CLI prompt first.
    ``prompt_timeout`` / ``banner_wait`` widen the fresh-channel
    prompt-detection budget for a slow/odd box (e.g. DNAAS-LEAF-B13) — they
    override the ``DNCTL_CLI_PROMPT_TIMEOUT`` / ``DNCTL_CLI_BANNER_WAIT`` env
    knobs, which in turn override the built-in defaults.

    Args:
        lines: A single CLI line, or a list of lines run in order.
        device: Device alias from the registry.
        host: Raw hostname/IP (alternative to device).
        stop_on_error: Abort on the first errored line (default True).
        answer_confirm: Reply auto-sent to interactive (yes/no) confirms.
        prompt_timeout: Seconds to coax a prompt out of a fresh channel.
        banner_wait: Per-drain settle window while detecting the prompt.
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-line timeout seconds.

    Returns the standard envelope plus a ``steps`` list.
    """
    if isinstance(lines, str):
        lines = [lines]
    cmds = [c.strip() for c in (lines or []) if c and c.strip()]
    if not cmds:
        return error_response(
            "Provide at least one non-empty CLI line.",
            device=device, host=host, next_action=RUN_RAW_NEXT_ACTION,
        )

    return _run_raw_on_device(
        "run_raw", device, host, user, password,
        cmds, timeout, RUN_RAW_NEXT_ACTION,
        stop_on_error=stop_on_error,
        answer_confirm=answer_confirm,
        prompt_timeout=prompt_timeout,
        banner_wait=banner_wait,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(run_raw)
