# qactl — project guide for Claude

One **agent-shaped** tool fronting an entire QA workflow — DNOS devices,
IxNetwork traffic, Jira, Confluence, Jenkins — behind one consistent contract,
exposed over two interchangeable fronts that drive the *same* shared tool layer:

- a **CLI**: `qactl <group> <cmd> --json`
- a **stdio MCP server**: `qactl mcp <group>` — **FROZEN** (2026-07): the CLI
  is the only exposed front for now. Don't extend or fix `qactl/mcp/`; don't
  delete it either. Keep the tool layer front-agnostic so MCP can return as a
  frontend-only change later.

See [README.md](README.md) for the full surface. This file is the working
contract; keep it short and follow it.

## The agent-shaped contract (don't break it)
- **`--json` everywhere** emits the raw envelope — keep it lossless.
  Envelope: `{status, kind, result, warnings, errors, next_actions}`.
- **Real exit codes**: 0 only on `status` ok/warning, non-zero otherwise.
- **stdin (`-`) / `--file` / inline** accepted for any text payload arg.
- **`--yes` gate** on every destructive/mutating op; refuses off a TTY without
  it. The MCP equivalent is `confirm=true`. New destructive commands MUST add
  the gate.
- **One envelope shape** across `jira` / `confluence` / `jenkins`.

## Groups & layout
- **Native (edit here)**: `jira` / `confluence` / `jenkins` under `qactl/`.
  Each is `client.py` (REST) + `tools.py` (envelopes) + `cli.py`, on a shared
  `qactl/core/` (envelope, output/exit-codes, env creds, request log, plumbing).
  `qactl/mcp/` maps groups to their MCP tool surface; `qactl/__main__.py` is the
  dispatcher.
- **Vendored, delegated — keep liftable**: `cli`/`nc`/`gnmi`/`rc`/`setup` (the
  `dnctl/` package) and `ixia` (`ixiactl/` + `ixia*/`). Don't refactor their
  internals to "match" native style; keep changes minimal and isolated. Fixes
  there belong upstream conceptually.

## Secrets
- **NEVER commit credentials.** All tokens resolve at runtime from the
  environment (`ATLASSIAN_*`, `JENKINS_*`; device creds via `qactl setup`).
  No `.env`, no token defaults in code/docstrings/tests/issues.

## Workflow — issues, commits, releases
- Bugs/change requests arrive as `gh` issues. Triage on read: confirm the repro
  (command + exit code) before fixing. Label `bug` / `enhancement` / `needs-info`.
- **Work directly on `main`** — no worktrees, no per-issue branches, no PRs.
  Keep `main` releasable: commit only green, focused work.
- Pull first: `git fetch origin && git pull --ff-only`.
- Small, focused, imperative commit subjects ≤72 chars referencing the issue,
  with a `Closes #N` line so it closes on push:
  `fix: gate jira comment delete on --yes (#4)`.
- Push directly to `main`. **No force-push to `main`. No `--no-verify`.**
- Tag `vX.Y.Z` after notable changes and bump the version the CLI reports —
  but **only create tags when explicitly asked**.
- No edits outside this repo.

## Tests & lint
- Add tests that fail before and pass after, alongside `tests/`.
- `python3 -m pytest -q` must be green before committing. Fix lints you
  introduce. Don't finish red.

## SSH vs CLI — the operating principle
The agent gets the typed, gated CLI, never a raw shell: structured `--json`,
`--yes` safety gates, and meaningful exit codes are what make it agent-safe.
SSH/gNMI is just the transport underneath. Humans explore the device over raw
SSH (it's the spec source), then distill stable fields into a typed, gated
subcommand — the manual session is the research, the CLI is the shipped artifact.

## Communication
- Default to short answers. No long recaps or multi-section summaries unless
  asked. For minor process/release choices (version bumps, changelog wording,
  commit splitting), just pick a sensible option and do it.
- A short/ambiguous prompt with no concrete task ("go", "next", "?") is a
  trigger to **check open GitHub issues and start working the top one** — don't
  ask what was meant. Run `gh issue list --state open`, pick the actionable one
  (oldest open, or the single one), and begin the work-issue flow. Only stop to
  say so if there are no open issues.
