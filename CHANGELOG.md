# Changelog

All notable changes to `qactl` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`qactl d42` auto-sources `~/.console_env`.** The Device42 and console-server
  credential resolvers now lazily source `~/.console_env` (setdefault — a value
  already in the environment wins), the same file the legacy `console` tool
  reads. Without this, `qactl d42 …` failed in a normal interactive shell
  (which doesn't export those vars) with "Missing Device42 credentials" / console
  SSH "auth failed", even though the creds were on disk. Best-effort: a missing
  file is silent.
- **`qactl d42` — Device42 CMDB group (native, read-only).** A Device42-backed
  surface so lab inventory/placement/power is read *live* from the CMDB instead
  of stale cached names (motivated by the 2026-07 hostname migration to
  `{Site}{NN}-{ROLE}-{RACK}`, which breaks name-encoded rack/PDU derivation).
  Three commands, each taking a device **name or serial**:
  `qactl d42 device <q>` (curated inventory + owner/`End User` + IPs + raw
  custom fields, via the REST API); `qactl d42 rack <q>` (rack / row / room /
  building / U-position via a single DOQL join); and `qactl d42 power <q>`
  (PDU power feed(s) — PDU name / outlet / model — from the structured
  `view_pduports_v1` relationship, with the lab's outlet-bank normalization).
  All three read placement/power from Device42 fields, never parsed from the
  device name, so they stay correct through the rename (the PDU names already
  come back in the new scheme). New `Device42Config` (env `DEVICE42_ENDPOINT`
  + `DEVICE42_AUTH`, from `~/.console_env`), a thin `Device42Client` (REST GET
  + DOQL POST, TLS-verify off for the self-signed lab cert), the
  `qactl.device42` tool/CLI layers, and tests.
- **`qactl console [<device>] [--server CS --port N]`** — open an interactive
  serial console (a top-level group: the console server + port come from
  Device42 behind the scenes, but reaching a console is a device action, not a
  CMDB read, so it is not under `qactl d42`). With a device name/serial it
  resolves the console server + port from Device42 (parsing the netport cable
  relationship's ``verbose_name``, e.g. ``"Console9 @ console-b08"`` →
  ``CONSOLE-B08`` port 9); with explicit ``--server``/``--port`` it connects
  manually, bypassing Device42 (also the fallback for devices whose Device42
  console field is too free-form to parse). Connect is native: SSH to the
  console server, parse its model-specific "Port Access" menu, select the port,
  then bridge a raw PTY (ported from the proven `console_db` tool). It only
  opens a session on an interactive TTY — with `--json` or off a TTY it resolves
  the server/port and stops. New `ConsoleServerConfig` (`CONSOLE_CS_USER` /
  `CONSOLE_CS_PASSWORD`) and the `qactl.console` package, with tests for the
  lookup/parse paths.
- **`qactl jira comment add <KEY> --text … | --text-file F | --text -`** —
  post a plain-text comment on an issue, rendered to clean ADF (blank line =
  new paragraph, single newline = hard break). Fills the gap where qactl could
  only `comment delete`; posting a comment previously required the MCP or the
  user-paste path. New `JiraClient.add_comment`, `tools.jira_add_comment`, and
  the `_text_to_adf` helper, with tests.

### Fixed
- **`qactl cli capture --mode datapath` no longer blindly defaults to
  `ncp 0`.** When port-mirroring config didn't resolve an NCP the tool
  assumed `ncp 0`, which hard-failed on a cluster (CL) chassis — its line
  cards are `NCP 1`/`NCP 2` and there is no `ncp 0` (`could not enter run
  start shell ncp 0`). Auto-detect now falls back to a *valid* NCP read from
  `show system` inventory: prefer `0` on a standalone, else the lowest present
  NCP (e.g. `1` on a CL), with a warning naming the pick and the present NCPs
  so the user can `--ncp` to the one the capture loop/mirror is on. New pure
  helper `resolve_ncps_from_system()` + tests.
- **`qactl cli restart process` now accepts a bare routing-daemon name.** DNOS
  registers routing-engine processes under namespaces (`routing:bgpd`,
  `routing:fibmgrd`, `infra:sshd`, …) and `request system process restart`
  only accepts the full token — so `restart process ncc 0 routing-engine bgpd`
  was rejected on-box with `ERROR: Unknown word: 'bgpd'`. On execute
  (`confirm=True`) a bare name is now auto-resolved to its unique namespaced
  form by reading the `| Process Name |` column of `show system <role> <id>
  container <container>` (read-only), and a note records the rewrite
  (`bgpd → routing:bgpd`). Already-namespaced, ambiguous, or unknown names are
  passed through unchanged for DNOS to validate. (`kill9 bgpd` was unaffected —
  it uses `pgrep -x` on the Linux binary name.)

### Changed
- **Long-running jobs now Slack-notify by DEFAULT.** `qactl jenkins trigger` /
  `trigger-raw` / `watch` previously only posted with an explicit
  `--notify-slack`; they now default-on (a plain `qactl jenkins trigger` spawns
  the background watcher and DMs build start + finish). Destination is resolved
  by the new `slack_notify.default_channel()`: `$QACTL_NOTIFY_CHANNEL` if set,
  else the built-in `@oshaboo`. Set `QACTL_NOTIFY_CHANNEL=""` as a global
  kill-switch, or pass per-command `--no-notify` to silence one build. An
  explicit `--notify-slack [CHANNEL]` still wins (bare flag = configured
  webhook). `cli tar-load` / `techsupport create` / `tar-load pre-check` kept
  their default `@oshaboo` but now route through the same
  `QACTL_NOTIFY_CHANNEL` toggle. (`monitor tick`/`watch` stay explicit
  opt-in — they post per-event, not per-job, and are `--yes`-gated.)

### Added
- **`qactl cli monitor tick`/`watch` now re-read a `--overlap` window (default
  `10m`) before the cursor**, closing a blind spot where a back-dated event —
  one whose line is merged into the readable log only after the cursor already
  advanced past its timestamp (e.g. a standby-NCC crash surfacing on the active
  NCC minutes late) — was never read and never alerted. The spool's fingerprint
  ring dedupes the re-read lines, so each event still alerts exactly once.
  `--overlap 0` restores the old strict-since-cursor behaviour. (The overlap was
  documented in the spool design all along but never actually applied on
  subsequent ticks.)
- **`qactl spirent bgp send-pdu --device D --hex <PDU>`** — send a raw,
  hand-crafted BGP PDU over an emulated router's session (full message hex,
  16-byte marker included; `-` reads stdin). Builds an STC `BgpCustomPdu`
  (uint8 byte-array) and fires `BgpSendCustomPduCommand`. The negative-testing
  workhorse: fuzz the DUT with byte shapes the object model can't express
  (malformed capabilities/attributes, bad lengths, truncated messages) and
  confirm bgpd survives. Caveat documented in the tool: STC does not suppress
  its own OPEN, so against an already-Established peer the inject resets the TCP
  rather than being parsed as the session's OPEN.

### Fixed
- **`qactl spirent bgp add` left a running device's BFD TX stalled after a
  reconfigure.** An STC config apply re-stages the device's protocol block,
  which silently halts an already-running control-plane-independent BFD
  session's transmission (it stays `Active=true` but stops emitting) until the
  device's protocols are restarted — so reconfiguring a live BFD/strict peer
  left a strict DUT sitting pending on a BFD that never came back Up. `bgp add`
  now bounces the device (`DeviceStop`→`DeviceStart` + ARP kick) after the
  apply when the device is already running and BFD is enabled; fresh,
  not-yet-started devices are left for the later `device start`. Verified live:
  a reconfigure of the established cl↔Spirent strict session auto-recovers BFD
  Up without a manual restart. Tests in `test_spirent.py`.
- **`qactl spirent bgp add --strict` produced a non-conformant BGP-BFD
  strict-mode peer that never negotiated.** The tool advertised Cap-74 as a
  `BgpCustomCapability` with `CapLength=1` (a 1-octet value); a spec-correct DUT
  (DNOS, since SW-276322) rejects any length ≠ 0 with NOTIFICATION 2/0
  ("BFD Strict Mode Capability length error: got 1, expected exactly 0") per
  draft-ietf-idr-bgp-bfd-strict-mode §5. It also omitted the MP address-family
  capability and left STC's BFD BGP-triggered, so even past the length check the
  session would negotiate no AF and/or deadlock against a strict DUT. `--strict`
  now builds the full negotiated recipe: Cap-74 at `CapLength=0` (STC accepts 0
  as long as the `Capability` value is left at default), an MP AFI capability
  (`CustomizedAfi` + `BgpCapabilityConfig`), and a control-plane-independent BFD
  session TXing to the DUT. Verified live: cl↔Spirent reaches Established with
  "BFD Strict Mode Capability: advertised and received", BFD Up. Regression test
  in `test_spirent.py`.
- **`qactl spirent bgp` — 4-byte AS sent a stale 2-byte AsNum in the OPEN.**
  When the local AS was >65535 (e.g. 100001), the tool set `AsNum4Byte` +
  `Enable4ByteAsNum` but left the old 2-byte `AsNum`, so STC put that stale
  value in the OPEN's 16-bit My-AS field alongside the AS4 capability. A DNOS
  peer then rejected the session with NOTIFICATION 2/2 "Bad Peer AS"
  (`myasn 65001 mismatch with remote_as 100001`). `stc_ops.apply_as` now stamps
  `AS_TRANS (23456)` into the 2-byte field for any 4-byte AS (local + DUT).
  Verified live: cl↔Spirent iBGP AS 100001 reaches Established. Regression test
  in `test_spirent.py`.
- **`qactl cli trace <subsystem>` silently read nothing.** A bare
  `qactl cli trace bgp` passed `bgp` as the positional *filename*, so it read a
  nonexistent `/core/traces/routing_engine/bgp` (the live file is `bgpd_traces`)
  and returned an empty result — looking like "no BGP traces exist." The `trace`
  command now promotes a positional name that matches a known subsystem preset
  (`bgp`/`isis`/`zebra`/`fibmgr`/`wb_agent`) to `--target` when `--target`
  wasn't given, so `trace bgp` reads `bgpd_traces` as intended.
- **Persistent-session daemon authenticated devices added after it started
  with the wrong (default) credentials.** `config.load_config` is
  `lru_cache`d for a process's whole lifetime, and the session daemon is
  long-lived, so a device whose per-device creds (`[devices."<name>"]`) were
  written *after* the daemon started was invisible to server-side credential
  resolution — the daemon fell back to the global default account and every
  `qactl cli show/config -d <name>` returned `Authentication failed`, even
  though `device add`/`refresh` (fresh processes) authenticated fine. Fixed
  by resolving credentials **client-side in `_maybe_daemon`**, before routing
  across the socket: the client is a fresh process with current config, so it
  ships the effective creds and the daemon's frozen config no longer matters.
  `resolve_device_credentials` is an idempotent passthrough once the password
  differs from the default, so the daemon's server-side re-resolution is a
  no-op. Mirrors the existing targeted-`DEVICE_HOSTS`-refresh fix for the same
  class of daemon staleness. Regression test in `test_session_daemon.py`.

### Added
- **`qactl spirent` — Spirent TestCenter (STC REST) traffic group (scaffold)**:
  a new sibling of `qactl ixia` that drives a Spirent TestCenter REST server
  (labserver) over the `stcrestclient` package — plain HTTP, no OTG adapter,
  no containers. Mirrors the ixia module layer-for-layer (`client/` low-level
  STC-REST session, `core/` envelope + reattach-first session cache, `tools/`
  ops, `ctl/` argparse front) and the shared contract (`--json`, real exit
  codes, `--yes` confirm gate).
  - Session model is **reattach-first** like ixia: because qactl is
    process-per-invocation, each call joins the named STC session
    (`"<name> - <user>"`, default `qactl-session`) if it exists, else creates
    it; `--new-session` forces a fresh one. Mirrors cheetah's proven
    `dnstc` `connect_to_session`.
  - Commands: `qactl spirent session connect` (reattach probe), `session
    sessions` (list, no join), `session describe` (server/system/BLL snapshot);
    and `qactl spirent port reserve|release|status` — reserve (attach) a
    physical port by location (`//<chassis-ip>/<slot>/<port>`), wait for link
    UP, release, and list session ports with link state. `--force` (RevokeOwner)
    and `release` are confirm-gated. Config via `$SPIRENT_HOST` / `SPIRENT_PORT`
    (80) / `SPIRENT_USER` (dn) / `SPIRENT_SESSION`.
  - `qactl spirent device create|list|start|stop|delete` — build an emulated
    IPv4 device (`Ipv4If → [VlanIf →] EthIIIf` stack via STC `DeviceCreate`,
    bound to a reserved port) with `--ip/--prefix/--gateway/--vlan/--mac/
    --router-id`, then start/stop its protocols.
  - `qactl spirent bgp add|status` — add a BGP router to a device with
    `--local-as/--peer-as` (4-byte ASNs auto-encoded to asdot), peer =
    device gateway (or explicit `--peer`), `--bfd`, and **`--strict`** —
    BGP-BFD strict-mode via custom capability code 74
    (draft-ietf-idr-bgp-bfd-strict-mode; `--strict` implies `--bfd` and
    auto-adds the device's `BfdRouterConfig`). Shared STC helpers (project /
    device lookup, 4-byte-AS split, strict-cap) live in
    `qactl/spirent/client/stc_ops.py`.
  - Live-verified 2026-07-16 against labserver `il-auto-containers` + chassis
    `100.64.3.238` port `6/13`: port attach → LinkStatus UP (100G); emulated
    WAN device `123.4.1.1/24` VLAN 1, iBGP AS 100001 toward `123.4.1.4`, with
    strict-mode + BFD enabled from the CLI.
  - `stcrestclient>=1.9.0` added as a dependency; imported lazily so parser,
    `--help`, and the offline tests need neither the package nor a live
    server. Ports / config-load / traffic / protocol authoring are the
    documented next step (see `qactl/spirent/README.md`), landing once the
    physical Spirent port is cabled.

### Changed
- **`qactl cli --help` groups subcommands under panel headers**: the ~30
  commands now render bucketed under `Reads / state`, `Discovery`, `Logs`,
  `Config`, `Exec / diagnostics`, `Destructive lifecycle`, and `Management`
  (in that order) instead of one flat list. Display-only — invocation is
  unchanged (`qactl cli show`, not `qactl cli reads show`). Implemented with a
  plain-text `_PanelGroup` formatter so the deliberate `rich_markup_mode=None`
  (help text carries literal `[...]`/`<...>` tokens) stays intact.

### Added
- **`qactl jobs` — cross-family async-job list/inspect**: a new native group
  that reads across every job-store namespace (`tarload` / `techsupport` /
  `orc`) so you can see and drill into long-running jobs in one place.
  - `qactl jobs list [--kind K] [--status S] [-d DEV] [--limit N]` — persisted
    jobs newest-first as an aligned table (job_id, family, device, status,
    detail, started, finished); filter by family, status, or device (`total`
    reports the pre-limit match count).
  - `qactl jobs show [job_id] [-d DEV] [--kind K]` — the full persisted
    envelope for one job, looked up by id (searched across families) or the
    newest on a device.
  - Read-only (no `--yes`). A job persisted as still-running whose worker
    process is gone is reported as `error` (died mid-flight), the same orphan
    rule the per-family `show` commands use. New `job_store.list_jobs()` +
    `job_store.JOB_FAMILIES` back the enumeration.
- **`qactl orc` — build/load/pre-check orchestrator**: a new native group that
  chains the existing single-purpose surfaces into one pollable job, with the
  tar-load and the pre-check run as two distinct, ordered phases.
  - `qactl orc load <build-url> -d <dev>` — tar-load an existing build (with
    `pre_check=False`), then run the pre-upgrade pre-check. Blocking by default
    (the load+pre-check is minutes); `--no-wait` detaches it.
  - `-d/--device` is **repeatable** on both `orc load` and `orc build`: one
    build (or one build URL) fans out to every listed device — the Jenkins
    build runs ONCE, then load + pre-check run per device (each device its own
    pollable, device-keyed job), e.g. `qactl orc build dev26.3 --sanitizer
    --baseos -d cl -d sa --yes`. A single device keeps the flat single-job
    envelope; multiple devices return a roll-up (`result.jobs[]`, one row per
    device). One failed device doesn't stop the others.
  - `qactl orc build <branch> -d <dev>` — trigger a cheetah Jenkins build, wait
    for it (`jenkins trigger --wait`), then load its tarballs + pre-check.
    **Detached by default** (a build can run hours): forks a session-detached
    worker and returns a job handle immediately, so an agent shell timeout can't
    take the run down. `--wait` blocks the whole flow inline instead. Common
    build knobs pass through (`--sanitizer`, `--baseos`, `--no-smoke`,
    `--nightly`, `--inherit-from`, `--single-test`, `--extra-params`,
    `--wait-timeout`, `--poll`).
  - `qactl orc show [job_id] [-d <dev>]` — poll a running/finished orc job. The
    driver persists a combined envelope (with per-phase sub-envelopes) to the
    job store after every phase transition, under its own `orc-jobs` namespace;
    a `running` job whose worker process is gone is reported as `error` (died
    mid-flight) rather than `running` forever.
  - Both loading flows are DESTRUCTIVE (`--yes` required). Device work stays
    inside the tar-load / pre-check tools, so their SSH handling, per-device
    guard, GI-detection and evidence journaling carry over unchanged.
- **Per-device custom SSH port**: `qactl cli device add <name> --host <ip>
  --port <N>` stores a `port` on the registry entry, and the transport opener
  uses it (`paramiko connect(port=…)`, previously hardcoded to 22). Lets several
  devices share ONE mgmt IP but differ by port — e.g. cdnos clab nodes fronted
  by per-node DNAT on the host (`h263:2201/2202/2203 → container:22`). `-d`
  reads resolve the stored port automatically; the registration probe honours
  `--port` too. No stored port ⇒ 22 (unchanged for every existing device).
- **Jenkins Slack build updates**: `qactl jenkins trigger` / `trigger-raw`
  take `--notify-slack [CHANNEL]`. qactl posts a Slack update when the build
  STARTS (`#N started`) and when it reaches a terminal state (`SUCCESS` /
  `FAILURE` / timeout), routed through the shared `slack_notify` transport —
  the bare flag uses the configured webhook (`QACTL_SLACK_WEBHOOK_URL`, same
  one the `cli monitor` collector posts to); an explicit `CHANNEL` targets the
  MCP slackbot fallback. Without `--wait` the command **returns immediately
  and hands off to a detached background watcher** (a re-invoked
  `qactl jenkins watch --queue-id …`), so your terminal isn't blocked for the
  whole build; add `--wait` to notify inline instead. Delivery failures are
  best-effort `warnings`, never breaking the build.
- **`qactl jenkins watch <branch>`**: attach to an already-triggered build by
  `--build-number` (running) or `--queue-id` (still queued) and poll it to a
  terminal state, with the same `--notify-slack` option. Read-only — never
  triggers a build. Backs the detached watcher above and is usable standalone
  to monitor a build someone else kicked off.
- **Multi-show batching**: `qactl cli show` / `show-config` accept several
  full quoted commands (`qactl cli show -d cl "show bgp summary" "show
  route summary"`) and run them in order on ONE CLI session — one SSH auth
  for the whole batch. New `show_many` / `show_config_many` tools validate
  every command up front (read-only, same rules as the single tools) and
  return a per-command `steps` transcript plus the joined `stdout`; a
  failing command doesn't skip the rest but still flags `status="error"`.
  Word-form and single-command calls are unchanged (batch triggers only
  when ≥2 args are each a full multi-word `show …` command).
- **Persistent SSH-session daemon**: `qactl cli session on|off|status|stop`.
  Every invocation is a fresh process, so back-to-back qactl calls re-auth
  SSH each time and trip DNOS sshd's connect rate-limit (10/min —
  `TCP_MAXIMUM_CONNECTION_ATTEMPTS_REACHED`, ~100 connect-retries logged in
  5 days). When enabled (`qactl cli session on` marker file, or
  `QACTL_SESSION_DAEMON=1`; `=0` force-disables), the five session ops
  (`run_once` / `run_sequence` / `run_sequence_pw` / `run_probes` /
  `run_ncm_cli`) route over a per-user unix socket (0600) to a small
  auto-spawned daemon that keeps one warm `TransportRegistry` transport per
  `(device, user)` — one SSH auth per device per session instead of per
  command, and each call skips TCP+auth+banner (~2 s). Zero-break: daemon
  unreachable / version-skewed / an unroutable callable arg (unnamed
  `stop_predicate`, `run_capture` drivers) silently falls back to the
  direct in-process path; errors cross the wire typed (`ConnectError`
  transient flag, `UnknownDeviceError`) so envelopes are unchanged. A
  connection that breaks mid-request maps to a transient `ConnectError`
  instead of a silent rerun (the command may have executed). Daemon
  idle-exits after 1 h (`QACTL_SESSION_DAEMON_IDLE`), single-instanced via
  flock, logs to `<state>/cli/session-daemon.log`.
- **Packet capture**: `qactl cli capture` — native, agent-safe packet
  capture on DNOS devices, replacing the external `dn_capture.py` script.
  Two modes: `routing` (control-plane — a `timeout`-bounded `tcpdump` in
  the routing-engine container's `inband_ns`, no device config or physical
  setup; captures BGP/179, BFD, ISIS, ICMP, …) and `datapath` (the NCP
  `wbox-cli` pcap engine with a `/tmp` free-space preflight + size cap;
  documents the loop-cable / mirror-chain lab prerequisite and warns when
  the sink opens but no bytes accrue). Multi-device: `-d cl -d sa` captures
  concurrently, one device-tagged pcap each. The pcap egresses straight to
  *this* host over the existing device→local-sftp path (same endpoint
  `cli backup` uses) — no `zkeiserman-dev` hop, no `~/Downloads/
  dn_devices.json`. `--filter <bpf>` scopes the capture: in `routing` mode
  it is applied **on the device** (trailing tcpdump expression) so the raw
  pcap lands already small — a ~180× reduction vs an unfiltered
  whole-control-plane capture; in `datapath` mode (no device BPF knob) it
  filters locally after download (`tcpdump -r`, sibling `*_filtered.pcap`).
  `--iface <name>` (routing mode) pins the tcpdump interface inside
  `inband_ns` instead of the default `-i any`; `any` double-counts each
  packet across netns legs (a sub-if and its parent), so pinning the sub-if
  (e.g. `g07008.0009` for `ge400-7/0/8.9`) yields exactly one copy per
  packet — what the CPU actually sent/received, no dedupe/editing needed.
  `--json` envelope
  carries per-device `{pcap_path, bytes, ...}`; non-zero exit if any device
  fails. Mutating (writes device `/tmp`; datapath toggles `wbox-cli`) —
  gated by `--yes`. (#86)
- **Core-dump surface**: `qactl cli core list` (parsed `show file core
  list`, wrapped rows merged) and `qactl cli core bt <full-name>` — one
  command from "box restarted" to the exact assert: extracts the bundle
  into a device scratch workdir, reads the crashed binary from the
  bundle's `process.info` (never from the tar name — a core may be named
  after a thread), runs `gdb -batch -ex bt` with debuginfod disabled,
  greps the bundled stderr log for the assert `file:line`, and cleans up
  (`--keep` to retain; `--all-threads` for `thread apply all bt`). v1
  extracts `routing_engine` bundles only; other containers return the
  manual recipe. Mutating (writes device scratch) — gated by `--yes`
  (MCP: `confirm=true`, else dry-run). MCP tools: `list_cores` /
  `get_core_backtrace`. Proven live on OHADZS-NCP1 against the
  SW-279187 bgpd SIGABRT cores. (#65)
- **Always-on per-device daily journal.** Every `qactl cli`/`nc`/`gnmi`/`rc`
  command keyed to a device now tees its full raw output — under a
  `ts | device | cmd | status` header, fenced for markdown — to
  `~/.qactl/device-logs/<device>/<YYYY-MM-DD>.md` (root overridable with
  `QACTL_DEVICE_LOG_DIR`), with no flag required. A whole day's work on a
  device is captured automatically; `--log FILE` remains for hand-picking a
  single evidence file. Best-effort: a write failure degrades silently and
  never breaks a command. CLI-only (the primary front); the MCP server
  keeps its own per-group request log.
- Slack notify now supports a self-contained **webhook** via
  `QACTL_SLACK_WEBHOOK_URL` (falling back to `DIVA_SLACK_WEBHOOK_URL` so a
  single shared webhook serves both tools). Works with a classic incoming
  webhook (`.../services/...`) or a Workflow Builder trigger
  (`.../triggers/...` taking a `text` variable). When set it is the
  preferred transport for `monitor`'s `--notify`, bypassing the MCP
  slackbot (no OAuth / bot-in-channel / per-user email dependency) — the
  right fit for an unattended `monitor watch`. Mirrors the `diva` slack
  adapter: posts `{"text": ...}` and treats any HTTP 2xx as success. The
  webhook posts to its own fixed channel, so the `--notify` channel arg is
  informational when a webhook is configured.
- `ixia bfd create|get|delete` (MCP: `ixia_create_bfdv4_interface` /
  `ixia_get_bfdv4_interface` / `ixia_delete_bfdv4_interface`): manage a
  `bfdv4Interface` on an IPv4 stack — TX/RX intervals, detect
  multiplier, admin state, control-plane-independent, aggregate. `get`
  surfaces live `session_status` / `state_counts` for verdict reads.
- `ixia bgp peer create` gains `--bfd` / `--no-bfd` and `--bfd-mode`
  (`singlehop` | `multihop`) to register the peer for BGP-over-BFD
  (`enableBfdRegistration` / `modeOfBfdOperations`). `ixia bgp peer get`
  and `ixia session describe` now report `bfd_registered` / `bfd_mode`
  per peer and the BFD interfaces under each device group. Unblocks
  bug-verification harnesses whose verdict is BFD session state
  (e.g. SW-279182) (#49).

### Changed
- **Package consolidation (stage 1)**: the vendored `dnctl` package now
  lives under `qactl.dnctl` — one importable package instead of a separate
  top-level tree. All in-repo imports point at `qactl.dnctl`; a thin
  top-level `dnctl` shim aliases the old name so any lingering `import
  dnctl` keeps working (zero-break). Runtime state/config paths
  (`~/.config/dnctl`, `~/.local/state/dnctl`) are unchanged.
- **Package consolidation (stage 2)**: the four top-level `ixia*` packages
  collapsed into one `qactl.ixia` — `ixia` → `qactl.ixia.client`,
  `ixia_core` → `qactl.ixia.core`, `ixia_tools` → `qactl.ixia.tools`,
  `ixiactl` → `qactl.ixia.ctl`. Top-level `ixia`/`ixia_core`/`ixia_tools`/
  `ixiactl` shims alias the old names (zero-break). `qactl ixia …`
  unchanged. Only the top-level `qactl` package remains.
- **Package consolidation (stage 3 — remove old names)**: dropped the
  back-compat shims and renamed the last old-name package `qactl.dnctl` →
  `qactl.dnos`. The runtime state/config also moved off the `dnctl` name:
  `~/.local/state/dnctl` → `~/.local/state/qactl`, `~/.config/dnctl` →
  `~/.config/qactl`, and the `DNCTL_*` env overrides → `QACTL_*`
  (`QACTL_STATE_DIR` / `QACTL_DEVICES` / `QACTL_CONFIG`). Deleted the dead
  standalone-`dnctl`/`ixiactl` deprecation shims. No old package/command/
  env name remains anywhere in the tree.

### Fixed
- **Device added after the session daemon started was unreachable by alias**:
  `qactl cli device add <name> --host <ip> --yes` writes the entry, but the
  long-lived session daemon snapshots `DEVICE_HOSTS` at startup, so a later
  `-d <name>` wrongly failed with "not in the device registry" (while
  `--host <ip>` worked). The daemon's `_execute` now re-reads a device from the
  canonical map on a cache miss (targeted `_refresh_alias_in_cache`, no
  full-reload `clear()` race across handler threads). Surfaced standing up
  cdnos clab nodes on h263.
- **`tar-load --no-wait` detached-worker state clobber**: the detached
  (`detach=True`) kickoff and its forked worker both persist the same
  `job_store` job file, with no ordering between them. The parent wrote its
  `loading` snapshot *after* forking, so if the worker reached a terminal
  state before the parent got there, the parent's stale `loading` write
  clobbered it — and a later `tar-load show` (disk fallback) then saw
  `loading` + a dead worker pid and orphan-flagged the job as `error`. Fixed
  with a fork handshake in `_spawn_detached_worker`: the parent persists the
  pre-worker snapshot and only then (via a pipe EOF) releases the child, so
  every worker write lands strictly after the parent's and can never be
  regressed. Surfaced as the intermittent `test_detach_real_fork_runs_load_to_done`
  full-suite failure; that test now also drives the real fork from a fresh
  single-threaded subprocess (`_fork_detach_runner.py`), mirroring the
  one-shot CLI front, and reads the envelope back over the cross-process
  disk path. Test assertions are unchanged.
- `cli capture --filter`: the local BPF re-write now stages through a `/tmp`
  tempdir instead of running `tcpdump -r/-w` directly on the pcap in the
  `~/.local/state/qactl/captures/…` dir. The stock Ubuntu `tcpdump` AppArmor
  profile (`audit deny @{HOME}/.*/** mrwkl`) denies reading/writing anything
  under a dot-directory in `$HOME`, so `--filter` silently failed with
  "Permission denied" (no `*_filtered.pcap`) on every such host. tcpdump now
  only ever touches `/tmp` (allowed); the result is moved into the captures
  dir with plain Python I/O, which is not AppArmor-confined.
- `jira status` (MCP: `jira_status`) now resolves JSM service-desk portal
  tickets (e.g. `HD-*`). Portal customers lack Browse-Project permission on
  a service desk, so `/rest/api/3/issue/{key}` 404s; on a 404 the client
  now falls back to `/rest/servicedeskapi/request/{key}` and maps its
  `currentStatus` / `summary`. A real auth error (401/403) still surfaces
  unchanged, and a genuinely missing key still 404s. The result carries a
  `source` field (`jira` | `servicedesk`) (#54).

- Registry-backed device commands (`cli` / `interfaces` / config-commit
  and their MCP equivalents) now distinguish "device not in the registry"
  from "registered but unreachable". A registry miss returns a dedicated
  `UnknownDeviceError` (`'<name>' is not in the device registry.`) and a
  next-action hinting `--host <ip/sn>` or `qactl cli device add <name>
  --host <ip/sn>` (MCP: `manage_device operation=add`), instead of the
  misleading "Verify device is reachable and credentials are correct."
  that sent users chasing phantom connectivity problems (#53).

- `ixia rest get --method OPTIONS` no longer raises
  `TypeError: _execute() takes 3 positional arguments but 5 were given`
  — OPTIONS now dispatches through RestPy's generic `_send_recv` (the
  same path `_read` uses) instead of the POST-only `_execute`. Relative
  REST paths (`topology/1/...`) now resolve against the live session's
  `ixnetwork` root rather than the bare server root, so the raw-REST
  schema-discovery fallback works (#49).

- `cli raw` (MCP: `run_raw`): an escape hatch that sends arbitrary CLI
  line(s) verbatim, in order, on ONE ephemeral channel and returns the
  full per-step transcript (`stdout` for humans, `steps` for machines) —
  for odd nested / multi-step flows the structured `show` / `show-config`
  / `config` / `shell` tools don't model. Destructive (can send
  config/commit) so it's gated by `--yes`; aborts on the first errored
  line unless `--continue-on-error`. Surfaces the prompt-detection budget
  as per-call flags `--prompt-timeout` / `--banner-wait` (explicit flag >
  `DNCTL_CLI_PROMPT_TIMEOUT` / `DNCTL_CLI_BANNER_WAIT` env > built-in
  default), threaded additively through `run_sequence` → `_init_channel`
  for slow/odd boxes like DNAAS-LEAF-B13 (#48).

### Fixed
- `cli config` / `cli config --check` no longer report `Commit succeeded`
  when the device silently drops statements mid-batch. DNOS commits
  whatever parsed and still prints success even when a statement was
  rejected — typically a top-level `interfaces ...` / `network-services
  ...` create parsed inside a stale context left by a preceding `no ...`
  delete (`ERROR: Unknown word: 'interfaces'.`). Those per-statement
  errors live in the rejected statement's own step output, invisible to
  the commit parser, so a partial apply masqueraded as success. Both
  paths now scan each statement step and fail (non-zero), naming every
  rejected statement, so a partial running config is surfaced loudly
  instead of buried under a passing commit (#47).

### Added
- `cli raw` (MCP: `run_raw`): escape hatch that sends raw CLI line(s)
  verbatim, in order, on ONE channel and returns the full per-step
  transcript (`stdout` human transcript + structured `steps`). For flows
  the structured `show` / `show-config` / `config` / `shell` tools don't
  model. Gated by `--yes` (can send config/commit). `--continue-on-error`
  runs every line instead of aborting on the first error. The prompt
  detection budget is now tunable per call with `--prompt-timeout` /
  `--banner-wait` (override the `DNCTL_CLI_PROMPT_TIMEOUT` /
  `DNCTL_CLI_BANNER_WAIT` env knobs), threaded through `run_sequence` ->
  `_init_channel`. `options.call` now forwards every non-None kwarg to a
  tool that declares `**kwargs` (previously dropped) (#48).
- `cli techsupport list` (MCP: `list_techsupports`): enumerate
  tech-support bundles on `dnftp` (`dn@dnftp:/ftpdisk/dn/oshaboo/ts/`),
  optionally filtered by `-d <device>`. Reports each bundle's name, size,
  timestamp and path, surfaces non-canonical files under `orphans`, and
  `--json` like every other command. Answers "which tech-support bundles
  do I have for device X?" without an ad-hoc SSH `ls` — the device only
  keeps the single latest bundle, so `dnftp` is the real catalog (#38).

## [0.10.0] - 2026-06-23

### Added
- `cli device add` now captures **where a device physically lives** —
  its rack, mgmt switch, and DNAAS fabric leaf — by reading
  `show lldp neighbors` during the registration probe and decoding the
  rack token (e.g. `B13`) from the mgmt-switch (`IL-SW-B13`) and
  fabric-leaf (`DNAAS-LEAF-B13`) neighbor names. The discovered
  `rack` / `mgmt_switch` / `fabric_leaf` (leaf + port per data link) are
  persisted on the registry entry and surfaced by `cli device list`
  (`rack` / `mgmt_switch` / `leaf`). `--rack <name>` overrides the
  auto-discovered rack; `--no-discover` skips the LLDP probe. Discovery
  is best-effort — a device that doesn't surface usable LLDP still
  registers, with a warning (#40).

## [0.9.1] - 2026-06-23

### Added
- `dnctl setup --check-local-sftp`: a self-check for the local SFTP
  endpoint that `cli backup`/`restore` drive the device to dial back
  into. It resolves `[local]` (host/user/vrf/port) fresh from env/config,
  confirms `[local].password` is set, and TCP-probes that an sshd/SFTP
  server is actually listening at the resolved `host:port` — exiting
  non-zero (and emitting `--json`) when either local precondition fails,
  plus a reminder to verify device→host reachability in the backup VRF.
  Backup/restore preflight `next_action` text now points at it so an
  unconfigured local endpoint surfaces a clear, actionable error instead
  of silently pushing workflows toward dnftp (#34).
- `[local].port` / `DNCTL_LOCAL_SFTP_PORT` / `--local-sftp-port` so the
  endpoint port (default `22`) is configurable.

## [0.9.0] - 2026-06-23

### Added
- `request_system_tar_load` is now exposed over MCP (dropped from
  `CLI_ONLY`). It grew a `confirm` gate mirroring the CLI `--yes`:
  `confirm=false` (default) returns a `status:"dry_run"` envelope without
  fetching Jenkins artifacts or touching the device; `confirm=true` kicks
  off the (fire-and-forget) load and returns the `state:"loading"` +
  `job_id` envelope immediately, so the whole resolve → start → poll
  cycle (`jenkins_artifacts` → `request_system_tar_load` →
  `get_tar_load_job`) is reachable over MCP (#28). The CLI front passes
  `confirm=true` after its own `--yes` gate, so CLI behaviour is
  unchanged. `scale_deploy` stays CLI-only.

## [0.8.1] - 2026-06-21

### Fixed
- `cli ncm-cli` (and the `run_ncm_cli` tool) now answer a nested
  interactive confirm in the NCM (ICOS/StrataX) CLI instead of timing
  out. Commands like `copy running-config startup-config` pause with
  `Do you want to continue? [y/n]:`; the driver watches for the
  `[y/n]:` / `[yes/no]:` confirm and replies with `--answer` (default
  `y`, pass `n` to decline), so the canonical startup-config save
  completes (#22).

## [0.8.0] - 2026-06-21

### Fixed
- `cli tar-load start` / `tar-load pre-check` now run **synchronously**
  on the CLI front and return the terminal envelope (real exit code)
  instead of a `state:"loading"` kickoff with an untrackable `job_id`.
  The async model (in-memory registry + daemon worker) only works inside
  the long-running MCP server; under the one-shot CLI the worker thread
  died when the process exited, aborting the on-device load mid-download
  and leaving `tar-load show` unable to find the job (#17). The terminal
  envelope is also persisted under the state dir so a later
  `tar-load show <job_id>` (or `-d <device>`) resolves it. The MCP front
  is unchanged (new `block=` arg defaults to async).
- tar-load no longer aborts the whole sequence when the device reports
  `file is already registered for download` — that just means the tarball
  is already staged, so the step is marked `already_staged` and the run
  continues to the next component.



### Added
- `run_shell` (and `cli shell`) gained an `ncm` / `--ncm` target so
  `run start shell ncm <A0|B0|...>` is reachable, mirroring the existing
  `ncc` / `ncp` selectors (mutually exclusive with them; no container
  sub-option). Part 1 of #8.
- `run_ncm_cli` tool + `cli ncm-cli` command: drives the NCM management
  switch's own nested (ICOS-style) CLI inside `run start shell ncm <id>`
  — not Linux, not DNOS — running a sequence of NCM commands (e.g.
  `show lldp neighbors`, then `configure` / `interface eth 0/X` /
  `shutdown` | `no shutdown`) and backing out cleanly to DNOS. Handles an
  optional shell password challenge and tracks the exec/config/interface
  prompt shapes; works against a GI-mode chassis. Destructive — gated by
  `--yes`. This automates the cluster-side half of "remove NCP from
  cluster to act as a SA" (closes #8).

## [0.6.0] - 2026-06-21

### Changed
- cli config backups now land on the **local host** instead of `dnftp`.
  The device SFTPs the saved config back to the machine running `dnctl`
  via `request file upload config <fn> <local-user>@<this-host>:<path>`
  (host/user resolved dynamically per machine); `list` / `read` / `restore`
  all operate on the local tree under `<state_dir>/backups/cli`. `dnftp` is
  now reserved for tech-support tarballs. This fixes backups failing when
  no `dnftp` password is configured even though device + SSH creds are fine
  (closes #6).

### Added
- `[local]` config section / `DNCTL_LOCAL_SFTP_*` env vars (host, user,
  password, vrf) for the self-SFTP target the device uploads backups to,
  wired through `dnctl setup` (`--local-sftp-*`). The password is fed to
  the device at the SFTP prompt, mirroring the dnftp flow.
- `dnctl.core.local_sftp` module resolving the dynamic self target;
  `build_upload_command` / `build_download_command` now take optional
  `user` / `host` (defaulting to dnftp, so tech-support is unchanged).

## [0.5.0] - 2026-06-21

### Changed
- MCP surface: expose device + NETCONF **backup/restore** over MCP
  (`backup_device`, `restore_device`, `netconf_backup`, `netconf_restore`).
  Backups are non-destructive; restores execute only with `confirm=true`
  (`confirm=false` returns a dry-run). The CLI-only bar is now *interactive*
  or *destructive without a confirm gate*; the only tools left CLI-only are
  `setup`, `request_system_tar_load`, and `scale_deploy`.
- Single device credential: removed the separate NETCONF account and the
  auth-failure fallback. Every protocol surface (SSH / NETCONF / gNMI /
  RESTCONF) now authenticates with the one `DNCTL_USER` / `DNCTL_PASSWORD`
  pair (default `dnroot` / `dnroot`) plus an optional `DNCTL_SSH_KEY`.
  `qactl setup` no longer prompts for NETCONF user/password, and the
  `[netconf]` config table / `DNCTL_NETCONF_*` env vars are gone.

### Removed
- `dnctl.core.credentials.NETCONF_USER` / `NETCONF_PASSWORD` /
  `PROTOCOL_FALLBACK` (and the `core.auth` re-exports); the gNMI
  `FALLBACK_CREDENTIALS` constant; the NETCONF session credential-fallback
  retry; `ConnectResult.fallback_used`.

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
