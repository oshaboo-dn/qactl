# qactl `cli` bug hunt — consolidated findings

Six read-only review agents covered the whole `dnctl/cli` package (~14.7k
lines) plus `dnctl/core/options.py` / `devices.py` / `output.py`.
Deduped and ranked below. Line numbers are from the hunt; re-confirm
before editing.

Recurring theme: **"false success"** — many tools decide `status: ok`
from the last step's stdout (or just "prompt returned"), so earlier
download/load/rollback failures, timeouts, missing files, and 100% ping
loss are reported as success (exit 0). Second theme: **destructive tools
ungated on the MCP front** (CLI gates with `--yes`, the tool layer does
not). Third: **dual-front state** bugs (in-process registries / caches
that don't fit the one-shot CLI or the long-lived MCP).

## HIGH

1. **tech-support is still #17.** `create_techsupport` registers an
   in-memory job + daemon worker and the CLI exits → worker killed, SFTP
   upload never runs, `techsupport show` always fails. (`app.py:492`,
   `techsupport.py:838`). Apply the tar-load fix (`block=True` + disk
   persistence).
2. **restore false success.** `restore_device` parses only the final
   `commit` stdout; SFTP download / `load override` failures in earlier
   steps are invisible → `status: ok` on a destructive restore that
   didn't apply. (`backup.py:609`). Use `capture_all=True` / scan
   `result.steps`.
3. **edit dry-run false success + dirty candidate.** `parse_commit_output`
   returns `check_ok` on the first match, ignoring a later failed
   `rollback 0`; `edit_config_check` / `scale_deploy --check` then return
   ok with staged statements left in the shared candidate.
   (`commit_sequence.py:71`, `edit.py:352`, `templates.py:408`).
4. **`load_override_factory_default` & `rollback_config` ignore outcome.**
   No `hit_prompt` / `detect_error` / commit parse → timeouts and commit
   failures return `status: ok`. (`edit.py:419,493`).
5. **factory-reset & rollback skip `device_lock`** despite the module
   contract; concurrent MCP sessions can corrupt the shared candidate.
   (`edit.py:387-501`).
6. **`qactl cli device add` is completely broken.** CLI passes a required
   positional `name` that `manage_device(operation="add")` explicitly
   rejects → every add fails before the SSH probe. (`app.py:570`,
   `devices.py:595`).
7. **Secondary-alias remove/refresh corrupts the registry.**
   `remove_device_host` / `_refresh_device` don't canonicalize the alias:
   remove-by-nickname returns `removed=True` while the device stays;
   refresh/partial-remove forks ghost keys. (`session.py:186-198`,
   `devices.py:402,672`).
8. **`clear` is unreachable from the CLI and ungated on MCP.** Imported in
   `app.py:16` but no `@app.command`; `clear.py` has no confirm/dry-run.
   State-mutating clears bypass the gate surface entirely.
9. **Missing log file → `status: ok` / exit 0.** `log_read` echoes "log
   file not found" to stderr + `exit 2`, but `detect_error` has no
   pattern for it and the shell exit isn't propagated → agents read
   "no log" as "no activity". (`log_read.py:160`, `runner.py:85`,
   `errors.py`).
10. **Idle reaper kills long single steps.** The 1800s idle reaper only
    sees `last_used` *between* steps; a single `target-stack load` (up to
    7200s) loses its pooled SSH transport mid-download after ~30 min on
    the long-lived MCP. (`session.py:327`).
11. **Destructive MCP tools ungated.** `edit_config`,
    `load_override_factory_default`, `rollback_config`, `clear`,
    `manage_device`, `kill_9_ncc_process` run immediately over
    `qactl mcp cli` with no `confirm`/dry-run (CLI_ONLY only excludes
    `scale_deploy` + `request_system_tar_load`). (`registry.py`, various).

## MEDIUM

- **Per-process device-slot mutex is ineffective under the CLI** — two
  concurrent `tar-load start` / `techsupport create` on one device both
  pass `active_for_device` (each has an empty in-memory registry).
  (`jobs.py`, `tarload.py`, `techsupport.py`). Needs a cross-process lock.
- **`job_store.load()` path traversal** — unsanitized `job_id` in
  `tar-load show` joins into the path; can read `.json` outside the cache
  dir. *(new code from the #17 fix — mine.)* (`job_store.py:42`).
- **JobRegistry register TOCTOU** — `active_for_device` then `register`
  isn't atomic; two MCP calls can both register. (`jobs.py:119-137`).
- **tar-load pre-check aborts on first connect blip** — one transient
  `ConnectError` during polling fails an otherwise-good load; tech-support
  tolerates N. (`tarload.py:_poll_tar_pre_check`).
- **`list_backups` fleet-wide sort is by filename (device-dominated), not
  time** — "newest first" is wrong across devices. (`backup_store.py:408`).
- **`read_backup` / `download_bytes` unbounded read** into the JSON
  envelope. (`backup.py:722`, `backup_store.py:527`).
- **Stale `DEVICE_HOSTS` in long-lived MCP** after another process edits
  the device map. (`session.py:84`).
- **Device map write not atomic + no lock** — crash mid-write truncates
  JSON → registry reads as empty; concurrent writers clobber.
  (`devices.py:193-222`).
- **restart abrupt SSH drop → `error` (traceback), not the documented
  `timeout` happy path**; switchover success exits 3; `kill_9_ncc_process`
  ungated on MCP. (`restart.py`).
- **ping 100% packet loss → `status: ok` / exit 0** (loss not parsed).
  (`ping.py:121`).
- **confirm refusal exits 2** (== connect_error) while the JSON says
  `status: error` (→1). (`confirm.py`, `app.py` handlers).
- **`status: "warning"` → exit 1**, contract says 0. (`output.py:43`).
- **`detect_error` lowercase `^error \w+ \w+` false-positives** on benign
  lowercase counter lines in show output. (`errors.py:23`).
- **log_read / traces `cat`/`zcat` the whole file on-device before
  filters** — heavy device I/O even for a tail. (`log_read.py:184`,
  `traces.py:502`).
- **Transcript log has no secret scrub** (only backup/techsupport scrub);
  request-log redacts dict keys but not secrets inside `command`/stdout.
  (`logging.py:68`, `runner.py`).
- **jinja generator subprocess isn't actually sandboxed** (only `-I` +
  `HOME` redirect); can read/write arbitrary host files. Document the
  trust model or contain it. (`jinja_store.py:597`).
- **`drain()` truncates slow context-help** (500 ms post-recv cap
  overrides `max_wait`) → partial `cmd_help`. (`shell.py:185`).

## LOW

- Same-second backups can collide on filename → silent overwrite.
  (`backup.py`/`backup_store.py:200`).
- `BACKUP_NEXT_ACTION` / `RESTORE_NEXT_ACTION` still reference dnftp;
  backups now land on the local host. (`errors.py:189`).
- `get_gitcommit` missing-file → `status: ok`. (`gitcommit.py:50`).
- tar-load disk fallback rejects alias-vs-host for the same box.
  (`tarload.py` get fallback).
- edit cleanup warnings cite `configure ; abort` but the code runs
  `rollback 0`. (`edit.py:187`).
- `collapse_progress` drops any line containing `\d+%`, not just progress
  bars. (`shell.py:97`).
- `auto_confirm` can fire on incidental `(yes/no)?` text before the real
  prompt. (`shell.py:847`).
- Transport pool key splits `device` vs `host` for the same box (dup
  sessions). (`session.py:278`).
- Unbounded per-day transcript growth. (`logging.py:42`).

## Suggested grouping into issues/PRs

- **A (high, systemic): "false success" sweep** — restore (#2), edit
  dry-run + load_override + rollback (#3,#4), log_read missing-file (#9),
  ping loss, gitcommit, plus `detect_error` tightening and the
  `warning`/refusal exit-code fixes. One coherent "errors must surface"
  PR (+ tests).
- **B (high): tech-support #17** — port the tar-load fix.
- **C (high): device registry** — fix `device add`, alias
  canonicalization, atomic map write. (`device add` is its own quick win.)
- **D (high): MCP confirm gates** — gate the ungated destructive tools
  (or extend CLI_ONLY) + add `device_lock` to factory-reset/rollback.
- **E (med/sec): hardening** — `job_store` traversal (mine), cross-process
  device locks, idle-reaper vs long step, SSH/SFTP leak + unbounded reads.
- **F (low): polish** — stale next_actions text, warning strings, progress
  collapse, drain budget, transcript scrub/rotation.
