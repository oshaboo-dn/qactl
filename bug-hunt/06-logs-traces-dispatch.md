# Area 6 — Log/trace reads + dispatch + envelope/errors/logging

First read `/home/dn/work/qactl/bug-hunt/_context.md` for the rules of
engagement and bug taxonomy. Then hunt this area thoroughly.

This area covers large file reads from devices (accounting/system-events/
traces), the CLI dispatch + option glue, and the shared envelope / error
classification / request+transcript logging (which must never leak
secrets).

## Files (read all; follow imports)
- `qactl/cli/tools/log_read.py`   (get_accounting / get_netconf_accounting / get_system_events)
- `qactl/cli/tools/traces.py`     (list_traces / get_trace)
- `qactl/cli/core/log_filters.py`
- `qactl/cli/app.py`              (Typer dispatch, every command's flags/gates)
- `qactl/core/options.py`         (build_ctx / call / finish — the kwarg-filtering glue)
- `qactl/cli/core/envelope.py`
- `qactl/cli/core/errors.py`      (detect_error patterns — false positives/negatives)
- `qactl/cli/core/logging.py`     (request log + per-device transcript)
- `qactl/cli/core/redact.py`

## Focus questions
- `errors.detect_error`: do the regexes false-positive on legitimate show
  output (e.g. lines containing "error" counters) or miss real errors?
  Case-sensitivity assumptions correct?
- log_read / traces: unbounded reads into the agent context? Are
  tail_lines / since / until / grep filters applied on-device or only
  after pulling the whole file? Path/filename validation for `get_trace`
  (traversal)? SFTP/exec session leaks.
- `options.call`: it drops `None` kwargs and filters to the fn signature —
  any case where a falsy-but-meaningful value (0, "", False) is wrongly
  dropped, or a needed kwarg silently not passed?
- `finish` / exit-code mapping: does every error envelope yield non-zero
  and every ok/warning yield 0? Any command that bypasses the gate?
- logging/redact: are passwords/tokens redacted in BOTH the request log
  and the per-device transcript? Any field that leaks a secret? Unbounded
  transcript growth?
- app.py: scan EVERY destructive command for a missing `--yes` /
  `confirm.ensure` gate (compare against backup/restore/tar-load/edit).
