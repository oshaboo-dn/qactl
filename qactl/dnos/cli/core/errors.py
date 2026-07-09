"""DNOS CLI error detection + next_action recommendations."""

from __future__ import annotations

import re
from typing import List, Tuple


_ERR_PATTERNS = [
    re.compile(r"^%\s+", re.MULTILINE),
    re.compile(r"(?i)\bunknown command\b"),
    re.compile(r"(?i)\binvalid input\b"),
    re.compile(r"(?i)\bsyntax error\b"),
    re.compile(r"(?i)\bincomplete command\b"),
    re.compile(r"(?i)\bambiguous command\b"),
    re.compile(r"(?i)\bno such command\b"),
    re.compile(r"(?i)^Error:", re.MULTILINE),
    # Lowercase runtime/operation errors emitted by DNOS daemons, e.g.
    # ``error downloading package`` from a refused ``request system
    # target-stack load``. Case-sensitive on purpose: title-case lines
    # like ``Error counters received`` in show output would otherwise
    # false-positive.
    re.compile(r"(?m)^error\s+\w+\s+\w+"),
    re.compile(r"(?i)\bupgrade in progress\b"),
]


def detect_error(output: str) -> Tuple[bool, List[str]]:
    """Return (is_error, error_lines). Error lines are the lines that matched."""
    if not output:
        return False, []
    hits: List[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for rx in _ERR_PATTERNS:
            if rx.search(stripped):
                hits.append(stripped)
                break
    return bool(hits), hits


_INCOMPLETE_RX = re.compile(r"(?i)\bincomplete command\b")


def is_incomplete_command(err_lines: List[str]) -> bool:
    """True if any error line is the DNOS ``Incomplete command`` message.

    DNOS emits this when the typed path needs one more required token at
    the current level — e.g. ``show config protocols bgp`` (the BGP
    container is keyed by AS-number, so the path is incomplete without
    one). Different recovery from a generic syntax error: don't restart
    discovery from scratch, just resolve the missing identifier.
    """
    return any(_INCOMPLETE_RX.search(line) for line in err_lines)


SHOW_NEXT_ACTION = (
    "Run `qactl cli search show <keywords> -d <device>` with short "
    "keywords from your request to discover the correct show syntax, then "
    "run it with `qactl cli show '<command>' -d <device>`."
)
SHOW_CONFIG_NEXT_ACTION = (
    "Run `qactl cli search show_config <keywords> -d <device>` with short "
    "keywords from your request to discover the correct show-config "
    "syntax, then run it with `qactl cli show-config '<command>' -d <device>`."
)
SHOW_CONFIG_INCOMPLETE_NEXT_ACTION = (
    "DNOS reported 'Incomplete command' — the path needs one more required "
    "token at this level (typically an instance identifier the device picks "
    "at runtime: BGP AS-number, ISIS instance name, VRF name, neighbor "
    "address, ...). Two ways to resolve without guessing: "
    "(a) `qactl cli crawl --config '<path without the leading \"show config\">'` "
    "to enumerate the expected next-token children; or "
    "(b) step UP one level and read the parent — "
    "`qactl cli show-config 'show config <parent>'` prints every configured "
    "child with its identifier in-line. For a focused view, pipe "
    "the parent through `| flatten | include <child>` to get one-line "
    "set-style output filtered to just that subtree (e.g. "
    "`qactl cli show-config 'show config protocols | flatten | include bgp'` "
    "surfaces 'protocols bgp 100001 ...' so you can re-run with the "
    "discovered AS-number appended). Other DNOS show-config pipes that "
    "help narrow output: `| include`, `| exclude`, `| find`, `| flatten`, "
    "`| count`, `| display-inherited`, `| display-xml`, `| tail`."
)
CMD_HELP_NEXT_ACTION = (
    "Run `qactl cli search <scope> <keywords>` (scope: show | show_config | "
    "configure | clear | request | run | set | unset | all-commands) first "
    "to find the exact command, then get its full help with "
    "`qactl cli help '<command>' -d <device>`."
)
CLEAR_NEXT_ACTION = (
    "Walk the clear subtree with `qactl cli crawl 'clear ...'` to pin the "
    "exact syntax (e.g. `qactl cli crawl 'clear bgp neighbor'` to see the "
    "accepted neighbor selectors, `qactl cli crawl 'clear arp'` for the "
    "interface/vrf variants). The clear tree is operational state, not "
    "config — there is no commit/rollback, so verify the target (peer, "
    "interface, VRF, MAC) before re-trying."
)
RUN_PING_NEXT_ACTION = (
    "Verify the destination is reachable, and that the vrf / "
    "source-interface (if any) exist on the device."
)
RUN_SHELL_NEXT_ACTION = (
    "`qactl cli shell <cmd> ... --yes` runs arbitrary Linux command(s) "
    "inside 'run start shell' on the device (active NCC default container "
    "unless you target another context). Check (1) each command is valid "
    "for the device's Linux userland and the joined line is shell-safe; "
    "(2) --ncc is '0'/'1'/'active', --ncp is 0..191 or 'bfd-master', and "
    "--ncc/--ncp are not combined; (3) --container (when set) names a real "
    "container on that NCC (`qactl cli system`). Commands are chained with "
    "'&&' (stop on first failure) unless --continue-on-error chains them "
    "with ';' instead."
)
RUN_RAW_NEXT_ACTION = (
    "`qactl cli raw <line> ... --yes` sends raw CLI line(s) verbatim, in "
    "order, on ONE channel and returns the full per-step transcript — an "
    "escape hatch for flows the structured tools don't cover. Check (1) "
    "each line is a valid DNOS CLI line for the mode it runs in "
    "(configure-mode lines must be preceded by a 'configure' line in the "
    "same call); (2) the sequence aborts on the first errored line unless "
    "--continue-on-error is set; (3) for a slow/odd prompt, widen the "
    "detection budget with --prompt-timeout / --banner-wait. Prefer the "
    "purpose-built `qactl cli show` / `qactl cli show-config` / "
    "`qactl cli config` / `qactl cli shell` when they fit."
)
RUN_PROBE_NEXT_ACTION = (
    "`qactl cli probe '<prefix>' [--key '?'|tab] [--config]` types the "
    "prefix WITHOUT Enter, injects one keystroke and returns what the CLI "
    "painted (context help for '?', the completed line buffer for tab) — "
    "read-only by construction: the line is wiped with Ctrl-U, never "
    "submitted. Check (1) the prefix is valid for the mode probed "
    "(--config probes the configure-mode grammar); (2) trailing-space "
    "semantics: '... bfd ' enumerates children, '... bfd str' acts on the "
    "partial token — quote the prefix so the shell preserves the trailing "
    "space; (3) each argument is one probe, all on ONE channel. For plain "
    "child enumeration prefer `qactl cli crawl` / `qactl cli help`."
)
RUN_NCM_CLI_NEXT_ACTION = (
    "`qactl cli ncm-cli <cmd> ... --ncm <id> --yes` drives the NCM "
    "management switch's own (ICOS-style) nested "
    "CLI inside 'run start shell ncm <id>' — not Linux, not DNOS. Check "
    "(1) --ncm names a real NCM ('A0' / 'B0' / ...); (2) each command is valid "
    "NCM CLI, e.g. 'show lldp neighbors' to map ctrl-ncp-<id>/0 to eth 0/X, "
    "then 'configure' / 'interface eth 0/X' / 'shutdown' (or 'no shutdown') "
    "to toggle a port; (3) config-mode commands are ordered so each runs in "
    "the mode the previous one entered. The session always backs out via "
    "'end' + 'exit' to DNOS, even on error. Works on a GI-mode chassis."
)
CORE_LIST_NEXT_ACTION = (
    "`qactl cli core list -d <dev>` wraps `show file core list`. Verify the "
    "device is reachable and the password is correct; an empty `cores` list "
    "on status ok means the box simply has no core dumps."
)
CORE_BT_NEXT_ACTION = (
    "`qactl cli core bt <full-name> -d <dev> --yes` extracts a core bundle "
    "and runs gdb on the device. Check (1) <full-name> is exactly as "
    "printed by `qactl cli core list` (<container>/core-....tar); (2) the "
    "bundle still exists under /core/core_dumps/containers/; (3) rerun "
    "with --keep and inspect the workdir manually if a step keeps failing."
)
CORE_BT_UNSUPPORTED_NEXT_ACTION = (
    "Only routing_engine cores are supported so far (bundle layouts differ "
    "per container). Manual recipe for other containers: tar -xf the bundle "
    "from /core/core_dumps/containers/<container>/ into a workdir, lz4 -d "
    "the core, read the crashed binary from process.info.<cpid> "
    "(fullpath= line), then run `gdb -batch -iex 'set debuginfod enabled "
    "off' -ex bt <binary> <core>` inside the matching container via "
    "`qactl cli shell --container <name>`."
)
KILL_NCC_NEXT_ACTION = (
    "Check the daemon name is one of bgpd/zebra/fibmgrd and that the "
    "device password is correct; rerun to confirm the daemon was "
    "respawned by the NCC process supervisor."
)
GET_GITCOMMIT_NEXT_ACTION = (
    "Verify the active NCC is reachable and the device password is correct. "
    "If stdout says 'No such file or directory', /.gitcommit is missing on "
    "this build — the DNOS image layout may have shifted."
)
GET_ACCT_NEXT_ACTION = (
    "Verify the active NCC is reachable and the device password is correct; "
    "narrow the window with --tail / --since / --until / --grep if the file is "
    "large or the result is truncated. If stdout says 'log file not found' "
    "the on-disk layout may have shifted — extend _CLI_ACCOUNTING_PATHS in "
    "the log_read tool with the new path."
)
GET_NETCONF_ACCT_NEXT_ACTION = (
    "Verify the active NCC is reachable and the device password is correct; "
    "narrow the window with --tail / --since / --until / --grep if the file is "
    "large or the result is truncated. If stdout says 'log file not found' "
    "the on-disk layout may have shifted — extend "
    "_NETCONF_ACCOUNTING_PATHS in the log_read tool with the new path."
)
GET_SYSTEM_EVENTS_NEXT_ACTION = (
    "Verify the active NCC is reachable and the device password is correct; "
    "narrow the window with --tail / --since / --until / --grep if the file is "
    "large or the result is truncated. Timestamps in system-events.log are "
    "in field 2, not 1 — the tool already handles that. If stdout says "
    "'log file not found' extend _SYSTEM_EVENTS_PATHS in the log_read tool."
)
LIST_TRACES_NEXT_ACTION = (
    "Verify the target (ncc/ncp/container) exists, and narrow with "
    "--component if the directory is large. Use `qactl cli traces` first; "
    "then feed a filename to `qactl cli trace <name>`. NCC traces live in "
    "/core/traces/routing_engine/; NCP traces in per-subsystem dirs under "
    "/core/traces/ (datapath, dnos-agent, ...) — `traces --ncp <id>` lists "
    "them with subdir-relative names."
)
GET_TRACE_NEXT_ACTION = (
    "Verify the name matches a file listed by `qactl cli traces` (same "
    "ncc/ncp/container target). Narrow with --tail / --since / --until / "
    "--grep if the file is large or the result is truncated."
)
REQUEST_RESTART_NEXT_ACTION = (
    "Re-check the restart grammar with "
    "`qactl cli crawl 'request system restart ...'` or `qactl cli help`; "
    "verify the node_role / node_id exist on this device "
    "(`qactl cli system`), and that mode/warm/force are valid for that role."
)
REQUEST_NCC_SWITCHOVER_NEXT_ACTION = (
    "Re-check the grammar with "
    "`qactl cli crawl 'request system ncc switchover'` or `qactl cli help`; "
    "verify this is a dual-NCC chassis and the standby NCC is online "
    "(`qactl cli show 'show system ncc'`) before retrying. The SSH "
    "session dropping mid-command (status='timeout') is the expected happy "
    "path — the active NCC just changed; rerun `qactl cli show 'show system "
    "ncc'` on a fresh call to confirm the new active."
)
REQUEST_TAR_LOAD_NEXT_ACTION = (
    "Verify (1) jenkins_url is a real cheetah build that exposes the "
    "gi_DNOS_artifact.txt / gi_GI_artifact.txt files (gi_base_os_artifact.txt "
    "is optional), (2) the device can reach the minio host the artifacts "
    "point at (`qactl cli ping <minio-host> --vrf mgmt0 -d <device>`), and "
    "(3) no other tar load / pre-check is already running. On a per-step "
    "DNOS error, subsequent loads and pre-check were skipped; re-run after "
    "fixing the failing step's URL or freeing the disk."
)
BACKUP_NEXT_ACTION = (
    "Config backups land on THIS host (the machine running qactl), not "
    "dnftp — the device SFTPs the saved config back to us. Verify (1) the "
    "device can reach this host in the backup VRF "
    "(`qactl cli ping <QACTL_LOCAL_SFTP_HOST> --vrf mgmt0 -d <device>`), (2) "
    "QACTL_LOCAL_SFTP_PASSWORD (or [local].password) is set — the device "
    "authenticates to our sshd with it at the SFTP prompt; run "
    "`qactl setup` to write the config, (3) this host's sshd accepts the "
    "QACTL_LOCAL_SFTP_USER account, and (4) the local backup root is "
    "writable. Run `qactl setup --check-local-sftp` to verify (2)+(3) "
    "before retrying. (dnftp is only used for the large tech-support "
    "tarballs.)"
)
RESTORE_NEXT_ACTION = (
    "Backups live on THIS host (the machine running qactl), not dnftp — "
    "the device pulls the file back from us. Check (1) "
    "`qactl cli backup list -d <device>` to confirm the file exists and its "
    "device prefix matches, (2) if the file lives in a sub-bucket pass the "
    "same --bucket you used at backup time, (3) the device can reach this "
    "host in the backup VRF "
    "(`qactl cli ping <QACTL_LOCAL_SFTP_HOST> --vrf mgmt0 -d <device>`) and "
    "QACTL_LOCAL_SFTP_PASSWORD is set (run `qactl setup --check-local-sftp` "
    "to verify the endpoint is configured and listening), and (4) commit "
    "did not conflict with a concurrent session."
)
CREATE_TS_NEXT_ACTION = (
    "Verify (1) the device can reach dnftp in vrf mgmt0 "
    "(`qactl cli ping dnftp --vrf mgmt0 -d <device>`), (2) sshd on dnftp "
    "accepts the dn account, (3) /ftpdisk/dn/oshaboo/ts on dnftp is "
    "writable, (4) this host (running qactl) can SFTP into dnftp with the "
    "same dn account (the verification stat after upload runs locally), and "
    "(5) no other tech-support generation is already running on the device "
    "(`qactl cli show 'show system tech-support status'`)."
)
FACTORY_DEFAULT_NEXT_ACTION = (
    "Check (1) the device is reachable and credentials are correct, and "
    "(2) commit did not conflict with a concurrent configure session. "
    "Consider taking a `qactl cli backup create -d <device>` snapshot "
    "first if you need a rollback target."
)
COMMIT_CONFLICT_NEXT_ACTION = (
    "Another session committed while this candidate was open, so DNOS "
    "flagged it out-of-sync and we answered the rebase prompt with 'abort' "
    "— nothing was applied. Re-run the same call: a fresh transaction "
    "rebases onto the new running config. If it keeps colliding, a "
    "concurrent operator (or automation loop) is committing repeatedly; "
    "serialise with them or retry once the device is quiet. To preview "
    "against the updated running first, run "
    "`qactl cli config --check '<statements>' -d <device>`."
)
EDIT_CONFIG_NEXT_ACTION = (
    "Verify each statement with `qactl cli search configure <keywords>` / "
    "`qactl cli help '<command>'` / `qactl cli crawl --config '<path>'` "
    "first; run `qactl cli config --check '<statement>' -d <device>` to "
    "run 'commit check' and see the validator's complaint without touching "
    "running config. Consider `qactl cli backup create -d <device>` before "
    "a large edit so rollback is cheap."
)
TEMPLATE_NEXT_ACTION = (
    "Run `qactl cli template list` / `qactl cli template get <name>` to see "
    "what's saved; template names must match [A-Za-z0-9._-]{1,60}. For "
    "create, the content body must be valid Jinja2 and non-empty — fix the "
    "syntax error (line/col in the errors list) and retry. Only register "
    "templates whose statements you have already verified against a live "
    "device via `qactl cli config --check '<statement>' -d <device>`."
)
RENDER_NEXT_ACTION = (
    "`qactl cli render` builds a local config only — no device is touched. "
    "Pass exactly one template source (--name a saved template, or "
    "--content/--template-file inline) and at most one vars source "
    "(--vars/--vars-file a YAML mapping, or --script/--script-file a Python "
    "generator that prints YAML to stdout). With no vars it's a preflight "
    "that reports declared_variables. A generator runs as 'python3 -I' in "
    "an isolated subprocess under jinja/scale/<name>/<ts>/ — check that "
    "audit dir (script.py / vars.yml / stderr.log) on failure. Then push "
    "with `qactl cli scale-deploy` (use --out to save a file, or pipe "
    "stdout into `qactl cli scale-deploy -`)."
)
SCALE_DEPLOY_NEXT_ACTION = (
    "`qactl cli scale-deploy` reads a rendered .cli config (one DNOS "
    "configure-mode statement per non-blank, non-'#' line) from a file path "
    "or stdin '-' and commits it in one shot. Build it first with "
    "`qactl cli render`, or check the path exists and is readable. Re-try "
    "with --check for a 'commit check' dry-run before pushing live. "
    "Consider a backup before a large deploy so rollback is cheap; on a "
    "commit failure the shared candidate is auto-cleared when "
    "--abort-on-failure is set."
)
