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

from dnctl.cli.core.envelope import make_response
from dnctl.cli.core.errors import detect_error
from dnctl.cli.core.logging import log_invocation, log_request
from dnctl.cli.core.registry import transport_registry
from dnctl.cli.core.session import ConnectError, run_once


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
            errors=[str(exc)],
            next_actions=["Verify device is reachable and credentials are correct."],
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

    is_err, err_lines = detect_error(result.output)
    if is_err:
        response["status"] = "error"
        response["errors"].extend(err_lines[-5:])
        response["next_actions"].append(next_action_on_error)

    log_request(tool, request, response)
    return response
