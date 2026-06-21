---
name: work-issue
description: >-
  End-to-end handling of a GitHub issue in the qactl repo: find the open issue,
  investigate the codebase, implement the fix on a per-issue branch, add and run
  tests (pytest), lint, then open a PR that closes the issue and let CI run. Use
  when the user says "we have a new bug", "new bug/feature req", "there's a new
  issue", "handle the latest issue", or otherwise asks to take a GitHub
  bug/feature report and ship a fix.
---

# Work a GitHub Issue End-to-End (qactl)

Take a freshly filed GitHub issue from report to an open, CI-green PR with no
further prompting. Default to acting; only stop for the blockers listed below.

Follows `.cursor/rules/repo-workflow.mdc`: **one issue → one branch → one PR**,
never commit to `main`, close issues via the merge (`Closes #N`), squash-merge.

## Workflow

```
- [ ] 1. Find the issue (gh issue list --state open)
- [ ] 2. Understand it + reproduce mentally
- [ ] 3. Investigate the codebase
- [ ] 4. Worktree off main (fix/<N>-slug or feat/<N>-slug) — never share the checkout
- [ ] 5. Implement the fix
- [ ] 6. Add/extend tests in tests/
- [ ] 7. Run tests + lint
- [ ] 8. Commit, push, open a PR that closes the issue
- [ ] 9. Let CI run, then squash-merge
```

### 1. Find the issue
`gh issue list --state open --limit 50`; if one, that's it; if several, the most
recent unless the user named one. Read it fully: `gh issue view <N>`. Triage and
label.

### 2–3. Understand + investigate
Parse Summary / Repro / Observed / Desired. Trace the real code path that
produces the Observed behavior. The domains are `jira` / `confluence` /
`jenkins`, each a `client.py` (REST) + `cli.py` (subcommands) under `qactl/`,
on a shared `core/` (envelope, output, creds, common).

### 4. Worktree off main
Other agents may share this repo, so never `git checkout` a branch in the
primary clone — work in a dedicated worktree (see the repo rule's
"Concurrency (worktrees)" clause):
```bash
git fetch origin
git worktree add ../qactl-<N> -b fix/<N>-slug origin/main
cd ../qactl-<N>
```
Do all of steps 5-9 from that worktree. When the PR merges, clean up:
`git worktree remove ../qactl-<N>` and `git branch -D fix/<N>-slug`.

### 5. Implement
Keep the agent-shaped contract intact (see the repo rule): `--json` lossless,
exit codes, `--yes` gate on destructive ops, one envelope shape, **no secrets**.
Match existing style; update help text / README rows you change.

### 6–7. Tests + lint
Add tests that fail before and pass after, alongside `tests/`. Then:
```bash
python3 -m pytest -q
```
Fix any lints you introduced. Don't finish red.

### 8. Commit, push, PR
```bash
git add <changed files>
git commit -m "<type>: <subject> (#<N>)"
git push -u origin HEAD
gh pr create --title "<subject>" --body "$(cat <<'EOF'
Closes #<N>

## Summary
- <what changed and why>

## Test plan
- [x] pytest -q
EOF
)"
```

### 9. CI green, then merge
```bash
gh pr checks <PR> --watch
gh pr merge <PR> --squash --delete-branch
git worktree remove ../qactl-<N> && git branch -D fix/<N>-slug   # from the primary clone
```
Only merge green. On red, push a NEW commit (never amend a pushed commit).
Note: `--delete-branch`'s local cleanup fails while the branch is checked
out in a worktree (`fatal: '<branch>' is already checked out`) — the remote
merge still succeeds; just remove the worktree + local branch afterwards.

## Stop and ask instead of guessing
- More than one plausible target issue and the user didn't name one.
- A product/scope decision (new public surface, breaking change) vs. a
  contained fix.
- The fix needs a live Jira/Confluence/Jenkins resource you can't reach —
  implement what's safe, then say what needs credentials/a server.
- Tests can't pass without changing unrelated behavior.
- Any destructive/irreversible git action.
