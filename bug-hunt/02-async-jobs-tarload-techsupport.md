# Area 2 — Async jobs (tar-load / tech-support / slack)

First read `/home/dn/work/qactl/bug-hunt/_context.md` for the rules of
engagement and bug taxonomy. Then hunt this area thoroughly.

This area centres on the #17 bug class. tar-load was just fixed (the CLI
now runs synchronously via `block=True` and persists job state to disk).
**tech-support almost certainly still has the original bug** — verify and
report it concretely.

## Files (read all; follow imports)
- `qactl/cli/core/jobs.py`        (generic in-memory JobRegistry + BaseJob)
- `qactl/cli/core/job_store.py`   (the new on-disk persistence — review for correctness too)
- `qactl/cli/tools/tarload.py`    (already partly fixed — look for *remaining* bugs, not the fixed ones)
- `qactl/cli/tools/techsupport.py`(create_techsupport / get_techsupport_job — does it survive the one-shot CLI?)
- `qactl/cli/core/ts_store.py`    (remote SFTP store)
- `qactl/cli/core/slack_notify.py`

## Focus questions
- tech-support: does `create_techsupport` register an in-memory job +
  daemon thread that dies when the CLI process exits, leaving
  `get_techsupport_job` unable to find it (exactly like #17)? Trace the
  CLI command in `qactl/cli/app.py`. If so, report it as a high-sev bug
  with repro.
- Is the device-slot mutex (`active_for_device`) effective under the CLI
  where the registry is per-process (so it can't actually prevent two
  concurrent CLI invocations)?
- `job_store.py`: atomicity, TTL reaping correctness, device-key matching,
  any way `latest_for_device` returns the wrong job; symlink/traversal via
  job_id in the filename.
- tar-load (remaining bugs only): pre-check poll loop edge cases, the GI
  vs deployed branching, URL validation gaps, the components filter logic,
  Slack notify error paths.
- slack_notify: failures must never break the tool; tokens never logged.
