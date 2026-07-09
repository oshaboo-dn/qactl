"""Shared driver for the ``configure → <body> → commit`` command shape.

Used by ``load_override_factory_default``, ``rollback_config``,
``edit_config``, and ``restore_device``. Owns the full boilerplate: step
assembly, SSH run, connect/error envelope mapping, output scrubbing,
``log_invocation`` + ``log_request`` calls on every exit path.

Callers keep only what diverges between tools: argument validation,
per-device lock, dry-run / confirm gates (assembled into the step list
before calling us), ``parse_commit_output`` tails, and the tool-specific
``no_change`` / ``check_ok`` wording.

Contract: :func:`drive_configure_commit` returns the raw
:class:`Invocation` on success (caller can inspect it further), or
``None`` on failure (response has already been updated and logged).
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from qactl.dnctl.cli.core.logging import log_invocation, log_request
from qactl.dnctl.cli.core.session import (
    ConnectError,
    UnknownDeviceError,
    connect_error_next_actions,
    Invocation,
    StepCapture,
    TransportRegistry,
    run_sequence_pw,
)

Step = Tuple[str, Optional[str]]

_DEFAULT_CONNECT_NEXT_ACTION = (
    "Verify device is reachable and credentials are correct."
)


def build_configure_commit_steps(
    *,
    pre_commands: Sequence[Step] = (),
    body_statements: Sequence[str] = (),
    commit_line: str = "commit",
    trailing_commands: Sequence[Step] = (),
) -> Tuple[List[Step], str]:
    """Assemble ``pre → configure → body → commit → trailing`` and
    ``" ; "``-join it for ``response["command"]``.
    """
    steps: List[Step] = list(pre_commands)
    steps.append(("configure", None))
    steps.extend((s, None) for s in body_statements)
    steps.append((commit_line, None))
    steps.extend(trailing_commands)
    return steps, " ; ".join(cmd for cmd, _ in steps)


def drive_configure_commit(
    registry: TransportRegistry,
    *,
    tool_name: str,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    timeout: float,
    steps: Sequence[Step],
    command: str,
    request: dict,
    response: dict,
    capture_all: bool = False,
    scrub_secret: Optional[str] = None,
    connect_next_action: str = _DEFAULT_CONNECT_NEXT_ACTION,
    commit_conflict_answer: Optional[str] = "abort",
    stop_predicate: Optional[Callable[[StepCapture], bool]] = None,
) -> Optional[Invocation]:
    """Run ``steps`` on one ephemeral channel and fold the result into
    ``response``.

    On failure (connect error, unexpected exception): updates ``response``
    with status/errors/next_actions, calls :func:`log_request`, returns
    ``None``.

    On success: scrubs the captured output if ``scrub_secret`` is set,
    writes ``host`` / ``device`` / ``stdout`` onto ``response``, calls
    :func:`log_invocation`, and returns the :class:`Invocation`. The
    caller still decides whether to call ``log_request`` (so a tool can
    parse commit output first and add ``commit`` / warnings before the
    request gets logged).

    ``commit_conflict_answer`` (default ``"abort"``) is forwarded to
    :func:`run_sequence_pw` so a live ``commit`` interrupted by DNOS'
    rebase prompt (another session committed first) is answered instead of
    hanging until timeout. ``abort`` declines the merge — nothing is
    applied and :func:`parse_commit_output` reports ``commit_conflict`` so
    the caller can advise a re-run. The dry-run (``commit check``) path
    already appends ``no-warning`` and never reaches the prompt, so the
    answer is harmless there.

    ``stop_predicate`` is forwarded to :func:`run_sequence_pw`: when it
    returns truthy for a just-completed step the remaining steps —
    including the commit line — are NOT sent. The caller can tell the
    sequence was cut short because the commit step is absent from
    ``Invocation.steps``.
    """
    try:
        inv = run_sequence_pw(
            registry,
            device=device, host=host, user=user, password=password,
            commands=list(steps), timeout=timeout,
            capture_all=capture_all,
            commit_conflict_answer=commit_conflict_answer,
            stop_predicate=stop_predicate,
        )
    except ConnectError as exc:
        response.update(
            status="connect_error", errors=[str(exc)],
            next_actions=(
                connect_error_next_actions(exc)
                if isinstance(exc, UnknownDeviceError)
                else [connect_next_action]
            ),
        )
        log_request(tool_name, request, response)
        return None
    except Exception as exc:
        response.update(status="error", errors=[str(exc)])
        log_request(tool_name, request, response)
        return None

    if scrub_secret:
        inv.output = inv.output.replace(scrub_secret, "***")
        # Also scrub each per-step capture so the transcript doesn't leak
        # the SFTP password when it appears mid-sequence (e.g. the DNOS
        # password prompt echo inside ``request file download ...``).
        inv.steps = [
            StepCapture(
                s.command, s.head_prompt_line,
                s.output.replace(scrub_secret, "***"),
                s.tail_prompt, s.hit_prompt,
            )
            for s in inv.steps
        ]

    response["host"] = inv.host
    response["device"] = inv.device or device
    response["stdout"] = inv.output
    log_invocation(
        inv.device or device, inv.host,
        command, inv.output,
        inv.head_prompt_line, inv.tail_prompt,
        steps=inv.steps,
    )
    return inv


__all__ = [
    "Step",
    "build_configure_commit_steps",
    "drive_configure_commit",
]
