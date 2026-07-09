"""``run_probe`` tool — keystroke probes (TAB / ``?``) without submitting.

QA test plans include interactive CLI-discoverability coverage: press TAB
(completion) or ``?`` (context help) on a command prefix and assert the
offered leaves/hints match ``cmd help``. ``cli raw`` can't do it — every raw
argument is a *line*, newline-terminated, and pressing Enter would actually
submit the statement. This tool types the prefix WITHOUT Enter, injects a
single keystroke, harvests what the CLI paints, and clears the line with
Ctrl-U before the next probe — read-only by construction (no ``--yes``).

Trailing-space semantics matter and are preserved verbatim: probing
``"... bfd "`` enumerates the children of ``bfd``, while ``"... bfd str"``
acts on the partial token (``?`` filters the leaf list, TAB completes it to
``strict-mode``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

from qactl.dnos.cli.core.envelope import error_response
from qactl.dnos.cli.core.errors import RUN_PROBE_NEXT_ACTION
from qactl.dnos.cli.core.runner import _run_probe_on_device
from qactl.dnos.cli.core.session import DEFAULT_CMD_TIMEOUT, DEFAULT_PASSWORD, DEFAULT_USER
from qactl.dnos.cli.vendors import CAP_DISCOVERY, requires

# Accepted --key spellings -> canonical key. ``\t`` / ``tab`` for completion,
# ``?`` / ``help`` for context help.
_KEY_ALIASES = {
    "?": "?",
    "help": "?",
    "tab": "tab",
    "\t": "tab",
}


@requires(CAP_DISCOVERY)
def run_probe(
    prefixes: Union[str, List[str]],
    key: str = "?",
    config_mode: bool = False,
    device: Optional[str] = None,
    host: Optional[str] = None,
    prompt_timeout: Optional[float] = None,
    banner_wait: Optional[float] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """Inject a bare keystroke (TAB / ``?``) after each prefix, never Enter.

    Each entry in ``prefixes`` is one probe: the prefix is typed verbatim
    (whitespace preserved — keep a trailing space to enumerate children,
    omit it to act on the partial last token), ``key`` is injected, the
    painted output is harvested, and Ctrl-U wipes the line before the next
    probe. All probes share ONE ephemeral channel; nothing is ever
    submitted, so the tool is read-only even on destructive command trees.

    ``key="?"`` returns the context-help block DNOS prints;
    ``key="tab"`` returns the completed line buffer (per-step
    ``line_buffer``) plus any candidate list. ``config_mode=True`` pushes
    the channel into ``configure`` first (and leaves via ``end``) so the
    probes hit the configuration grammar.

    Args:
        prefixes: One prefix, or a list of prefixes probed in order.
        key: ``"?"`` (context help, default) or ``"tab"`` (completion).
        config_mode: Probe the configure-mode grammar instead of operational.
        device: Device alias from the registry.
        host: Raw hostname/IP (alternative to device).
        prompt_timeout: Seconds to coax a prompt out of a fresh channel.
        banner_wait: Per-drain settle window while detecting the prompt.
        user: SSH username (default dnroot).
        password: SSH password (default dnroot).
        timeout: Per-probe timeout seconds.

    Returns the standard envelope plus a ``steps`` list of
    ``{prefix, key, stdout, line_buffer, hit_prompt}``.
    """
    raw_key = key if key is not None else "?"
    # Look the raw spelling up first so a literal "\t" survives (strip()
    # would erase it), then fall back to the normalised form.
    canonical = _KEY_ALIASES.get(raw_key) or _KEY_ALIASES.get(
        raw_key.strip().lower()
    )
    if canonical is None:
        return error_response(
            f"Unknown --key {key!r}: use '?' (context help) or 'tab' (completion).",
            device=device, host=host, next_action=RUN_PROBE_NEXT_ACTION,
        )

    if isinstance(prefixes, str):
        prefixes = [prefixes]
    probes: List[Tuple[str, str]] = [
        (p, canonical) for p in (prefixes or []) if p is not None
    ]
    if not probes:
        return error_response(
            "Provide at least one prefix to probe (an empty string probes the root).",
            device=device, host=host, next_action=RUN_PROBE_NEXT_ACTION,
        )

    return _run_probe_on_device(
        "run_probe", device, host, user, password,
        probes, timeout, RUN_PROBE_NEXT_ACTION,
        config_mode=config_mode,
        prompt_timeout=prompt_timeout,
        banner_wait=banner_wait,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(run_probe)
