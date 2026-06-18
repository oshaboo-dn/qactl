"""Jinja2-based CLI-config template store for cli-mcp.

Layout on disk:

    cli-mcp/
      jinja/
        templates/              # saved Jinja templates
          <name>.j2
        scale/                  # audit trail for deploy_scale runs
          <template>/<UTC-ts>/
            script.py           # agent-supplied generator
            vars.yml            # captured stdout of the generator
            rendered.cli        # rendered statements, one per line

Templates are plain Jinja2 text. Each non-blank, non-``#``-comment line of
the rendered output becomes one DNOS configure-mode statement (the same
shape ``edit_config(statements=[...])`` already accepts).

Validation / safety policy:

- ``jinja2.StrictUndefined`` — any reference to a variable the vars YAML
  did not provide is an error, never an empty string. Catches typos
  before anything ever reaches the device.
- Template names are ``[A-Za-z0-9._-]{1,60}`` — the file lives on our
  filesystem, not the device, so we pick the conservative intersection of
  "valid filename" and "safe to echo in the envelope".
- Rendered lines inherit the ``edit_config`` per-statement rules (no
  control chars, ≤ :data:`MAX_STATEMENT_LEN` chars, ≤
  :data:`MAX_STATEMENT_COUNT` lines total) so the deploy path never
  receives anything it would reject.
- ``deploy_scale`` subprocess runs the agent's python3 script with
  ``-I`` (isolated mode: no user site-packages, no ``PYTHONPATH``), a
  configurable wall-clock timeout (clamped to a small range), and a
  dedicated cwd under ``scale/<template>/<ts>/`` so any stray file
  writes land inside the audit dir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import jinja2
    from jinja2 import meta as jinja2_meta
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "jinja2 is required for jinja_store; add 'jinja2>=3.1' to requirements.txt"
    ) from exc

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "pyyaml is required for jinja_store; add 'pyyaml>=6.0' to requirements.txt"
    ) from exc


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------

from dnctl.core import paths as _paths

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent

JINJA_ROOT = _paths.state_dir("cli") / "jinja"
TEMPLATES_DIR = JINJA_ROOT / "templates"
SCALE_DIR = JINJA_ROOT / "scale"


def _ensure_dirs() -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    SCALE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Constants shared with edit_config
# ---------------------------------------------------------------------------

# Mirror the limits enforced by _validate_edit_statements in cli_mcp_server.py
# so anything we render is guaranteed to be acceptable to the deploy path.
MAX_STATEMENT_LEN = 1000
MAX_STATEMENT_COUNT = 200

# The scale path (scale_build / scale_deploy) renders to a local file first
# and pushes it in one commit, so it is not bound by the small edit_config
# ceiling. 8k services × a handful of lines each still fits comfortably.
MAX_SCALE_STATEMENT_COUNT = 100_000

# Control chars (NUL + C0 minus TAB) — same regex as _EDIT_CONFIG_BAD_CHAR_RE.
_BAD_CHAR_RE = re.compile(r"[\x00-\x08\x0a-\x1f]")

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
_TEMPLATE_EXT = ".j2"

_DEFAULT_EXEC_TIMEOUT = 30.0
_MIN_EXEC_TIMEOUT = 5.0
_MAX_EXEC_TIMEOUT = 300.0

_EXEC_STDOUT_CAP = 5 * 1024 * 1024  # 5 MiB of YAML is plenty.
_EXEC_STDERR_CAP = 64 * 1024

# Agent-supplied scripts live on our FS; we don't need to support huge
# sources. 512 KiB covers any practical generator.
MAX_SCRIPT_LEN = 512 * 1024
MAX_CONTENT_LEN = 512 * 1024
MAX_VARS_YAML_LEN = 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TemplateError(Exception):
    """Base class for jinja_store errors with an operator-friendly message."""


class TemplateNotFoundError(TemplateError):
    pass


class TemplateExistsError(TemplateError):
    pass


class TemplateValidationError(TemplateError):
    """Template body is malformed (syntax / name / size)."""


class RenderError(TemplateError):
    """Rendering failed — missing vars, bad YAML, or post-render line rejected."""


class ScaleExecError(TemplateError):
    """The scale-runner subprocess failed — non-zero exit, timeout, or bad YAML."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class TemplateMetadata:
    name: str
    path: str
    size: int
    mtime_utc: str
    declared_variables: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "mtime_utc": self.mtime_utc,
            "declared_variables": self.declared_variables,
        }


@dataclass
class RenderResult:
    name: Optional[str]
    declared_variables: List[str]
    statements: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "declared_variables": self.declared_variables,
            "statements": self.statements,
            "warnings": self.warnings,
        }


@dataclass
class ScaleRunArtifacts:
    run_dir: str
    script_path: str
    vars_path: str
    rendered_path: str
    exec_stderr_tail: str
    exec_stdout_truncated: bool
    exec_stderr_truncated: bool


# ---------------------------------------------------------------------------
# Name + path helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise TemplateValidationError("template name must be a string.")
    if not _NAME_RE.match(name):
        raise TemplateValidationError(
            "template name must match [A-Za-z0-9._-]{1,60} (got "
            f"{name!r})."
        )
    return name


def template_path(name: str) -> Path:
    return TEMPLATES_DIR / (_validate_name(name) + _TEMPLATE_EXT)


# ---------------------------------------------------------------------------
# Core Jinja environment — kept private so callers always go through the
# validated helpers below.
# ---------------------------------------------------------------------------


def _build_env() -> jinja2.Environment:
    # autoescape is off: DNOS CLI is plain text, HTML escaping would
    # corrupt command syntax (e.g. `&` inside a regex).
    return jinja2.Environment(
        loader=jinja2.BaseLoader(),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        keep_trailing_newline=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _parse_and_declared(content: str) -> Tuple[jinja2.Environment, jinja2.Template, Set[str]]:
    """Parse ``content``; return env, compiled template, declared vars set.

    Raises :class:`TemplateValidationError` with line/col on syntax error.
    """
    if not isinstance(content, str):
        raise TemplateValidationError("template content must be a string.")
    if not content.strip():
        raise TemplateValidationError("template content must be non-empty.")
    if len(content) > MAX_CONTENT_LEN:
        raise TemplateValidationError(
            f"template content must be ≤ {MAX_CONTENT_LEN} bytes."
        )

    env = _build_env()
    try:
        ast = env.parse(content)
    except jinja2.TemplateSyntaxError as exc:
        loc = f"line {exc.lineno}" if exc.lineno else "?"
        raise TemplateValidationError(
            f"Jinja syntax error at {loc}: {exc.message}"
        ) from exc

    declared = jinja2_meta.find_undeclared_variables(ast)

    # Compile now so a later `.render` call can't fail with a fresh
    # TemplateSyntaxError we missed (belt and braces — parse catches it,
    # but this also exercises the compiler).
    try:
        template = env.from_string(content)
    except jinja2.TemplateSyntaxError as exc:  # pragma: no cover
        loc = f"line {exc.lineno}" if exc.lineno else "?"
        raise TemplateValidationError(
            f"Jinja compile error at {loc}: {exc.message}"
        ) from exc

    return env, template, declared


def declared_variables(content: str) -> List[str]:
    """Return the sorted list of top-level variables the template reads."""
    _, _, declared = _parse_and_declared(content)
    return sorted(declared)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def save_template(
    name: str, content: str, overwrite: bool = False,
) -> TemplateMetadata:
    """Validate + write a template. Fails if it already exists (unless
    ``overwrite=True``)."""
    _ensure_dirs()
    path = template_path(name)
    declared = declared_variables(content)  # may raise TemplateValidationError

    if path.exists() and not overwrite:
        raise TemplateExistsError(
            f"template {name!r} already exists at {path}; delete it first "
            "or pass overwrite=True."
        )

    # Atomic-ish write via tmp + rename so a crashing editor can't leave a
    # half-written .j2 on disk.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)

    return _stat_template(path, declared)


def delete_template(name: str) -> str:
    path = template_path(name)
    if not path.exists():
        raise TemplateNotFoundError(f"template {name!r} not found at {path}.")
    path.unlink()
    return str(path)


def rename_template(old: str, new: str) -> TemplateMetadata:
    src = template_path(old)
    dst = template_path(new)
    if not src.exists():
        raise TemplateNotFoundError(f"template {old!r} not found at {src}.")
    if dst.exists():
        raise TemplateExistsError(
            f"template {new!r} already exists at {dst}; pick another name "
            "or delete the target first."
        )
    os.rename(src, dst)
    try:
        declared = declared_variables(dst.read_text(encoding="utf-8"))
    except TemplateValidationError:
        # The template was already on disk — preserve the rename even if
        # the body is now unparseable (shouldn't happen), and surface
        # empty declared_variables. The caller can `template_get` to see
        # the raw body.
        declared = []
    return _stat_template(dst, declared)


def read_template(name: str) -> Tuple[str, TemplateMetadata]:
    path = template_path(name)
    if not path.exists():
        raise TemplateNotFoundError(f"template {name!r} not found at {path}.")
    content = path.read_text(encoding="utf-8")
    declared = declared_variables(content)
    return content, _stat_template(path, declared)


def list_templates() -> List[TemplateMetadata]:
    _ensure_dirs()
    out: List[TemplateMetadata] = []
    for path in sorted(TEMPLATES_DIR.glob(f"*{_TEMPLATE_EXT}")):
        try:
            content = path.read_text(encoding="utf-8")
            declared = declared_variables(content)
        except (OSError, TemplateValidationError):
            declared = []
        out.append(_stat_template(path, declared))
    out.sort(key=lambda m: m.mtime_utc, reverse=True)
    return out


def _stat_template(path: Path, declared: List[str]) -> TemplateMetadata:
    st = path.stat()
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    name = path.stem  # filename without .j2
    return TemplateMetadata(
        name=name,
        path=str(path),
        size=st.st_size,
        mtime_utc=mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
        declared_variables=sorted(declared),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _parse_vars_yaml(vars_yaml: Optional[str]) -> Dict[str, Any]:
    if vars_yaml is None:
        return {}
    if not isinstance(vars_yaml, str):
        raise RenderError("vars_yaml must be a string (YAML document) or null.")
    if len(vars_yaml) > MAX_VARS_YAML_LEN:
        raise RenderError(
            f"vars_yaml must be ≤ {MAX_VARS_YAML_LEN} bytes."
        )
    if not vars_yaml.strip():
        return {}
    try:
        data = yaml.safe_load(vars_yaml)
    except yaml.YAMLError as exc:
        raise RenderError(f"vars_yaml is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RenderError(
            "vars_yaml must deserialize to a mapping at the top level "
            f"(got {type(data).__name__})."
        )
    return data


def _split_and_validate_lines(
    rendered: str,
    *,
    max_statements: int = MAX_STATEMENT_COUNT,
) -> Tuple[List[str], List[str]]:
    """Split rendered output into statements; return (statements, warnings).

    Raises :class:`RenderError` on hard rejects (control chars, oversize
    line, too many lines). Empty render is a warning, not an error.

    ``max_statements`` is the per-call ceiling. The edit_config-style
    deploy path keeps the conservative :data:`MAX_STATEMENT_COUNT`; the
    scale path raises it to :data:`MAX_SCALE_STATEMENT_COUNT`.
    """
    statements: List[str] = []
    warnings: List[str] = []
    for raw_line in rendered.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if _BAD_CHAR_RE.search(line):
            raise RenderError(
                f"rendered statement contains a control character: {line!r}"
            )
        if len(line) > MAX_STATEMENT_LEN:
            raise RenderError(
                f"rendered statement exceeds {MAX_STATEMENT_LEN} chars "
                f"(got {len(line)}): {line[:80]!r}..."
            )
        statements.append(line)

    if len(statements) > max_statements:
        raise RenderError(
            f"rendered {len(statements)} statements; this path accepts "
            f"at most {max_statements} per call."
        )
    if not statements:
        warnings.append(
            "rendered 0 statements — check your vars (empty loop over "
            "empty list, or all conditionals were false)."
        )
    return statements, warnings


def split_statements(
    text: str,
    *,
    max_statements: int = MAX_SCALE_STATEMENT_COUNT,
) -> List[str]:
    """Parse a rendered ``.cli`` file body back into a statement list.

    Applies the same per-line rules as rendering (skip blank / ``#``
    comments, reject control chars / oversize lines, cap the count).
    Used by the scale-deploy path to load a previously-built config file
    from disk. Raises :class:`RenderError` on any rejected line.
    """
    statements, _ = _split_and_validate_lines(text, max_statements=max_statements)
    return statements


def render(
    *,
    name: Optional[str] = None,
    content: Optional[str] = None,
    vars_yaml: Optional[str] = None,
    max_statements: int = MAX_STATEMENT_COUNT,
) -> RenderResult:
    """Validate + (optionally) render a template.

    Exactly one of ``name`` / ``content`` must be provided.

    - Without ``vars_yaml``: parse the template, return declared variables.
      ``statements`` is empty.
    - With ``vars_yaml``: also render with StrictUndefined, split into
      statements, enforce per-line rules (``max_statements`` is the count
      ceiling — raise it for the scale path).

    Always raises ``RenderError`` / ``TemplateValidationError`` on failure
    — returning an empty envelope on bad input would hide bugs from the
    agent.
    """
    if (name is None) == (content is None):
        raise TemplateValidationError(
            "render requires exactly one of name / content."
        )

    resolved_name: Optional[str] = None
    if name is not None:
        resolved_name = _validate_name(name)
        body, _ = read_template(resolved_name)
    else:
        body = content  # type: ignore[assignment]

    _, template, declared = _parse_and_declared(body)
    declared_sorted = sorted(declared)

    # Preflight-only when no vars string was supplied. An explicit empty
    # / whitespace-only string is treated as "render with zero vars" —
    # the StrictUndefined path below will fire if the template actually
    # references anything.
    if vars_yaml is None:
        return RenderResult(
            name=resolved_name,
            declared_variables=declared_sorted,
            statements=[],
            warnings=[],
        )
    vars_dict = _parse_vars_yaml(vars_yaml)

    try:
        rendered = template.render(**vars_dict)
    except jinja2.UndefinedError as exc:
        missing = _missing_var_hint(declared_sorted, vars_dict)
        hint = f" (missing: {missing})" if missing else ""
        raise RenderError(f"undefined variable during render: {exc}{hint}") from exc
    except jinja2.TemplateError as exc:
        raise RenderError(f"render failed: {exc}") from exc
    except Exception as exc:
        raise RenderError(f"render raised unexpected error: {exc}") from exc

    statements, warnings = _split_and_validate_lines(
        rendered, max_statements=max_statements,
    )
    return RenderResult(
        name=resolved_name,
        declared_variables=declared_sorted,
        statements=statements,
        warnings=warnings,
    )


def _missing_var_hint(declared: List[str], vars_dict: Dict[str, Any]) -> str:
    missing = [v for v in declared if v not in vars_dict]
    return ", ".join(sorted(missing))


# ---------------------------------------------------------------------------
# Scale runner
# ---------------------------------------------------------------------------


_SAFE_TS_RE = re.compile(r"[^A-Za-z0-9]")


def _utc_ts() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def scale_run_dir(template_name: str, *, ts: Optional[str] = None) -> Path:
    _validate_name(template_name)
    stamp = ts or _utc_ts()
    return SCALE_DIR / template_name / stamp


def run_scale_script(
    *,
    template_name: str,
    script: str,
    exec_timeout: float = _DEFAULT_EXEC_TIMEOUT,
) -> Tuple[Dict[str, Any], ScaleRunArtifacts]:
    """Run a Python generator, capture stdout as YAML, persist everything.

    Returns ``(vars_dict, artifacts)``. Raises :class:`ScaleExecError` on
    subprocess failure, timeout, or non-YAML stdout.
    """
    _validate_name(template_name)
    if not isinstance(script, str) or not script.strip():
        raise ScaleExecError("python_script must be a non-empty string.")
    if len(script) > MAX_SCRIPT_LEN:
        raise ScaleExecError(
            f"python_script must be ≤ {MAX_SCRIPT_LEN} bytes."
        )

    try:
        timeout = float(exec_timeout)
    except (TypeError, ValueError) as exc:
        raise ScaleExecError(f"exec_timeout must be a number: {exc}") from exc
    timeout = max(_MIN_EXEC_TIMEOUT, min(_MAX_EXEC_TIMEOUT, timeout))

    _ensure_dirs()
    run_dir = scale_run_dir(template_name)
    run_dir.mkdir(parents=True, exist_ok=True)

    script_path = run_dir / "script.py"
    vars_path = run_dir / "vars.yml"
    script_path.write_text(script, encoding="utf-8")

    # python3 -I: isolated mode — ignore PYTHONPATH, user site-packages,
    # PYTHON* env vars. Keeps the MCP host's env out of agent scripts and
    # vice-versa. cwd is the run dir so any stray file writes land here.
    env = {
        # Preserve minimal env: PATH (for `python3` child invocations) and
        # a fixed LANG. Everything else is stripped.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(run_dir),  # stray ~/.cache writes land in the audit dir
    }

    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(script_path)],
            cwd=str(run_dir),
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Preserve partial output for debugging.
        stdout = _decode_truncated(exc.stdout or b"", _EXEC_STDOUT_CAP)[0]
        stderr = _decode_truncated(exc.stderr or b"", _EXEC_STDERR_CAP)[0]
        vars_path.write_text(stdout, encoding="utf-8")
        (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        raise ScaleExecError(
            f"python_script timed out after {timeout}s; "
            f"partial artefacts under {run_dir}"
        ) from exc

    stdout, stdout_trunc = _decode_truncated(proc.stdout or b"", _EXEC_STDOUT_CAP)
    stderr, stderr_trunc = _decode_truncated(proc.stderr or b"", _EXEC_STDERR_CAP)

    vars_path.write_text(stdout, encoding="utf-8")
    if stderr:
        (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    if proc.returncode != 0:
        raise ScaleExecError(
            f"python_script exited with returncode={proc.returncode}; "
            f"see stderr.log under {run_dir}. Tail: "
            f"{stderr[-400:] if stderr else '(empty)'}"
        )

    try:
        data = yaml.safe_load(stdout) if stdout.strip() else {}
    except yaml.YAMLError as exc:
        raise ScaleExecError(
            f"python_script stdout is not valid YAML: {exc}. "
            f"See vars.yml under {run_dir}."
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ScaleExecError(
            "python_script stdout must deserialize to a mapping at the top "
            f"level (got {type(data).__name__}). See vars.yml under {run_dir}."
        )

    artifacts = ScaleRunArtifacts(
        run_dir=str(run_dir),
        script_path=str(script_path),
        vars_path=str(vars_path),
        rendered_path=str(run_dir / "rendered.cli"),
        exec_stderr_tail=stderr[-800:] if stderr else "",
        exec_stdout_truncated=stdout_trunc,
        exec_stderr_truncated=stderr_trunc,
    )
    return data, artifacts


def write_rendered_cli(rendered_path: str, statements: List[str]) -> None:
    Path(rendered_path).write_text(
        "\n".join(statements) + ("\n" if statements else ""),
        encoding="utf-8",
    )


def _decode_truncated(blob: bytes, cap: int) -> Tuple[str, bool]:
    truncated = False
    if len(blob) > cap:
        blob = blob[:cap]
        truncated = True
    return blob.decode("utf-8", errors="replace"), truncated


__all__ = [
    "JINJA_ROOT",
    "TEMPLATES_DIR",
    "SCALE_DIR",
    "MAX_STATEMENT_LEN",
    "MAX_STATEMENT_COUNT",
    "MAX_SCALE_STATEMENT_COUNT",
    "TemplateError",
    "TemplateNotFoundError",
    "TemplateExistsError",
    "TemplateValidationError",
    "RenderError",
    "ScaleExecError",
    "TemplateMetadata",
    "RenderResult",
    "ScaleRunArtifacts",
    "template_path",
    "declared_variables",
    "save_template",
    "delete_template",
    "rename_template",
    "read_template",
    "list_templates",
    "render",
    "split_statements",
    "run_scale_script",
    "write_rendered_cli",
    "scale_run_dir",
]
