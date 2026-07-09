# Area 5 — Discovery / devices / clear / ping / gitcommit / restart

First read `/home/dn/work/qactl/bug-hunt/_context.md` for the rules of
engagement and bug taxonomy. Then hunt this area thoroughly.

This area covers command discovery (cmd_search / crawlers), the device
registry, and operational ops (clear, ping, restart, gitcommit). The
device registry mutates on-disk state; restart/switchover is destructive
and expects the SSH session to drop.

## Files (read all; follow imports)
- `qactl/cli/tools/discovery.py`  (cmd_search / cmd_help / cli_crawler / cli_config_crawler)
- `qactl/cli/tools/devices.py`    (list_devices / manage_device add|remove — mutates the device map)
- `qactl/cli/tools/restart.py`    (request_system_restart / ncc switchover — destructive, session-drop expected)
- `qactl/cli/tools/clear.py`
- `qactl/cli/tools/ping.py`
- `qactl/cli/tools/gitcommit.py`

## Focus questions
- devices add/remove: atomic write of the device map (temp+rename)? Two
  concurrent CLI invocations clobbering the JSON? `--yes` gating on
  add/remove? Validation of the alias/SN (traversal, dup keys)?
- restart / ncc switchover: the SSH session dropping mid-command is the
  expected happy path — is a dropped session correctly mapped to
  success/timeout rather than a misleading error (or vice versa)? Are
  node_role / node_id validated before firing?
- clear: operational state with no commit/rollback — any wrong target
  selection or missing confirm for a disruptive clear?
- ping: arg/vrf/source-interface validation; parse of ping result;
  exit-code mapping (0% vs 100% loss).
- discovery crawlers: recursion/iteration bounds (can a crawl loop or
  explode?), correct handling of `Incomplete command`, regex parsing of
  help output.
