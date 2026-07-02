# qactl

One **agent-shaped** tool for an entire QA workflow — DNOS devices,
IxNetwork traffic generation, Jira, Confluence, and Jenkins — behind a
single consistent contract, exposed over **two interchangeable fronts**:

- a **CLI** (`qactl <group> <cmd> --json`) for shells, scripting, and CI, and
- a **local stdio MCP server** (`qactl mcp <group>`) for AI agents.

Both fronts drive the *same* shared tool layer, so they stay in lockstep.
It collapses a fleet of MCP servers and scattered helper scripts into one
executable.

| Group | Domain | Source |
|---|---|---|
| `cli` / `nc` / `gnmi` / `rc` / `setup` | DNOS devices (SSH / NETCONF / gNMI / RESTCONF) | vendored `dnctl` |
| `ixia` | IxNetwork sessions / topology / protocols / traffic | vendored `ixiactl` |
| `jira` | Jira watchers / attachments / comments / transitions / status | native |
| `confluence` | Confluence comments / attachments | native |
| `jenkins` | Jenkins builds: trigger / inspect / stop | native |
| `arista` | Arista EOS switches: interfaces / lldp / config / version (read-only, eAPI) | native |

`qactl` is a thin dispatcher: the `cli/nc/gnmi/rc/setup` and `ixia`
groups delegate to the bundled `dnctl` / `ixiactl` entrypoints unchanged
(full surface, help, and behaviour preserved), while `jira` /
`confluence` / `jenkins` / `arista` are implemented natively. All groups share the
same contract, and the same envelope-returning tool functions back both
the CLI and the MCP front.

## MCP front (stdio)

```bash
qactl mcp jira          # serve the jira tools over stdio
qactl mcp all           # one server exposing every group
qactl mcp --list        # print each group's exposed MCP tools (JSON)
qactl mcp --help
```

Register it in your MCP client (Cursor/Claude) — stdio, no HTTP ports, no
systemd. See [`mcp.example.json`](mcp.example.json):

```json
{
  "mcpServers": {
    "qactl-jira":    { "command": "qactl", "args": ["mcp", "jira"] },
    "qactl-jenkins": { "command": "qactl", "args": ["mcp", "jenkins"] },
    "qactl-cli":     { "command": "qactl", "args": ["mcp", "cli"] }
  }
}
```

Because the server runs **locally over stdio** (same host as the agent),
tools that touch local files behave exactly as they do under the CLI, and
credentials resolve from the **environment** the client launches `qactl`
with (`ATLASSIAN_*`, `JENKINS_*`, device creds via `qactl setup`) — there
are no per-request HTTP headers. Destructive MCP tools require a
`confirm=true` argument (the MCP equivalent of the CLI's `--yes`).

### What's on MCP vs CLI-only

Agent-driven surfaces are exposed over MCP: all of `jira`, `confluence`,
`jenkins`, `arista`, `gnmi`, `rc`, `ixia`, plus nearly all of `cli` and `nc`. This
includes tech-support (`create_techsupport` is fire-and-forget — the `.tar`
lands on remote `dnftp`, never locally), the cheap read-only / job-poll
tools, and device + NETCONF **backup/restore** (backups are
non-destructive; restores execute only with `confirm=true`, otherwise they
return a dry-run). Config backups land on the **local host** (the device
SFTPs them back to the machine running `dnctl`); `dnftp` is reserved for
the big tech-support tarballs. A tool stays **CLI-only** only when it is *interactive*
or *destructive without a confirm gate*:

- `setup` (one-time device registry / credentials — interactive)
- `cli`: `scale_deploy` (long deploy op that mutates the box and doesn't
  yet take a `confirm` argument)

Run those as `qactl cli ... --yes` / `qactl setup`. Everything else —
including `backup_device` / `restore_device` / `netconf_backup` /
`netconf_restore`, the backup read side, the tech-support / tar-load job
lookups, `request_system_tar_load` (fire-and-forget kickoff, gated by
`confirm=true`), and `request_system_pre_check` — is on MCP.

> Migrating from the old HTTP MCP servers (ports 8200–8207 under systemd):
> replace each `http://127.0.0.1:820N/mcp` URL entry with a stdio
> `{"command": "qactl", "args": ["mcp", "<group>"]}` entry, drop the
> systemd units, and move credentials from request headers to the
> environment.

## The contract

1. **`--json` everywhere.** Default is readable text; `--json` emits the
   exact envelope so you can pipe to `jq`.
2. **Real exit codes.** Non-zero on any error, so `&&` chains and CI work.
3. **stdin / `--file` / inline** for any text payload argument.
4. **`--yes` confirm gate** on every destructive op (deletes, watcher
   removal, transitions, build trigger/stop). Refuses off a TTY without it.
5. **No secrets in the repo.** Credentials resolve at runtime from the
   environment — see below.

Every command prints one envelope:

```json
{ "status": "ok", "kind": "jira_status", "result": { ... },
  "warnings": [], "errors": [], "next_actions": [] }
```

## Install

```bash
pipx install git+ssh://git@github.com/oshaboo-dn/qactl.git
qactl --help
```

or for development:

```bash
git clone git@github.com:oshaboo-dn/qactl.git && cd qactl
pip install -e ".[dev]"
pytest -q
```

## Credentials (local to you, never committed)

**Atlassian — one token covers both Jira and Confluence** (same site):

```bash
export ATLASSIAN_EMAIL="you@drivenets.com"
export ATLASSIAN_API_TOKEN="ATATT3x..."          # id.atlassian.com
export ATLASSIAN_BASE_URL="https://drivenets.atlassian.net"   # optional
```

**Jenkins:**

```bash
export JENKINS_USER="your-jenkins-id"
export JENKINS_API_TOKEN="<token>"
export JENKINS_URL="https://jenkins.dev.drivenets.net"        # optional
```

**Arista EOS** (optional — defaults to `admin` with an empty password):

```bash
export ARISTA_USER="admin"
export ARISTA_PASSWORD="<password>"
```

Any of these can be overridden per-command (`--email`/`--token`/`--base-url`
for Atlassian; `--user`/`--token`/`--url` for Jenkins; `--user`/`--password`
for Arista) but the environment is the default. The repo ships no `.env`
and no baked-in tokens.

## Subcommands

### `qactl jira`

| Command | Description | Gate |
|---|---|---|
| `whoami` | resolve the token to a Jira user | |
| `status <issue>` | issue status + summary (falls back to the JSM service-desk API on a 404, so portal `HD-*` tickets resolve) | |
| `watchers list <issue>` | list watchers | |
| `watchers add <issue> <account_id>` | add a watcher | |
| `watchers remove <issue> <account_id>` | remove a watcher | `--yes` |
| `attachments list <issue>` | list attachments | |
| `attachments upload <issue> <file> [--name N]` | upload a file | |
| `attachments delete <id>` | delete an attachment | `--yes` |
| `comment delete <issue> <comment_id>` | delete a comment | `--yes` |
| `transitions list <issue>` | valid workflow transitions now | |
| `transitions do <issue> <transition_id>` | apply a transition (validated) | `--yes` |

### `qactl confluence`

| Command | Description | Gate |
|---|---|---|
| `comment <page> [--text T \| --text-file F \| --text -] [--attach F]` | post a comment (body inline, from a file, or from stdin), optionally attaching+embedding a file | |
| `list <page>` | list a page's comments + attachments | |
| `delete <id>` | delete a comment or attachment by id | `--yes` |

### `qactl jenkins`

| Command | Description | Gate |
|---|---|---|
| `whoami` | sanity-check the Jenkins token | |
| `trigger <branch> [...flags] [--wait]` | trigger a cheetah build; `--wait` blocks until it finishes | `--yes` |
| `trigger-raw <job_path> [--param K=V]... [--extra-params JSON] [--wait]` | trigger any parameterized job by path with raw params (non-cheetah) | `--yes` |
| `info <branch> [build]` | build details (params, result, causes) | |
| `console <branch> [build] [--tail N]` | tail the console log | |
| `artifacts <branch> [build] [--all]` | a build's published download links (baseos / GI / dnos / cdnos tarballs + registry image refs) | |
| `list <branch> [--limit N]` | recent builds | |
| `stop <branch> --build-number N` / `stop --queue-id N` | abort a running build, or cancel a queued one | `--yes` |

> The MCP's `get_jenkins_build_job` (in-memory async job registry) has no CLI
> equivalent by design — a CLI is process-per-invocation, so build state is read
> live from Jenkins via `info` / `list` (or `trigger --wait`).

Cheetah trigger knobs map to Jenkins parameters: `--sanitizer`
(`TEST_NAMES=ENABLE_SANITIZER`), `--baseos`, `--no-lint`, `--no-dnos`,
`--no-tarballs`, `--no-smoke`, `--delta-build`, `--single-test*`,
`--nightly`, `--qa-version`, `--inherit-from <build#>`, `--extra-params
'<json>'`. Branch slashes (`feature/foo`) are URL-encoded for you.

### `qactl arista`

Read-only queries against Arista EOS switches over eAPI (JSON-RPC over
HTTPS; enable with `management api http-commands` on the switch). Host is
positional; credentials default to `$ARISTA_USER` / `$ARISTA_PASSWORD`
(`--port` / `--http` select the eAPI endpoint).

| Command | Description | Gate |
|---|---|---|
| `interfaces <host>` | `show interfaces status` + derived `free_candidates` (link notconnect/disabled) | |
| `lldp <host>` | LLDP neighbors — map local ports to fabric/DUT peers | |
| `config <host> [--interface IFACE]...` | running config: whole box, or per-interface sections | |
| `version <host>` | model / EOS version / serial — the connectivity sanity check | |

The free-port workflow: `interfaces` proposes `free_candidates`, then
`lldp` cross-checks that a candidate has no neighbor before you cable it.
Config apply (with `--check` / `--yes` gating) is future work — this
group is deliberately read-only for now.

## Acceptance smoke test

```bash
# Atlassian
qactl jira whoami --json | jq .result.email
qactl jira status SW-264282 --json | jq .result.status
qactl jira transitions list SW-264282 --json
qactl jira comment delete SW-264282 999            # must REFUSE (no --yes)

# Confluence
qactl confluence list <page-id> --json | jq '.result.comments'

# Jenkins
qactl jenkins whoami --json | jq .result
qactl jenkins list feature/foo --json | jq '.result.builds'
qactl jenkins trigger feature/foo                  # must REFUSE (no --yes)
qactl jenkins trigger feature/foo --yes            # queues the build

# Arista (read-only)
qactl arista version arista410 --json | jq .result.version
qactl arista interfaces arista410 --json | jq .result.free_candidates
qactl arista lldp arista410 --json | jq '.result.neighbors'
```

## DNOS devices & Ixia

These groups are the bundled `dnctl` / `ixiactl` verbatim — same flags,
same behaviour, same `--json`/`--yes` contract:

```bash
qactl setup ...                       # one-time device registry / creds (dnctl)
qactl cli system -d sa --json
qactl cli interfaces -d cl --json    # aggregated per-iface: state+desc+LLDP+IGP
qactl cli show -d cl 'show bgp summary' --log run.md   # tee raw output → QA evidence
qactl cli config -d cl --compare 'protocols bgp neighbor 1.1.1.1 peer-as 65001'  # candidate diff, no commit
qactl cli shell -d cl 'grep -lE libasan /proc/[0-9]*/maps'   # read-only shell exec, no --yes
cat filter.xml | qactl nc get -d sa - --json
qactl gnmi get -d cl /interfaces --json
qactl ixia session connect --host 10.0.0.5 --json   # IXIA_HOST also honoured
qactl ixia traffic stats --host 10.0.0.5 --json
```

Device credentials/registry come from `qactl setup` (dnctl's resolver);
Ixia honours `IXIA_HOST` / `IXIA_USER` / `IXIA_PORT`. See
`qactl <group> --help` for the full surface.

### Per-device daily journal

Every `qactl cli`/`nc`/`gnmi`/`rc` command keyed to a device also tees its
full raw output — with a `ts | device | cmd | status` header — to an
always-on journal, no flag needed:

```
~/.qactl/device-logs/<device>/<YYYY-MM-DD>.md   # override root with QACTL_DEVICE_LOG_DIR
```

So a day's work on any device is captured automatically (`--log FILE`
remains for hand-picking a single evidence file). The journal is
best-effort: a write failure degrades silently and never breaks a command.
It's the CLI front that journals — the CLI is the primary, human-and-agent
surface; the MCP front keeps its own per-group request log.

## Layout

```
qactl/
  qactl/
    core/        envelope, output/exit-codes, creds (env), request_log, CLI plumbing
    jira/        client.py (REST) + tools.py (envelopes) + cli.py  -> qactl jira ...
    confluence/  client.py (REST) + tools.py (envelopes) + cli.py  -> qactl confluence ...
    jenkins/     client.py (REST) + tools.py (envelopes) + cli.py  -> qactl jenkins ...
    arista/      client.py (eAPI) + tools.py (envelopes) + cli.py  -> qactl arista ...
    mcp/         registry.py (group->tool surface map) + server.py -> qactl mcp ...
    __main__.py  dispatcher: native groups, mcp front, delegation to dnctl / ixiactl
  dnctl/         vendored DNOS device CLI  -> qactl cli/nc/gnmi/rc/setup (+ MCP tools)
  ixiactl/ ixia/ ixia_core/ ixia_tools/    vendored Ixia CLI -> qactl ixia (+ MCP tools)
  mcp.example.json   stdio mcp.json template for MCP clients
tests/           native CLI tests + MCP front tests + vendored dnctl / ixiactl suites
```

The `tools.py` layer is the shared "compute an envelope" boundary: each
function returns the one envelope dict, the CLI prints it via `emit()`,
and the MCP front serializes it as the tool result. The vendored
`dnctl/*/tools/*` and `ixia_tools/*` already are this layer (they kept
their `register(mcp)` hooks), so the MCP front re-exposes them directly.

## Changelog

Notable changes per release are in [`CHANGELOG.md`](CHANGELOG.md). The
current version is reported by `qactl --version`.
