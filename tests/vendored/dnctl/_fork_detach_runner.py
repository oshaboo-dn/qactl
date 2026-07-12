"""Clean-process driver for the real-``os.fork()`` tar-load detach test.

Run as a standalone script (``python -m`` / ``python <path>``), NOT under
pytest. It exists so the ``detach=True`` fork happens in a fresh
single-threaded interpreter — exactly what the one-shot CLI front does in
production — rather than forking out of the pytest process (which by the
time the fork test runs carries background daemon threads from the rest of
the suite). Mirroring production keeps the test faithful and side-steps any
inherited-thread hazard.

Contract: the caller sets ``QACTL_STATE_DIR`` (shared with the parent so it
can read the persisted job back) and passes ``<jenkins_url> <device>`` as
argv. We install the same no-network mocks the test uses, kick off a
detached load, reap the forked worker, and print a one-line JSON result
(kickoff state + worker pid + child exit code + job_id) to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import types


# The DNOS/GI tarball URLs the mocked Jenkins artifacts resolve to; the
# worker turns each into a ``request system target-stack load <tar>`` step.
DNOS_TAR = "http://minio/pkg/drivenets_dnos_26.2.0.610_dev.dev_v26_2_1565.tar"
GI_TAR = "http://minio/pkg/drivenets_gi_26.2.0.610_dev.dev_v26_2_1565.tar"


def _step(command, output, hit_prompt=True):
    return types.SimpleNamespace(
        command=command,
        output=output,
        hit_prompt=hit_prompt,
        head_prompt_line=f"HOST# {command}",
        tail_prompt="HOST#",
    )


def _result(steps):
    return types.SimpleNamespace(
        host="HOST1",
        device="dev",
        output="\n".join(s.output for s in steps),
        head_prompt_line="HOST#",
        tail_prompt="HOST#",
        steps=steps,
    )


def _install_mocks(tarload):
    """Mirror the test's ``_mock_kickoff_network`` + ``_clean_sequence`` so
    nothing touches the network/device."""
    urls = {"gi_DNOS_artifact.txt": DNOS_TAR, "gi_GI_artifact.txt": GI_TAR}

    tarload._fetch_jenkins_artifact = (
        lambda base, name, fetch_timeout: (urls.get(name), None)
    )

    def _probe(reg, **kw):
        assert kw.get("command") == tarload._SHOW_SYSTEM_CMD
        return types.SimpleNamespace(
            output="Version: DNOS [26.2.0.610]", hit_prompt=True,
            head_prompt_line="HOST#", tail_prompt="HOST#",
            host="HOST1", device="dev", steps=[],
        )

    tarload.run_once = _probe

    def _clean_sequence(reg, **kw):
        steps = [_step(c, "Package loaded.") for c in kw["commands"]]
        sp = kw.get("stop_predicate")
        for s in steps:
            if sp is not None and sp(s):
                break
        return _result(steps)

    tarload.run_sequence = _clean_sequence


def main(argv):
    jenkins_url, device = argv[0], argv[1]

    from qactl.dnos.cli.tools import tarload

    _install_mocks(tarload)

    env = tarload.request_system_tar_load(
        jenkins_url=jenkins_url, device=device, confirm=True,
        detach=True, pre_check=False, notify_slack="",
    )
    pid = env.get("worker_pid")
    child_exit = None
    if isinstance(pid, int) and pid > 0:
        _, status = os.waitpid(pid, 0)
        child_exit = os.waitstatus_to_exitcode(status)

    print(json.dumps({
        "kickoff_state": env.get("state"),
        "kickoff_status": env.get("status"),
        "worker_pid": pid,
        "child_exit": child_exit,
        "job_id": env.get("job_id"),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
