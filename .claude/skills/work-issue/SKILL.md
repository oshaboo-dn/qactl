---
name: work-issue
description: >-
  End-to-end handling of a GitHub issue in the qactl repo: find the open issue,
  investigate the codebase, implement the fix directly on main, add and run
  tests (pytest), lint, then commit and push to main, closing the issue. Use
  when the user says "we have a new bug", "new bug/feature req", "there's a new
  issue", "handle the latest issue", asks to take a GitHub bug/feature report
  and ship a fix, or gives a short/ambiguous prompt ("go", "next", "?") with no
  other concrete task.
---

# Work a GitHub Issue End-to-End (qactl)

Take a freshly filed GitHub issue from report to a committed, pushed fix on
`main` with no further prompting. Default to acting; only stop for the blockers
listed below.

Follows [CLAUDE.md](../../../CLAUDE.md): **work directly on `main`** — no
worktrees, no branches, no PRs. Close issues via the commit (`Closes #N`).

## Workflow

```
- [ ] 1. Find the issue (gh issue list --state open)
- [ ] 2. Understand it + reproduce mentally
- [ ] 3. Investigate the codebase
- [ ] 4. Pull latest on main
- [ ] 5. Implement the fix
- [ ] 6. Add/extend tests in tests/
- [ ] 7. Run tests + lint
- [ ] 8. Commit + push to main (Closes #N)
```

### 1. Find the issue
`gh issue list --state open --limit 50`; if one, that's it; if several, the most
recent unless the user named one. Read it fully: `gh issue view <N>`. Triage and
label (`bug` / `enhancement` / `needs-info`).

### 2–3. Understand + investigate
Parse Summary / Repro / Observed / Desired; confirm the repro (command + exit
code) before fixing. Trace the real code path that produces the Observed
behavior. Native domains are `jira` / `confluence` / `jenkins`, each a
`client.py` (REST) + `tools.py` (envelopes) + `cli.py` under `qactl/`, on a
shared `core/`. Device/ixia groups are vendored — keep changes minimal there.

### 4. Pull latest
```bash
git fetch origin && git pull --ff-only
```

### 5. Implement
Keep the agent-shaped contract intact (see CLAUDE.md): `--json` lossless, real
exit codes, `--yes` gate on destructive ops, one envelope shape, **no secrets**.
Match existing style; update help text / README rows you change.

### 6–7. Tests + lint
Add tests that fail before and pass after, alongside `tests/`. Then:
```bash
python3 -m pytest -q
```
Fix any lints you introduced. Don't finish red.

### 8. Commit + push to main
Only commit green work — `main` stays releasable.
```bash
git add <changed files>
git commit -m "<type>: <subject> (#<N>)

Closes #<N>"
git push origin main
```
The `Closes #N` line closes the issue on push. No force-push, no `--no-verify`.

## Stop and ask instead of guessing
- More than one plausible target issue and the user didn't name one.
- A product/scope decision (new public surface, breaking change) vs a
  contained fix.
- The fix needs a live Jira/Confluence/Jenkins resource you can't reach —
  implement what's safe, then say what needs credentials/a server.
- Tests can't pass without changing unrelated behavior.
- Any destructive/irreversible git action.
