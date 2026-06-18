# qactl

One **agent-shaped** command-line tool for the external services a QA
workflow touches outside the lab — **Jira**, **Confluence**, and
**Jenkins** — behind a single consistent contract. It replaces a set of
local MCP servers and scattered helper scripts with one executable that
an AI agent (or a human) can drive over a shell.

Pairs with [`dnctl`](https://github.com/oshaboo-dn/dnctl) (DNOS devices)
and [`ixiactl`](https://github.com/oshaboo-dn/ixiactl) (IxNetwork traffic
gen); those stay their own tools/repos.

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

Any of these can be overridden per-command (`--email`/`--token`/`--base-url`
for Atlassian; `--user`/`--token`/`--url` for Jenkins) but the environment
is the default. The repo ships no `.env` and no baked-in tokens.

## Subcommands

### `qactl jira`

| Command | Description | Gate |
|---|---|---|
| `whoami` | resolve the token to a Jira user | |
| `status <issue>` | issue status + summary | |
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
| `comment <page> [--text T] [--attach F]` | post a comment, optionally attaching+embedding a file | |
| `list <page>` | list a page's comments + attachments | |
| `delete <id>` | delete a comment or attachment by id | `--yes` |

### `qactl jenkins`

| Command | Description | Gate |
|---|---|---|
| `whoami` | sanity-check the Jenkins token | |
| `trigger <branch> [...flags] [--wait]` | trigger a cheetah build; `--wait` blocks until it finishes | `--yes` |
| `info <branch> [build]` | build details (params, result, causes) | |
| `console <branch> [build] [--tail N]` | tail the console log | |
| `list <branch> [--limit N]` | recent builds | |
| `stop <branch> --build-number N` | abort a build | `--yes` |

Cheetah trigger knobs map to Jenkins parameters: `--sanitizer`
(`TEST_NAMES=ENABLE_SANITIZER`), `--baseos`, `--no-lint`, `--no-dnos`,
`--no-tarballs`, `--no-smoke`, `--delta-build`, `--single-test*`,
`--nightly`, `--qa-version`, `--inherit-from <build#>`, `--extra-params
'<json>'`. Branch slashes (`feature/foo`) are URL-encoded for you.

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
```

## Layout

```
qactl/
  core/        envelope, output/exit-codes, creds (env), CLI plumbing
  jira/        client.py (REST) + cli.py  -> qactl jira ...
  confluence/  client.py (REST) + cli.py  -> qactl confluence ...
  jenkins/     client.py (REST) + cli.py  -> qactl jenkins ...
  __main__.py  argparse tree + dispatch
tests/         CLI-layer tests (no live services)
```
