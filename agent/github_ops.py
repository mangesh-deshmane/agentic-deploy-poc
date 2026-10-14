"""
The agent's entire reach into the world: open a PR, dispatch a workflow,
read run status.

No cloud credentials. A repo-scoped token and nothing else. Everything that
touches GCP or AWS happens inside GitHub Actions under federated identity,
after the policy gate and after a human approves.
"""

import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

API = "https://api.github.com"


@dataclass
class Run:
    id: int
    name: str
    status: str          # queued | in_progress | completed
    conclusion: Optional[str]   # success | failure | cancelled | None
    url: str

    @property
    def finished(self) -> bool:
        return self.status == "completed"

    @property
    def ok(self) -> bool:
        return self.conclusion == "success"


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, repo: Optional[str] = None, token: Optional[str] = None):
        self.repo = repo or os.getenv("GITHUB_REPO", "")
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        if not self.repo or not self.token:
            raise GitHubError(
                "GITHUB_REPO and GITHUB_TOKEN must be set in .env"
            )
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    # ---- plumbing ----
    def _call(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{API}{path}", timeout=30, **kw)
        if r.status_code >= 400:
            raise GitHubError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # ---- writing a tfvars file ----
    def default_branch_sha(self, branch: str = "main") -> str:
        return self._call("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")["object"]["sha"]

    def create_branch(self, name: str, base: str = "main") -> str:
        sha = self.default_branch_sha(base)
        self._call("POST", f"/repos/{self.repo}/git/refs",
                   json={"ref": f"refs/heads/{name}", "sha": sha})
        return name

    def put_file(self, branch: str, path: str, content: str, message: str):
        import base64

        # Update in place if the file already exists on that branch.
        sha = None
        try:
            existing = self._call(
                "GET", f"/repos/{self.repo}/contents/{path}", params={"ref": branch}
            )
            sha = existing.get("sha")
        except GitHubError:
            pass

        body = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        return self._call("PUT", f"/repos/{self.repo}/contents/{path}", json=body)

    def open_pr(self, branch: str, title: str, body: str, base: str = "main") -> dict:
        return self._call("POST", f"/repos/{self.repo}/pulls",
                          json={"title": title, "body": body,
                                "head": branch, "base": base})

    # ---- running workflows ----
    def dispatch(self, workflow: str, ref: str, inputs: dict) -> None:
        """
        Fire a workflow. GitHub returns 204 with no body and no run id, which
        is why every dispatch carries a correlation_id - it is the only way to
        find the run we just started rather than someone else's.
        """
        self._call("POST", f"/repos/{self.repo}/actions/workflows/{workflow}/dispatches",
                   json={"ref": ref, "inputs": inputs})

    def find_run(self, workflow: str, correlation_id: str,
                 timeout: int = 60) -> Optional[Run]:
        """Poll until a run whose name contains the correlation id appears."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self._call(
                "GET", f"/repos/{self.repo}/actions/workflows/{workflow}/runs",
                params={"per_page": 20},
            )
            for r in data.get("workflow_runs", []):
                if correlation_id in (r.get("name") or ""):
                    return Run(id=r["id"], name=r["name"], status=r["status"],
                               conclusion=r.get("conclusion"), url=r["html_url"])
            time.sleep(3)
        return None

    def get_run(self, run_id: int) -> Run:
        r = self._call("GET", f"/repos/{self.repo}/actions/runs/{run_id}")
        return Run(id=r["id"], name=r.get("name") or "", status=r["status"],
                   conclusion=r.get("conclusion"), url=r["html_url"])

    def run_jobs(self, run_id: int) -> list[dict]:
        """Step-level detail, so the UI can show which stage is running."""
        data = self._call("GET", f"/repos/{self.repo}/actions/runs/{run_id}/jobs")
        out = []
        for job in data.get("jobs", []):
            out.append({
                "name": job["name"],
                "status": job["status"],
                "conclusion": job.get("conclusion"),
                "steps": [
                    {"name": s["name"], "status": s["status"],
                     "conclusion": s.get("conclusion")}
                    for s in job.get("steps", [])
                ],
            })
        return out

    def run_logs(self, run_id: int, tail: int = 80) -> str:
        """
        Last N lines, for the SRE agent. Never the whole log - Groq's free
        tier allows 12k tokens a minute and a CI log is far larger than that.
        """
        r = self.s.get(f"{API}/repos/{self.repo}/actions/runs/{run_id}/logs",
                       timeout=60, allow_redirects=True)
        if r.status_code >= 400:
            return ""
        import io
        import zipfile

        try:
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            lines: list[str] = []
            for name in zf.namelist():
                if name.endswith(".txt"):
                    lines.extend(zf.read(name).decode("utf-8", "replace").splitlines())
            return "\n".join(lines[-tail:])
        except Exception:
            return ""
