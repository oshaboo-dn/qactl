# Area 1 — SSH session / transport core

First read `/home/dn/work/qactl/bug-hunt/_context.md` for the rules of
engagement, the agent-shaped contract, and the bug taxonomy. Then hunt
this area thoroughly.

This is the SSH/transport substrate every other tool sits on, so subtle
bugs here are high-impact (leaked sessions, hangs, mis-parsed prompts,
auto-confirm answering the wrong prompt, multi-step sequences aborting
incorrectly).

## Files (read all; follow imports)
- `dnctl/cli/core/session.py`  (run_once / run_sequence, ConnectResult, StepCapture, timeouts, auto_confirm)
- `dnctl/cli/core/shell.py`    (interactive shell / prompt detection / channel handling)
- `dnctl/cli/core/shell_exec.py`
- `dnctl/cli/core/runner.py`
- `dnctl/cli/core/registry.py` (transport_registry — is it safe across the one-shot CLI vs MCP server?)
- `dnctl/cli/core/locks.py`

## Focus questions
- Are paramiko transports/channels/SFTP always closed on every error path
  (connect failure, timeout, exception mid-sequence)?
- Prompt detection: can the regex mis-fire on banner/MOTD/output that
  looks like a prompt, or miss a prompt and hang to timeout?
- `auto_confirm`: can it answer "yes" to the wrong prompt, or send into a
  channel that already closed?
- `run_sequence` stop_predicate / step capture: is an aborted step
  classified correctly? Off-by-one in the steps list?
- `transport_registry`: is it a module-level cache assumed to persist? Any
  cross-request leakage of sessions/credentials between devices?
- Timeout values: any place a timeout is accepted but not actually applied
  to the channel/exec.
