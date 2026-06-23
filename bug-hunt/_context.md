# Bug hunt: qactl `cli` group — shared context

You are a meticulous code reviewer hunting for **real, triggerable bugs**
in the qactl DNOS device CLI at `/home/dn/work/qactl/dnctl/cli`. Read-only:
do not edit anything.

## What this tool is
`qactl cli ...` is an "agent-shaped" CLI that wraps SSH-to-DNOS-device
operations. **Each CLI invocation is ONE short-lived process.** The same
tool functions are ALSO exposed over a long-running stdio MCP server
(`qactl mcp cli`). This dual-front split is a rich bug source: anything
that relies on in-process state surviving across calls (in-memory
registries, daemon threads, module-level caches/singletons) works on the
MCP server but **BREAKS under the one-shot CLI**.

Reference bug (just fixed, issue #17): `tar-load start` registered an
in-memory job + spawned a daemon worker thread, then the CLI process
exited — killing the worker mid-upload and discarding the registry, so
`tar-load show` could never find the job. Look for more of this class.

## The agent-shaped contract (violations are bugs)
- `--json` output must be lossless; one consistent envelope shape.
- Exit codes are semantic: 0 only on `status` ok/warning, non-zero otherwise.
- Every destructive/mutating subcommand MUST be gated by `--yes` and
  refuse off a TTY without it.
- stdin (`-`) / `--file` / inline must be accepted for text payload args.
- No secrets in code/logs; credentials resolve at runtime from env/config.

## What to look for (prioritise real defects over style)
- Logic errors: off-by-one, inverted conditions, wrong comparisons.
- The #17 class: in-process state that can't survive a one-shot CLI
  (daemon threads, in-memory job registries, caches expected to persist).
- Resource leaks: paramiko SSH/SFTP/transport, file handles, threads not
  closed on error paths; channels left open after timeout.
- Exception handling: bare/over-broad `except` that swallows real errors;
  error paths that still return `status: ok` / exit 0; envelopes missing
  `errors`; partial-failure reported as success.
- Concurrency: shared mutable state, missing locks, device-slot mutex
  gaps, races between kickoff and worker.
- Parsing/regex: wrong/fragile patterns scraping DNOS output, false
  matches, case/locale bugs, catastrophic backtracking (ReDoS).
- Injection / path traversal in filenames, remote SFTP paths, shell
  command construction, URL interpolation.
- Mutable default arguments; shared default list/dict instances.
- Timeout handling: ignored timeouts, hangs, infinite poll loops.
- Missing `--yes` gate on a destructive op; TTY-refusal bypass.
- stdin/`--file` payload resolution bugs.

## Output format (be concrete; verify against the code before reporting)
For each finding:
- `[SEVERITY high|med|low] path:line — one-line title`
- What's wrong (quote/cite the exact code).
- How to trigger + impact.
- Suggested fix (1–2 lines).

Rules: skip pure style/nits. No speculation you haven't checked against
the actual code. Group findings by file. End with a ranked **Top 3** for
your area. If you find nothing real in a file, say so briefly.
