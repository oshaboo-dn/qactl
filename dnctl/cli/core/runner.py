"""Standard "run one command on a device, return an envelope" wrapper.

This is the glue between the tool surface and ``dnctl.cli.core.session``: a
single SSH-channel command on a device, with the envelope shape
(`status` / `device` / `host` / `command` / `stdout` / `warnings` /
`errors` / `next_actions`) every simple tool returns. Tools that need
multi-step sequences or non-default response shapes use
``dnctl.cli.core.session.run_sequence`` / ``run_sequence_pw`` directly
instead.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from typing import List

from dnctl.cli.core.envelope import make_response
from dnctl.cli.core.errors import detect_error
from dnctl.cli.core.logging import log_invocation, log_request
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import (
    ConnectError,
    connect_error_next_actions,
    run_ncm_cli,
    run_once,
    run_probes,
    run_sequence,
)


def _run_on_device(
    tool: str,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    command: str,
    timeout: float,
    next_action_on_error: str,
    mode: str = "command",
    shell_entry: str = "run start shell",
) -> Dict[str, Any]:
    request = {"device": device, "host": host, "user": user, "command": command}
    response = make_response(device=device, host=host, command=command)

    try:
        result = run_once(
            transport_registry,
            device=device,
            host=host,
            user=user,
            password=password,
            command=command,
            timeout=timeout,
            mode=mode,
            shell_entry=shell_entry,
        )
    except ConnectError as exc:
        response.update(
            status="connect_error",
            state="unreachable",
            errors=[str(exc)],
            next_actions=connect_error_next_actions(exc),
        )
        log_request(tool, request, response)
        return response
    except Exception as exc:
        response.update(status="error", errors=[str(exc)])
        log_request(tool, request, response)
        return response

    response["host"] = result.host
    response["device"] = result.device or device
    response["stdout"] = result.output
    log_invocation(
        result.device or device,
        result.host,
        command,
        result.output,
        result.head_prompt_line,
        result.tail_prompt,
        steps=result.steps,
    )

    if not result.hit_prompt:
        response["status"] = "timeout"
        response["errors"].append(f"Timed out waiting for CLI prompt after {timeout}s.")
        response["next_actions"].append("Retry with a narrower command or a larger timeout.")
        log_request(tool, request, response)
        return response

    # Detect device-rejected-the-command using the device's vendor
    # patterns (DNOS for unknown / host-only — identical to the legacy
    # ``detect_error``).
    from dnctl.cli.vendors.registry import plugin_for_device
    is_err, err_lines = plugin_for_device(device, host).detect_error(result.output)
    if is_err:
        response["status"] = "error"
        response["errors"].extend(err_lines[-5:])
        response["next_actions"].append(next_action_on_error)

    log_request(tool, request, response)
    return response


def _run_raw_on_device(
    tool: str,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    lines: List[str],
    timeout: float,
    next_action_on_error: str,
    stop_on_error: bool = True,
    prompt_timeout: Optional[float] = None,
    banner_wait: Optional[float] = None,
    answer_confirm: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a raw line sequence on one channel; return the full transcript.

    The escape hatch behind ``cli raw``. Unlike :func:`_run_on_device`
    (single command) this drives every line in ``lines`` on the SAME
    ephemeral channel via :func:`run_sequence` and returns the per-step
    transcript so the caller can see exactly what each line produced. By
    default the sequence aborts on the first line that DNOS flags as an
    error (``stop_on_error``); pass ``stop_on_error=False`` to keep going.
    ``prompt_timeout`` / ``banner_wait`` widen the fresh-channel
    prompt-detection budget for a slow/odd box (e.g. DNAAS-LEAF-B13).
    ``answer_confirm`` (e.g. ``"yes"``) auto-answers interactive
    ``(yes/no)?`` / ``[y/n]?`` confirms a line raises mid-execution —
    required for ``request system target-stack load`` and friends, whose
    confirm otherwise wedges the line until the timeout.
    """
    joined = " ; ".join(lines)
    request = {"device": device, "host": host, "user": user, "command": joined}
    response = make_response(device=device, host=host, command=joined)

    stop_predicate = None
    if stop_on_error:
        stop_predicate = lambda step: detect_error(step.output)[0]  # noqa: E731

    try:
        result = run_sequence(
            transport_registry,
            device=device,
            host=host,
            user=user,
            password=password,
            commands=lines,
            timeout=timeout,
            stop_predicate=stop_predicate,
            auto_confirm=answer_confirm is not None,
            confirm_answer=answer_confirm or "yes",
            prompt_timeout=prompt_timeout,
            banner_wait=banner_wait,
        )
    except ConnectError as exc:
        response.update(
            status="connect_error",
            state="unreachable",
            errors=[str(exc)],
            next_actions=connect_error_next_actions(exc),
        )
        log_request(tool, request, response)
        return response
    except Exception as exc:
        response.update(status="error", errors=[str(exc)])
        log_request(tool, request, response)
        return response

    response["host"] = result.host
    response["device"] = result.device or device

    # Full per-step transcript: agents read `stdout` (human transcript) and
    # may machine-read `steps` (structured per-line outcome).
    steps_out = []
    transcript_blocks = []
    for s in result.steps:
        steps_out.append(
            {"command": s.command, "stdout": s.output, "hit_prompt": s.hit_prompt}
        )
        block = s.command if not s.output else f"{s.command}\n{s.output.rstrip()}"
        transcript_blocks.append(block)
    response["stdout"] = "\n\n".join(transcript_blocks)
    response["steps"] = steps_out

    log_invocation(
        result.device or device,
        result.host,
        joined,
        result.output,
        result.head_prompt_line,
        result.tail_prompt,
        steps=result.steps,
    )

    if not result.hit_prompt:
        response["status"] = "timeout"
        response["errors"].append(
            f"Timed out waiting for the CLI prompt after {timeout}s "
            f"(line {len(result.steps)} of {len(lines)})."
        )
        # A line stuck at an interactive (yes/no)? confirm never paints the
        # prompt — point at --answer-confirm instead of a bigger timeout.
        from dnctl.cli.core.shell import _CONFIRM_RE
        last_out = result.steps[-1].output if result.steps else ""
        if answer_confirm is None and _CONFIRM_RE.search(last_out.rstrip()[-256:]):
            response["next_actions"].append(
                "The line is waiting at an interactive (yes/no) confirm; "
                "re-run with --answer-confirm yes (or no) to auto-answer it."
            )
        else:
            response["next_actions"].append(
                "Retry with a larger --timeout, or --prompt-timeout if the "
                "prompt itself is slow to appear."
            )
        log_request(tool, request, response)
        return response

    # Surface an error on any step (not just the last), so a mid-sequence
    # failure isn't masked by a clean trailing line.
    err_lines: List[str] = []
    for s in result.steps:
        is_err, lines_err = detect_error(s.output)
        if is_err:
            err_lines.extend(lines_err[-5:])
    if err_lines:
        response["status"] = "error"
        response["errors"].extend(err_lines[-5:])
        response["next_actions"].append(next_action_on_error)

    log_request(tool, request, response)
    return response


def _run_probe_on_device(
    tool: str,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    probes: List[tuple],
    timeout: float,
    next_action_on_error: str,
    config_mode: bool = False,
    prompt_timeout: Optional[float] = None,
    banner_wait: Optional[float] = None,
) -> Dict[str, Any]:
    """Run keystroke probes on one channel; return the per-probe transcript.

    The glue behind ``cli probe``. Mirrors :func:`_run_raw_on_device` but
    drives :func:`run_probes` — each ``(prefix, key)`` pair is typed
    without a newline, one keystroke is injected, and the line is wiped
    with Ctrl-U before the next probe, so nothing is ever submitted. The
    per-probe ``steps`` carry the harvested ``stdout`` plus the
    post-keystroke ``line_buffer`` (the completed line for a TAB probe).
    """
    joined = " ; ".join(
        f"{p}<TAB>" if key == "tab" else f"{p}?" for p, key in probes
    )
    request = {"device": device, "host": host, "user": user, "command": joined}
    response = make_response(device=device, host=host, command=joined)

    try:
        result = run_probes(
            transport_registry,
            device=device,
            host=host,
            user=user,
            password=password,
            probes=probes,
            config_mode=config_mode,
            timeout=timeout,
            prompt_timeout=prompt_timeout,
            banner_wait=banner_wait,
        )
    except ConnectError as exc:
        response.update(
            status="connect_error",
            state="unreachable",
            errors=[str(exc)],
            next_actions=connect_error_next_actions(exc),
        )
        log_request(tool, request, response)
        return response
    except Exception as exc:
        response.update(status="error", errors=[str(exc)])
        log_request(tool, request, response)
        return response

    response["host"] = result.host
    response["device"] = result.device or device

    # In config mode the session records the ``configure`` entry as the
    # first step (so a box where it fails — e.g. GI mode — is caught by the
    # error scan below); the probe steps follow it.
    setup_steps = result.steps[:1] if config_mode else []
    probe_steps = result.steps[1:] if config_mode else result.steps

    steps_out = []
    transcript_blocks = [
        s.command if not s.output else f"{s.command}\n{s.output.rstrip()}"
        for s in setup_steps
    ]
    for (prefix, key), s in zip(probes, probe_steps):
        steps_out.append(
            {
                "prefix": prefix,
                "key": key,
                "stdout": s.output,
                "line_buffer": s.line_buffer,
                "hit_prompt": s.hit_prompt,
            }
        )
        block = s.command if not s.output else f"{s.command}\n{s.output.rstrip()}"
        if key == "tab" and s.line_buffer:
            block += f"\n[buffer] {s.line_buffer}"
        transcript_blocks.append(block)
    response["stdout"] = "\n\n".join(transcript_blocks)
    response["steps"] = steps_out

    log_invocation(
        result.device or device,
        result.host,
        joined,
        result.output,
        result.head_prompt_line,
        result.tail_prompt,
        steps=result.steps,
    )

    if not result.hit_prompt:
        response["status"] = "timeout"
        detail = (
            f"probe {len(probe_steps)} of {len(probes)}"
            if probe_steps
            else "entering configure mode"
        )
        response["errors"].append(
            f"Timed out waiting for the CLI prompt after {timeout}s ({detail})."
        )
        response["next_actions"].append(
            "Retry with a larger --timeout, or --prompt-timeout if the prompt "
            "itself is slow to appear."
        )
        log_request(tool, request, response)
        return response

    # Surface an error on any probe (an invalid prefix makes DNOS print an
    # error block instead of help/completion).
    err_lines: List[str] = []
    for s in result.steps:
        is_err, lines_err = detect_error(s.output)
        if is_err:
            err_lines.extend(lines_err[-5:])
    if err_lines:
        response["status"] = "error"
        response["errors"].extend(err_lines[-5:])
        response["next_actions"].append(next_action_on_error)

    log_request(tool, request, response)
    return response


def _run_ncm_on_device(
    tool: str,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    ncm_commands: List[str],
    shell_entry: str,
    timeout: float,
    next_action_on_error: str,
    answer: str = "y",
) -> Dict[str, Any]:
    """Drive the NCM nested CLI on a device and build the standard envelope.

    Mirrors :func:`_run_on_device` but runs a *sequence* of NCM (ICOS-style)
    CLI commands inside ``shell_entry`` (``run start shell ncm <id>``) and
    returns their combined transcript in ``stdout``. ``command`` carries the
    joined NCM command line for the transcript log. ``answer`` is the reply
    sent to any interactive ``[y/n]:`` confirm a command raises.
    """
    joined = " ; ".join(ncm_commands)
    request = {
        "device": device, "host": host, "user": user,
        "command": joined, "shell_entry": shell_entry,
    }
    response = make_response(device=device, host=host, command=joined)

    try:
        result = run_ncm_cli(
            transport_registry,
            device=device,
            host=host,
            user=user,
            password=password,
            ncm_commands=ncm_commands,
            shell_entry=shell_entry,
            timeout=timeout,
            answer=answer,
        )
    except ConnectError as exc:
        response.update(
            status="connect_error",
            state="unreachable",
            errors=[str(exc)],
            next_actions=connect_error_next_actions(exc),
        )
        log_request(tool, request, response)
        return response
    except Exception as exc:
        response.update(status="error", errors=[str(exc)])
        log_request(tool, request, response)
        return response

    response["host"] = result.host
    response["device"] = result.device or device
    response["stdout"] = result.output
    log_invocation(
        result.device or device,
        result.host,
        joined,
        result.output,
        result.head_prompt_line,
        result.tail_prompt,
        steps=result.steps,
    )

    if not result.hit_prompt:
        response["status"] = "timeout"
        response["errors"].append(
            f"Timed out waiting for the NCM CLI prompt after {timeout}s."
        )
        response["next_actions"].append(next_action_on_error)
        log_request(tool, request, response)
        return response

    is_err, err_lines = detect_error(result.output)
    if is_err:
        response["status"] = "error"
        response["errors"].extend(err_lines[-5:])
        response["next_actions"].append(next_action_on_error)

    log_request(tool, request, response)
    return response
