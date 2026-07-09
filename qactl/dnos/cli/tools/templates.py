"""Jinja-template config tools (local-first).

After the MCP→CLI migration the agent shares a shell and a filesystem with
this tool, so template *authoring* is just editing ``.j2`` files under
``jinja/templates/`` directly — no create/delete/rename-as-a-tool needed.
What remains are the parts that still earn their keep:

- ``template_list`` / ``template_get`` — read-only metadata + body.
- ``render_config`` — render a template (saved or inline) with vars or a
  Python generator into a local ``.cli`` file / stdout. Touches no device.
- ``scale_deploy`` — push a rendered ``.cli`` file (or stdin) to a device,
  staged into one DNOS candidate and committed once.

The deploy path goes through :func:`_deploy_rendered_statements`, which
funnels into :func:`qactl.cli.core.configure_commit.drive_configure_commit`
exactly the way :func:`qactl.cli.tools.edit.edit_config` does. So semantics
(commit-and-exit vs commit-check, candidate cleanup on failure, the shared
per-device lock) are identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from qactl.dnos.cli.core import jinja_store
from qactl.dnos.cli.core.commit_sequence import parse_commit_output
from qactl.dnos.cli.core.configure_commit import drive_configure_commit
from qactl.dnos.cli.core.edit_helpers import (
    abort_shared_candidate,
    batch_abort_errors,
    build_edit_config_commands,
    commit_was_attempted,
    stop_on_rejected_statement,
    validate_edit_log,
    validate_edit_statements,
)
from qactl.dnos.cli.core.envelope import error_response, make_response
from qactl.dnos.cli.core.errors import (
    RENDER_NEXT_ACTION,
    SCALE_DEPLOY_NEXT_ACTION,
    TEMPLATE_NEXT_ACTION,
    detect_error,
)
from qactl.dnos.cli.core.locks import device_lock
from qactl.dnos.cli.core.logging import log_request
from qactl.dnos.cli.core.registry import transport_registry
from qactl.dnos.cli.core.session import DEFAULT_PASSWORD, DEFAULT_USER
from qactl.dnos.cli.vendors import CAP_CONFIGURE, requires


# Above this statement count, the joined "configure ; ... ; commit" string
# is replaced by a short summary in the envelope / request log.
_COMMAND_SUMMARY_THRESHOLD = jinja_store.MAX_STATEMENT_COUNT


def template_list() -> Dict[str, Any]:
    """List saved Jinja templates, newest-first (by mtime, UTC).

    Returns an envelope with ``templates: [...]``, each item having
    ``name`` / ``path`` / ``size`` / ``mtime_utc`` / ``declared_variables``.
    Pure local FS read — no device contact. Templates live under
    ``templates_dir``; you can also just edit those ``.j2`` files directly.
    """
    try:
        metas = jinja_store.list_templates()
    except OSError as exc:
        return error_response(
            f"filesystem error listing templates: {exc}",
            next_action=TEMPLATE_NEXT_ACTION,
        )
    response = make_response(
        command="template_list",
        templates=[m.to_dict() for m in metas],
        count=len(metas),
        templates_dir=str(jinja_store.TEMPLATES_DIR),
    )
    log_request("template_list", {}, response)
    return response


def template_get(name: str) -> Dict[str, Any]:
    """Return a saved template's raw Jinja body + metadata.

    Envelope extras: ``content`` (raw ``.j2`` body), ``name``, ``path``,
    ``size``, ``mtime_utc``, ``declared_variables``.
    """
    try:
        body, meta = jinja_store.read_template(name)
    except jinja_store.TemplateNotFoundError as exc:
        return error_response(str(exc), next_action=TEMPLATE_NEXT_ACTION)
    except jinja_store.TemplateValidationError as exc:
        return error_response(str(exc), next_action=TEMPLATE_NEXT_ACTION)
    except OSError as exc:
        return error_response(
            f"filesystem error reading template {name!r}: {exc}",
            next_action=TEMPLATE_NEXT_ACTION,
        )
    response = make_response(
        command=f"template_get name={name}",
        content=body,
        **meta.to_dict(),
    )
    log_request("template_get", {"name": name}, response)
    return response


def render_config(
    name: Optional[str] = None,
    content: Optional[str] = None,
    vars_yaml: Optional[str] = None,
    python_script: Optional[str] = None,
    out_file: Optional[str] = None,
    exec_timeout: float = 30.0,
) -> Dict[str, Any]:
    """Render a template into a local config (file / stdout). No device.

    This is the "build the config" half of the local scale flow. Pass
    exactly one template source and at most one vars source:

    - Template source: ``name`` (a saved template) XOR ``content`` (an
      inline Jinja2 body).
    - Vars source: ``vars_yaml`` (a YAML mapping) XOR ``python_script``
      (a Python generator that prints a YAML mapping to stdout — for when
      hand-writing vars for thousands of objects is impractical).
      ``python_script`` requires a saved ``name`` so the run is captured
      under an audit dir (``jinja/scale/<name>/<ts>/``).

    With neither vars source it's a preflight: returns the template's
    ``declared_variables`` and writes nothing.

    Rendering uses ``jinja2.StrictUndefined`` (missing keys are hard
    errors) and the scale per-call ceiling (``MAX_SCALE_STATEMENT_COUNT``),
    so thousands of statements are fine. The rendered statements are placed
    on the envelope ``stdout`` (so plain text output pipes straight into
    ``scale_deploy``), written to ``out_file`` when given, and — when a
    ``python_script`` ran — also to ``rendered.cli`` under the audit dir.

    Args:
        name: Saved template name (``[A-Za-z0-9._-]{1,60}``).
        content: Inline Jinja2 body (exclusive with ``name``).
        vars_yaml: YAML document string (a top-level mapping).
        python_script: Python source that prints a YAML mapping to stdout
            (≤512 KiB). Requires ``name``.
        out_file: Optional path to also write the rendered config to.
        exec_timeout: Wall-clock budget for the generator subprocess
            (seconds). Clamped to ``[5, 300]``.
    """
    if (name is None) == (content is None):
        return error_response(
            "provide exactly one of name (saved template) or content (inline).",
            next_action=RENDER_NEXT_ACTION,
        )
    if vars_yaml is not None and python_script is not None:
        return error_response(
            "provide at most one of vars_yaml or python_script, not both.",
            next_action=RENDER_NEXT_ACTION,
        )

    artifacts = None
    if python_script is not None:
        if not name:
            return error_response(
                "python_script requires a saved template name (the run is "
                "captured under jinja/scale/<name>/<ts>/).",
                next_action=RENDER_NEXT_ACTION,
            )
        try:
            vars_dict, artifacts = jinja_store.run_scale_script(
                template_name=name,
                script=python_script,
                exec_timeout=exec_timeout,
            )
        except jinja_store.ScaleExecError as exc:
            return error_response(str(exc), next_action=RENDER_NEXT_ACTION)
        except jinja_store.TemplateValidationError as exc:
            return error_response(str(exc), next_action=RENDER_NEXT_ACTION)
        except OSError as exc:
            return error_response(
                f"filesystem error preparing scale run for {name!r}: {exc}",
                next_action=RENDER_NEXT_ACTION,
            )
        try:
            import yaml as _yaml
            vars_yaml = _yaml.safe_dump(vars_dict, default_flow_style=False)
        except Exception as exc:  # pragma: no cover
            return error_response(
                f"could not re-serialise captured vars to YAML: {exc}",
                next_action=RENDER_NEXT_ACTION,
            )

    try:
        result = jinja_store.render(
            name=name, content=content, vars_yaml=vars_yaml,
            max_statements=jinja_store.MAX_SCALE_STATEMENT_COUNT,
        )
    except jinja_store.TemplateNotFoundError as exc:
        return error_response(str(exc), next_action=RENDER_NEXT_ACTION)
    except jinja_store.TemplateValidationError as exc:
        return error_response(str(exc), next_action=RENDER_NEXT_ACTION)
    except jinja_store.RenderError as exc:
        return error_response(str(exc), next_action=RENDER_NEXT_ACTION)

    command = f"render name={name}" if name else "render content=<inline>"

    # Preflight (no vars): just report declared variables; write nothing.
    if vars_yaml is None:
        response = make_response(
            command=command,
            name=result.name,
            declared_variables=result.declared_variables,
            rendered=False,
            statement_count=0,
        )
        log_request("render_config", {"name": name, "preflight": True}, response)
        return response

    if not result.statements:
        return error_response(
            "rendered 0 statements — vars produced an empty template "
            "(empty loop / all conditionals false). Nothing to write.",
            next_action=RENDER_NEXT_ACTION,
        )

    warnings = list(result.warnings)
    rendered_path: Optional[str] = None
    if artifacts is not None:
        try:
            jinja_store.write_rendered_cli(
                artifacts.rendered_path, result.statements,
            )
            rendered_path = artifacts.rendered_path
        except OSError as exc:
            warnings.append(f"could not write rendered.cli under audit dir: {exc}")

    out_written: Optional[str] = None
    if out_file:
        try:
            jinja_store.write_rendered_cli(out_file, result.statements)
            out_written = out_file
        except OSError as exc:
            warnings.append(f"could not write out_file {out_file!r}: {exc}")

    target = out_written or rendered_path
    if target:
        next_action = (
            "Inspect the rendered file, then push it: "
            f"qactl cli scale-deploy {target} -d <device> --yes"
        )
    else:
        next_action = (
            "No file written (pass --out to save). Pipe instead: "
            "qactl cli render ... | qactl cli scale-deploy - -d <device> --yes"
        )

    scale_meta: Dict[str, Any] = {
        "out_file": out_written,
        "rendered_path": rendered_path,
    }
    if artifacts is not None:
        scale_meta.update(
            run_dir=artifacts.run_dir,
            script_path=artifacts.script_path,
            vars_path=artifacts.vars_path,
            exec_stderr_tail=artifacts.exec_stderr_tail,
            exec_stdout_truncated=artifacts.exec_stdout_truncated,
            exec_stderr_truncated=artifacts.exec_stderr_truncated,
        )

    # The rendered config goes on ``stdout`` so plain-text output pipes
    # straight into ``scale_deploy -``; --json still carries it losslessly.
    response = make_response(
        command=command,
        stdout="\n".join(result.statements) + "\n",
        warnings=warnings,
        name=result.name,
        declared_variables=result.declared_variables,
        rendered=True,
        statement_count=len(result.statements),
        preview=result.statements[:30],
        scale=scale_meta,
        next_actions=[next_action],
    )
    log_request(
        "render_config",
        {
            "name": name,
            "out_file": out_file,
            "used_script": python_script is not None,
            "statement_count": len(result.statements),
            "run_dir": artifacts.run_dir if artifacts else None,
        },
        response,
    )
    return response


def _deploy_rendered_statements(
    *,
    tool_name: str,
    statements: List[str],
    template_name: str,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    timeout: float,
    log: Optional[str],
    deploy: bool,
    abort_on_failure: bool,
    max_statements: int = jinja_store.MAX_STATEMENT_COUNT,
    extra_response: Optional[Dict[str, Any]] = None,
    request_extra: Optional[Dict[str, Any]] = None,
    next_action: str = SCALE_DEPLOY_NEXT_ACTION,
) -> Dict[str, Any]:
    """Shared deploy-path for the rendered-statements push.

    Mirrors ``edit_config``'s flow (configure → statements → commit
    and-exit / commit check + rollback 0) verbatim. ``template_name`` /
    any ``extra_response`` keys land on the envelope so the caller can
    reconcile the push with the originating source. The whole list is
    staged into a single DNOS candidate and committed once (all-or-nothing).
    """
    err = validate_edit_statements(statements, max_statements)
    if err:
        return error_response(
            err, device=device, host=host, next_action=next_action,
        )
    log_norm, err = validate_edit_log(log)
    if err:
        return error_response(
            err, device=device, host=host, next_action=next_action,
        )

    steps, commit_line, full_command = build_edit_config_commands(
        list(statements), log_norm, deploy,
    )

    # A scale push can carry tens of thousands of statements; the joined
    # command would bloat the envelope (and the request log) to megabytes.
    # Summarise it for display while ``steps`` still drives the real run.
    if len(statements) > _COMMAND_SUMMARY_THRESHOLD:
        display_command = f"configure ; <{len(statements)} statements> ; {commit_line}"
    else:
        display_command = full_command

    request: Dict[str, Any] = {
        "tool": tool_name,
        "template": template_name,
        "device": device, "host": host, "user": user,
        "statements_count": len(statements),
        "log": log_norm, "deploy": deploy,
        "abort_on_failure": abort_on_failure,
    }
    if request_extra:
        request.update(request_extra)

    response = make_response(
        device=device, host=host, command=display_command,
        deploy=deploy, commit_line=commit_line,
        template={
            "name": template_name,
            "statement_count": len(statements),
        },
    )
    if extra_response:
        response.update(extra_response)

    device_key = device or host or ""
    lock = device_lock(device_key)
    with lock:
        result = drive_configure_commit(
            transport_registry, tool_name=tool_name,
            device=device, host=host, user=user, password=password,
            timeout=timeout, steps=steps, command=display_command,
            request=request, response=response,
            capture_all=not deploy,
            # All-or-nothing on the live push (issue #64): a rejected
            # statement stops the sequence before 'commit and-exit'. The
            # dry-run ends in 'rollback 0' and never applies, so it keeps
            # running to report every rejection at once.
            stop_predicate=stop_on_rejected_statement if deploy else None,
        )
        if result is None:
            return response

        if not result.hit_prompt:
            response["status"] = "timeout"
            response["errors"].append(
                f"Timed out waiting for CLI prompt after {timeout}s."
            )
            response["next_actions"].append(next_action)
            if deploy and abort_on_failure:
                abort_err = abort_shared_candidate(
                    device, host, user, password, timeout,
                )
                if abort_err:
                    response["warnings"].append(
                        f"candidate-abort cleanup failed: {abort_err}"
                    )
                else:
                    response["warnings"].append(
                        "candidate-abort cleanup: ran 'configure ; rollback 0' "
                        "to clear the shared candidate."
                    )
            log_request(tool_name, request, response)
            return response

        # Empty steps ⇒ no per-step transcript; only a captured transcript
        # missing its commit step proves the stop_predicate cut the batch.
        if deploy and result.steps and not commit_was_attempted(result.steps):
            # The stop_predicate cut the batch at a rejected statement, so
            # 'commit and-exit' was never sent — nothing changed on the
            # device. Clear the statements staged before the rejection
            # from the shared candidate.
            response["status"] = "error"
            response["commit"] = {
                "status": "aborted", "user": None, "timestamp": None,
            }
            response["errors"].extend(
                batch_abort_errors(result.steps, len(statements))
            )
            response["next_actions"].append(next_action)
            if abort_on_failure:
                abort_err = abort_shared_candidate(
                    device, host, user, password, timeout,
                )
                if abort_err:
                    response["warnings"].append(
                        f"candidate-abort cleanup failed: {abort_err}"
                    )
                else:
                    response["warnings"].append(
                        "candidate-abort cleanup: ran 'configure ; "
                        "rollback 0' to clear the statements staged before "
                        "the rejection."
                    )
            log_request(tool_name, request, response)
            return response

        is_err, err_lines = detect_error(result.output)
        commit = parse_commit_output(result.output)
        response["commit"] = {
            "status": commit.status,
            "user": commit.user,
            "timestamp": commit.timestamp,
        }

        expected_ok = "check_ok" if not deploy else "ok"
        if commit.status == expected_ok:
            if is_err:
                response["warnings"].append(
                    "commit reported success but error-looking lines were "
                    "detected in stdout; review manually."
                )
                response["warnings"].extend(err_lines[-3:])
            log_request(tool_name, request, response)
            return response

        response["status"] = "error"
        if commit.status == "no_change":
            response["errors"].append(
                "Commit reported no changes — the rendered statements may "
                "already match the running config, or an earlier configure "
                "step failed."
            )
        elif commit.status == "ok" and not deploy:
            response["errors"].append(
                "Dry-run requested (deploy=False) but DNOS reported an "
                "applied commit — review stdout and confirm running config "
                "is intact."
            )
        elif commit.status == "check_ok" and deploy:
            response["errors"].append(
                "Deploy requested (deploy=True) but DNOS reported only a "
                "commit-check result — review stdout and retry."
            )
        else:
            response["errors"].extend(commit.error_lines or [])
        if is_err:
            response["errors"].extend(err_lines[-3:])
        response["next_actions"].append(next_action)

        if deploy and abort_on_failure:
            abort_err = abort_shared_candidate(
                device, host, user, password, timeout,
            )
            if abort_err:
                response["warnings"].append(
                    f"candidate-abort cleanup failed: {abort_err}"
                )
            else:
                response["warnings"].append(
                    "candidate-abort cleanup: ran 'configure ; rollback 0' "
                    "to clear the shared candidate after the failed commit."
                )

        log_request(tool_name, request, response)
        return response


@requires(CAP_CONFIGURE)
def scale_deploy(
    rendered_file: Optional[str] = None,
    rendered_text: Optional[str] = None,
    device: Optional[str] = None,
    log: Optional[str] = None,
    deploy: bool = True,
    abort_on_failure: bool = True,
    host: Optional[str] = None,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASSWORD,
    timeout: int = 300,
) -> Dict[str, Any]:
    """Deploy a rendered config (file or stdin text) to a device.

    The "push the config" half of the local scale flow. Reads a rendered
    ``.cli`` body — one DNOS configure-mode statement per non-blank,
    non-``#`` line — validates every line, stages the whole set into a
    single DNOS candidate, and commits **once** (all-or-nothing). Same
    commit path as ``edit_config``, without the per-call statement ceiling.

    Provide exactly one of ``rendered_file`` (a path) or ``rendered_text``
    (the body itself, e.g. piped via stdin).

    - ``deploy=True`` (default) → ``commit and-exit`` (pushes live).
    - ``deploy=False`` → ``commit check`` + ``rollback 0`` (dry-run).
    - ``abort_on_failure=True`` (default) → on a commit failure, open a
      second channel and run ``configure ; rollback 0`` to clear the
      shared DNOS candidate.

    Args:
        rendered_file: Path to the rendered ``.cli`` file to push.
        rendered_text: Rendered config body (alternative to a file path).
        device: Device alias.
        log: Optional commit annotation (same rules as ``edit_config``).
        deploy: ``True`` to commit live, ``False`` for dry-run.
        abort_on_failure: Cleanup the shared candidate on commit failure.
        host: Raw hostname/IP (alternative to device).
        user: SSH username.
        password: SSH password.
        timeout: Per-step SSH timeout seconds (default 300). This is
            per statement, not for the whole push — a large file just
            takes proportionally longer to stream onto the candidate.
    """
    if (rendered_file is None) == (rendered_text is None):
        return error_response(
            "provide exactly one of rendered_file or rendered_text.",
            device=device, host=host, next_action=SCALE_DEPLOY_NEXT_ACTION,
        )

    if rendered_text is not None:
        text = rendered_text
        source = "<stdin>"
    else:
        path = Path(rendered_file)  # type: ignore[arg-type]
        source = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return error_response(
                f"could not read rendered file {rendered_file!r}: {exc}",
                device=device, host=host, next_action=SCALE_DEPLOY_NEXT_ACTION,
            )

    try:
        statements = jinja_store.split_statements(
            text, max_statements=jinja_store.MAX_SCALE_STATEMENT_COUNT,
        )
    except jinja_store.RenderError as exc:
        return error_response(
            str(exc), device=device, host=host,
            next_action=SCALE_DEPLOY_NEXT_ACTION,
        )

    if not statements:
        return error_response(
            f"{source} contains no statements (only blank/comment lines). "
            "Nothing to deploy.",
            device=device, host=host, next_action=SCALE_DEPLOY_NEXT_ACTION,
        )

    return _deploy_rendered_statements(
        tool_name="scale_deploy",
        statements=statements,
        template_name=source,
        device=device, host=host, user=user, password=password,
        timeout=timeout, log=log,
        deploy=deploy, abort_on_failure=abort_on_failure,
        max_statements=jinja_store.MAX_SCALE_STATEMENT_COUNT,
        extra_response={
            "scale": {
                "source": source,
                "statement_count": len(statements),
            },
        },
        request_extra={"source": source},
        next_action=SCALE_DEPLOY_NEXT_ACTION,
    )


def register(mcp) -> None:
    """Wire this module's tools onto a FastMCP instance."""
    mcp.tool()(template_list)
    mcp.tool()(template_get)
    mcp.tool()(render_config)
    mcp.tool()(scale_deploy)
