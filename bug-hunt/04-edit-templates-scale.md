# Area 4 — Config edit / templates / scale-deploy

First read `/home/dn/work/qactl/bug-hunt/_context.md` for the rules of
engagement and bug taxonomy. Then hunt this area thoroughly.

These tools build and commit DNOS configuration (the most destructive
surface): candidate edits, commit/rollback sequences, Jinja templating,
and a subprocess that runs user-supplied Python generators. Watch for
commit/rollback correctness, candidate-config leakage between sessions,
sandbox escapes in the generator subprocess, and `--yes` gating.

## Files (read all; follow imports)
- `dnctl/cli/tools/edit.py`            (edit_config / edit_config_check / rollback / load override)
- `dnctl/cli/core/edit_helpers.py`
- `dnctl/cli/core/configure_commit.py`
- `dnctl/cli/core/commit_sequence.py`
- `dnctl/cli/tools/templates.py`       (template store CRUD + render)
- `dnctl/cli/core/jinja_store.py`      (Jinja render, the generator subprocess, audit dir)
- `dnctl/cli/core/validation.py`
- relevant `dnctl/cli/app.py` commands (check `--yes` gates on edit/scale-deploy)

## Focus questions
- commit/rollback: on a failed `commit` or `commit check`, is the shared
  candidate config always cleared/aborted? Can one session's candidate
  leak into another (the CLI is one-shot, but the MCP server is not)?
- Is every config-mutating command gated by `--yes` + TTY refusal?
- jinja_store generator subprocess: is it really isolated (`python3 -I`)?
  Any way user vars/script reach a shell, write outside the audit dir, or
  the template itself injects (Jinja SSTI is moot since user owns it, but
  check file path handling / `name` sanitisation for traversal)?
- Template `name` validation: `[A-Za-z0-9._-]` — any path traversal or
  overwrite of unrelated files via the name?
- render preflight: declared_variables logic correct? Does an empty or
  malformed vars source crash vs. error cleanly?
- Statement payloads via stdin/`--file`: resolution + size handling.
