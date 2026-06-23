# Area 3 — Backup / restore + storage

First read `/home/dn/work/qactl/bug-hunt/_context.md` for the rules of
engagement and bug taxonomy. Then hunt this area thoroughly.

Backup/restore moves config files between the device and a store (local
host and/or dnftp SFTP), then commits restores onto the device — so it's
both destructive and I/O-heavy. Watch for partial-failure-as-success,
path traversal in filenames/buckets, SFTP leaks, and `--yes` gating.

## Files (read all; follow imports)
- `dnctl/cli/tools/backup.py`        (backup_device / restore_device / list_backups / read_backup)
- `dnctl/cli/core/backup_store.py`   (filename grammar, local/SFTP storage, verification)
- `dnctl/cli/core/dnftp.py`
- relevant bits of `dnctl/cli/app.py` (the `cli backup`/`restore` commands — check `--yes` gates)

## Focus questions
- Is `restore_device` (destructive: overwrites running config + commit)
  properly gated by `--yes` AND does it refuse off a TTY without it?
- Filename / bucket / device-prefix handling: any path traversal (`..`,
  absolute paths, `/`) reaching an SFTP path or local path? Can a caller
  forge another device's prefix?
- Are SFTP sessions/transports closed on every error path (stat failure,
  missing file, partial transfer)?
- Restore commit: if `commit` fails or conflicts, is that surfaced as an
  error (non-zero) — or can a failed restore report success?
- `read_backup` / `list_backups`: unbounded reads into the agent context?
  Correct newest-first ordering? Size caps?
- Local-host backup path resolution (recent change): host/user resolved
  per machine — any assumption that breaks on a different host/user?
