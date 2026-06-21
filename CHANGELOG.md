# Changelog

All notable changes to `qactl` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-06-21

### Changed
- MCP surface: expose tech-support and the read-only / job-poll tools that
  were previously `CLI_ONLY`. Dropped from the carve-out (now reachable over
  `qactl mcp cli` / `qactl mcp nc`): `create_techsupport`,
  `get_techsupport_job`, `list_backups`, `read_backup`, `get_tar_load_job`,
  `request_system_pre_check`, `netconf_list_backups`, `netconf_read_backup`.
  The bar for staying CLI-only is now *interactive* or *writes a large
  config onto the device* — "artifact lands on remote dnftp" is not a reason
  to hide a tool, since that data never enters the local agent context (#4).

### Notes
- Still CLI-only: `setup` (interactive), and the long/destructive device-
  config writers `backup_device`, `restore_device`, `request_system_tar_load`,
  `scale_deploy`, `netconf_backup`, `netconf_restore`.

## [0.3.0] - 2026-06-21

### Added
- **Local stdio MCP front** (`qactl mcp <group>`): serve any group's
  agent-shaped tools to an MCP client (Cursor/Claude) over stdio — no HTTP
  ports, no systemd. Groups: `jira`, `confluence`, `jenkins`, `cli`, `nc`,
  `gnmi`, `rc`, `ixia`, plus `qactl mcp all` for one server exposing every
  group.
- `qactl mcp --list` prints each group's exposed MCP tool surface as JSON.
- Per-group `tools.py` modules (`jira` / `confluence` / `jenkins`) as the
  shared "compute an envelope" layer backing *both* the CLI and MCP fronts,
  so the two stay in lockstep.
- JSONL request logging for the MCP front (`qactl/core/request_log.py`):
  correlated `req`/`resp` lines per tool call with timing and response-size
  telemetry. No secrets are logged (only capped argument reprs). Override the
  log root with `QACTL_MCP_LOG_DIR`.
- `mcp.example.json` — a ready-to-copy `mcp.json` template for MCP clients.
- Destructive MCP tools require a `confirm=true` argument (the MCP equivalent
  of the CLI's `--yes` gate).

### Changed
- Refactored the `jira` / `confluence` / `jenkins` `cli.py` modules to consume
  the new `tools.py` layer instead of holding logic inline.

### Notes
- Heavy `dnftp`/large-artifact tools and `setup` stay **CLI-only** by design
  (backups, tech-support, tar-load, scale-deploy, netconf backup/restore).
- Migrating from the old HTTP MCP servers (ports 8200–8207 under systemd):
  replace each `http://127.0.0.1:820N/mcp` entry with a stdio
  `{"command": "qactl", "args": ["mcp", "<group>"]}` entry, drop the systemd
  units, and move credentials from request headers to the environment.

## [0.2.0]

### Added
- Folded the vendored `dnctl` (DNOS devices: `cli` / `nc` / `gnmi` / `rc` /
  `setup`) and `ixiactl` (`ixia`) trees into `qactl` — one CLI for the whole
  QA workflow. The dispatcher delegates to the bundled entrypoints unchanged,
  preserving their full surface, help, and behaviour.
- `dnctl`: multi-op `gnmi set`, `nc` out-file/restore modes, `rc` alias
  resolution.

### Fixed
- Emit the confirm prompt on stderr with a uniform TTY gate across groups.
- Point Ixia `next_actions` at the `qactl` CLI commands.
- Show `qactl ixia` (not the bare vendored name) in delegated Ixia help.

## [0.1.0]

### Added
- Initial unified, agent-shaped CLI for Jira, Confluence, and Jenkins behind
  one consistent contract (`--json` everywhere, real exit codes, stdin /
  `--file` / inline payloads, a `--yes` confirm gate on destructive ops, and a
  single envelope shape across groups).
- Jenkins: generic `trigger-raw` and queue-item cancel for full MCP parity.

[0.3.0]: https://github.com/oshaboo-dn/qactl/releases/tag/v0.3.0
[0.2.0]: https://github.com/oshaboo-dn/qactl/releases/tag/v0.2.0
[0.1.0]: https://github.com/oshaboo-dn/qactl/releases/tag/v0.1.0
