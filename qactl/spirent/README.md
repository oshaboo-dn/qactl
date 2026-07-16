# `qactl spirent` — Spirent TestCenter (STC REST) traffic group

The sibling of `qactl ixia`. Where `qactl ixia` drives an IxNetwork REST API
server via `ixnetwork-restpy`, `qactl spirent` drives a **Spirent TestCenter
REST server** (labserver) via the [`stcrestclient`](https://pypi.org/project/stcrestclient/)
package — plain HTTP, **no containers, no OTG adapter**.

> **Status: scaffold (2026-07-16).** Only the session/connection surface is
> wired. Ports, config-load, traffic, and protocol authoring land once the
> physical Spirent port is cabled. See the roadmap below.

## Why STC REST (not OTG/snappi)

Two Spirent integration styles exist in `drivenets/cheetah`:

| | **STC REST** (this module) | **OTG / snappi** |
|---|---|---|
| Talks to | the Spirent box's own REST server, directly | an `otg-adapter` container in front of Spirent |
| Config language | Spirent-native (`.tcc`, STC objects) | vendor-neutral OTG schema |
| Extra infra | none — just HTTP | 2 containers (adapter + labserver) |
| Matches `qactl ixia` | yes, 1:1 | no (container-managed) |

qactl is a lightweight, process-per-invocation CLI with no container
orchestration, so STC REST is the natural fit — it mirrors the ixia module
layer-for-layer and reuses the proven session logic from cheetah's
`src/tests/routing/spirent/dnstc/` (which is itself built on `stcrestclient`).

## Layers (mirror `qactl.ixia`)

```
qactl/spirent/
├── client/      low-level STC REST session (wraps stcrestclient, lazy import)
├── core/        response envelope + reattach-first session cache
├── tools/       high-level ops returning envelopes (diag.py: connect/sessions/describe)
└── ctl/         argparse CLI front (session group)
```

## Session model — reattach-first

qactl is process-per-invocation, so every command **reattaches** to the named
STC session rather than creating a duplicate (which would strand the previous
command's config). STC sessions are addressed by name — the server exposes
them as `"<name> - <user>"`. Default name `qactl-session` (override with
`--session NAME` or `$SPIRENT_SESSION`); `--new-session` forces a fresh one.

## Config (env)

| Env | Meaning | Default |
|---|---|---|
| `SPIRENT_HOST` | STC REST server host (makes `--host` optional) | — |
| `SPIRENT_PORT` | STC REST port | `80` |
| `SPIRENT_USER` | session user | `dn` |
| `SPIRENT_SESSION` | session name | `qactl-session` |
| `SPIRENT_PASSWORD` | password (usually none) | — |

Confirmed lab endpoint (2026-07-16, live-verified — `session sessions` returned
60 sessions):

- `SPIRENT_HOST` = **`il-auto-containers.dev.drivenets.net`** (10.10.50.18), port **80**
  — the labserver / STC REST server (shared; hosts the whole team's sessions).
- Our port location = **`//spirent01/6/13`** (chassis `spirent01`, slot 6, port 13),
  reserved *through* the labserver.

Note the chassis `spirent01` (192.168.114.37) answers on `:80` too, but that is
the chassis web UI, **not** the STC REST API — `stcrestclient` rejects it. Always
point `SPIRENT_HOST` at the labserver. (cheetah's `spirent_consts.py`
`SPIRENT_HOST = "kvm36"` / `spirent-vlab` are stale for this port.)

## Commands (scaffold)

```
qactl spirent session connect     # reattach-first probe; reports if the session existed
qactl spirent session sessions    # list STC sessions on the REST server (no join)
qactl spirent session describe    # connect + server/system/BLL info snapshot
```

Same contract as every qactl group: `--json` (exact envelope, pipe to `jq`),
real exit codes, `--yes` on destructive ops (none yet).

## Acceptance smoke test (needs a live STC REST server + `stcrestclient`)

```bash
pip install stcrestclient           # or: pip install -e .
export SPIRENT_HOST=<stc-rest-server>
qactl spirent session sessions --json
qactl spirent session connect --json
qactl spirent session describe --json
```

The offline unit tests (`tests/test_spirent.py`) cover parsing, the confirm
gate, exit-code mapping, envelope rendering, and the reattach decision (join
vs create) with a mocked `StcHttp` — no server or `stcrestclient` needed.

## Roadmap (once the port is cabled)

Mirror the ixia surface, reusing cheetah `dnstc` object logic:

1. `spirent port` — reserve / release / status (STC `reserve`, `is_online`).
2. `spirent config` — load `.tcc`/`.xml`, apply, save, reset.
3. `spirent traffic` — start / stop / wait, port + flow counters (`ResultsSubscribe`).
4. `spirent proto` / `spirent bgp` — device + BGP authoring (STC `create`/`config`).
5. Decide vendor-vs-dependency for `stcrestclient` (dep for now; vendor if the
   DriveNets logging patch in `dnstc` proves necessary).
