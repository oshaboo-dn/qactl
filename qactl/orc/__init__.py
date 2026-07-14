"""``qactl orc`` — orchestrate multi-step build/deploy flows.

The orchestrator chains the existing single-purpose surfaces into one
pollable job:

- ``orc load <build-url>``  — tar-load a build, then run the pre-upgrade
  pre-check (load and pre-check are two distinct steps, in order).
- ``orc build <branch>``    — trigger a cheetah Jenkins build, wait for it,
  then tar-load + pre-check.

Both run through the same phase driver and persist progress to the local
job store after every phase, so ``orc show`` can poll a detached run from
any later process. See :mod:`qactl.orc.tools`.
"""
