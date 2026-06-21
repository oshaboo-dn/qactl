"""Thin Jenkins REST client.

Lifted from the jenkins-mcp ``jenkins_core`` client. Credentials come
from :class:`qactl.core.creds.JenkinsConfig` (the environment) instead
of per-request MCP headers. The MCP's in-memory async build registry is
intentionally dropped: a CLI is process-per-invocation, so ``trigger``
either returns the queued handle or polls the build to completion inline
(``--wait``) using the same queue/build endpoints.

Multibranch jobs nest as ``/job/<org>/job/<repo>/job/<branch>``. A
branch like ``feature/foo`` contains a ``/`` which must be URL-encoded
*twice* (``%252F``) because Jenkins' servlet decodes once before
routing. :func:`branch_to_job_path` / :func:`_job_path_to_url` handle
this so callers pass plain branch names.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import requests

from qactl.core.creds import JenkinsConfig


def _job_path_to_url(base: str, job_path: str) -> str:
    if job_path.startswith("http"):
        return job_path.rstrip("/")
    parts = [p for p in job_path.strip("/").split("/") if p]
    out: list[str] = []
    for seg in parts:
        if "%252F" in seg:
            out.append(seg)
        elif "%2F" in seg:
            out.append(seg.replace("%2F", "%252F"))
        else:
            out.append(quote(seg, safe=""))
    return base + "/job/" + "/job/".join(out)


def branch_to_job_path(branch: str, repo: str = "cheetah", org: str = "drivenets") -> str:
    branch_seg = quote(branch, safe="").replace("%2F", "%252F")
    return f"{org}/{repo}/{branch_seg}"


class JenkinsError(RuntimeError):
    pass


class JenkinsClient:
    def __init__(self, cfg: JenkinsConfig, timeout: float = 30.0):
        self.cfg = cfg
        self.timeout = timeout
        self._session = requests.Session()
        self._session.auth = (cfg.user, cfg.token)
        self._crumb: tuple[str, str] | None = None

    def _crumb_header(self) -> dict[str, str]:
        if self._crumb is None:
            r = self._session.get(f"{self.cfg.url}/crumbIssuer/api/json", timeout=self.timeout)
            if r.status_code == 404:
                self._crumb = ("", "")
            else:
                r.raise_for_status()
                d = r.json()
                self._crumb = (d["crumbRequestField"], d["crumb"])
        field, value = self._crumb
        return {field: value} if field else {}

    def _job_url(self, job_path: str) -> str:
        return _job_path_to_url(self.cfg.url, job_path)

    def whoami(self) -> dict[str, Any]:
        r = self._session.get(f"{self.cfg.url}/me/api/json", timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        return {"id": d.get("id"), "fullName": d.get("fullName")}

    def get_job(self, job_path: str) -> dict[str, Any]:
        url = self._job_url(job_path)
        r = self._session.get(
            f"{url}/api/json",
            params={"tree": ",".join([
                "name", "fullName", "url", "buildable", "inQueue",
                "lastBuild[number,url,result,building]",
                "lastSuccessfulBuild[number,url]",
                "lastFailedBuild[number,url]",
                "lastCompletedBuild[number,url,result]",
                "property[parameterDefinitions[name,type,defaultParameterValue[value],choices]]",
            ])},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_build(self, job_path: str, build_number: int | str = "lastBuild") -> dict[str, Any]:
        url = self._job_url(job_path)
        r = self._session.get(
            f"{url}/{build_number}/api/json",
            params={"tree": ",".join([
                "number", "url", "result", "building", "duration",
                "estimatedDuration", "timestamp", "displayName",
                "actions[parameters[name,value],causes[shortDescription,userId,userName]]",
            ])},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        params: dict[str, Any] = {}
        causes: list[dict[str, Any]] = []
        for a in d.get("actions") or []:
            for p in a.get("parameters") or []:
                params[p.get("name")] = p.get("value")
            for c in a.get("causes") or []:
                causes.append({k: c.get(k) for k in
                               ("shortDescription", "userId", "userName") if c.get(k)})
        return {
            "number": d.get("number"), "url": d.get("url"), "result": d.get("result"),
            "building": d.get("building"), "duration_ms": d.get("duration"),
            "estimated_duration_ms": d.get("estimatedDuration"),
            "timestamp_ms": d.get("timestamp"), "display_name": d.get("displayName"),
            "parameters": params, "causes": causes,
        }

    def get_build_parameters(self, job_path: str, build_number: int | str = "lastBuild") -> dict[str, Any]:
        return self.get_build(job_path, build_number)["parameters"]

    def get_build_artifacts(self, job_path: str, build_number: int | str = "lastBuild") -> dict[str, Any]:
        """The build's archived artifacts plus enough build context to fetch them."""
        url = self._job_url(job_path)
        r = self._session.get(
            f"{url}/{build_number}/api/json",
            params={"tree": "number,url,result,building,artifacts[fileName,relativePath]"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        build_url = (d.get("url") or f"{url}/{build_number}/").rstrip("/") + "/"
        return {
            "number": d.get("number"), "url": build_url, "result": d.get("result"),
            "building": d.get("building"), "artifacts": d.get("artifacts") or [],
        }

    def get_artifact_text(self, build_url: str, relative_path: str) -> str:
        """Fetch the (text) contents of one archived artifact by relative path."""
        r = self._session.get(
            f"{build_url.rstrip('/')}/artifact/{relative_path.lstrip('/')}",
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.text

    def trigger_build(self, job_path: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(parameters or {})
        url = self._job_url(job_path)
        endpoint = f"{url}/buildWithParameters" if params else f"{url}/build"
        form = [(k, "" if v is None else str(v)) for k, v in params.items()]
        r = self._session.post(
            endpoint, data=form, headers=self._crumb_header(),
            timeout=self.timeout, allow_redirects=False,
        )
        if r.status_code not in (200, 201, 302, 303):
            raise JenkinsError(
                f"Jenkins refused build trigger: HTTP {r.status_code} body={r.text[:300]!r}"
            )
        queue_url = r.headers.get("Location", "").rstrip("/")
        queue_id: int | None = None
        if queue_url:
            try:
                queue_id = int(queue_url.rsplit("/", 1)[-1])
            except ValueError:
                queue_id = None
        return {
            "status_code": r.status_code, "queue_url": queue_url,
            "queue_id": queue_id, "job_url": url, "parameters_sent": params,
        }

    def get_queue_item(self, queue_id: int) -> dict[str, Any]:
        r = self._session.get(
            f"{self.cfg.url}/queue/item/{queue_id}/api/json", timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        ex = d.get("executable") or {}
        return {
            "queue_id": queue_id, "why": d.get("why"), "blocked": d.get("blocked"),
            "stuck": d.get("stuck"), "cancelled": d.get("cancelled"),
            "build_number": ex.get("number"), "build_url": ex.get("url"),
        }

    def cancel_queue_item(self, queue_id: int) -> dict[str, Any]:
        r = self._session.post(
            f"{self.cfg.url}/queue/cancelItem", params={"id": queue_id},
            headers=self._crumb_header(), timeout=self.timeout,
        )
        return {"status_code": r.status_code, "queue_id": queue_id}

    def get_console(self, job_path: str, build_number: int | str = "lastBuild",
                    tail_lines: int | None = 200) -> dict[str, Any]:
        url = self._job_url(job_path)
        r = self._session.get(f"{url}/{build_number}/consoleText", timeout=self.timeout)
        r.raise_for_status()
        text = r.text
        if tail_lines is not None:
            text = "\n".join(text.splitlines()[-tail_lines:])
        return {
            "build_number": build_number if isinstance(build_number, int) else None,
            "lines_returned": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "text": text,
        }

    def stop_build(self, job_path: str, build_number: int) -> dict[str, Any]:
        url = self._job_url(job_path)
        r = self._session.post(
            f"{url}/{build_number}/stop", headers=self._crumb_header(),
            timeout=self.timeout, allow_redirects=False,
        )
        return {"status_code": r.status_code, "build_url": f"{url}/{build_number}/"}

    def list_recent_builds(self, job_path: str, limit: int = 10) -> list[dict[str, Any]]:
        url = self._job_url(job_path)
        r = self._session.get(
            f"{url}/api/json",
            params={"tree": f"builds[number,url,result,building,timestamp,duration]{{0,{limit}}}"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json().get("builds", [])

    def wait_for_build_number(self, queue_id: int, timeout_s: float = 300.0,
                              poll_s: float = 5.0) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_queue_item(queue_id)
            if last.get("cancelled"):
                return {**last, "status": "cancelled"}
            if last.get("build_number"):
                return {**last, "status": "started"}
            time.sleep(poll_s)
        return {**last, "status": "timeout"}

    def wait_for_build_result(self, job_path: str, build_number: int,
                              timeout_s: float = 4 * 3600, poll_s: float = 30.0) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_build(job_path, build_number)
            if not last.get("building") and last.get("result"):
                return {**last, "status": "finished"}
            time.sleep(poll_s)
        return {**last, "status": "timeout"}

    @classmethod
    def from_env(cls, timeout: float = 30.0, **overrides: str) -> "JenkinsClient":
        return cls(JenkinsConfig.resolve(**overrides), timeout=timeout)
