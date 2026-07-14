"""``qactl jobs`` — one place to list and inspect every async job.

qactl's long-running operations (tar-load, pre-check, tech-support, and the
``orc`` orchestrator) each persist a pollable envelope to the local job store
under their own namespace. This group reads across all of them:

    qactl jobs list [--kind …] [--status …] [-d DEV] [--limit N]
    qactl jobs show <job_id> | -d DEV [--kind …]

Read-only — it only reads the persisted envelopes, never a device. See
:mod:`qactl.jobs.tools`.
"""
