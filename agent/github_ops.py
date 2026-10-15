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
        # Every transport failure becomes a GitHubError. This class promises
        # callers one exception type; letting a requests exception escape made
        # a momentary network problem crash the whole Streamlit page rather
        # than degrade one panel.
        try:
            r = self.s.request(method, f"{API}{path}", timeout=30, **kw)
        except requests.exceptions.ConnectionError as err:
            raise GitHubError(
                "Cannot reach api.github.com. Check the network, then reload."
            ) from err
        except requests.exceptions.Timeout as err:
            raise GitHubError("api.github.com timed out after 30s.") from err
        except requests.exceptions.RequestException as err:
            raise GitHubError(f"GitHub request failed: {err}") from err

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

    # ---- audit trail ----
    # The run name is the audit record. GitHub writes it from the dispatch
    # inputs at queue time, so the approver and correlation id in it cannot be
    # edited afterwards by anyone - including by this agent. That is why the
    # trail is read back from here rather than from the local runs.jsonl,
    # which is just a convenience log on one laptop.
    AUDIT_WORKFLOWS = ("plan.yml", "apply.yml", "destroy.yml")

    @staticmethod
    def parse_run_name(name: str) -> dict:
        """
        Pull the fields back out of a run name, parsing from the right because
        the environment stem itself contains hyphens.

            apply-dev-app2-acme-acme-a1b2c3-by-ayush
            |     |             |          |
            |     env           cid        approver

        Anything unparseable is returned as-is rather than guessed at - a
        wrong audit record is worse than an incomplete one.
        """
        out = {"action": "", "env": "", "cid": "", "approver": ""}
        if not name:
            return out

        head, _, rest = name.partition("-")
        if head not in ("plan", "apply", "destroy") or not rest:
            return out
        out["action"] = head

        if "-by-" in rest:
            rest, _, approver = rest.rpartition("-by-")
            out["approver"] = approver

        parts = rest.split("-")
        # A generated correlation id ends in six hex characters; a manual or
        # pull-request run does not.
        if len(parts) >= 2 and len(parts[-1]) == 6 and all(
            c in "0123456789abcdef" for c in parts[-1]
        ):
            out["cid"] = "-".join(parts[-2:])
            out["env"] = "-".join(parts[:-2])
        else:
            out["cid"] = parts[-1] if len(parts) > 1 else ""
            out["env"] = "-".join(parts[:-1]) if len(parts) > 1 else rest

        return out

    def list_runs(self, workflows: Optional[tuple] = None,
                  per_workflow: int = 20) -> list[dict]:
        """
        Recent runs across the deployment workflows, newest first.

        A workflow that does not exist yet (destroy.yml before it is merged)
        is skipped rather than failing the whole listing.
        """
        rows: list[dict] = []
        targets = workflows or self.AUDIT_WORKFLOWS
        failures = 0

        for wf in targets:
            try:
                data = self._call(
                    "GET", f"/repos/{self.repo}/actions/workflows/{wf}/runs",
                    params={"per_page": per_workflow},
                )
            except GitHubError:
                # One workflow failing is expected - destroy.yml does not exist
                # until it is merged. Every workflow failing means GitHub could
                # not be reached, and that must not be reported to the caller as
                # an empty history: "nothing happened" and "I could not check"
                # are different answers.
                failures += 1
                continue

            for r in data.get("workflow_runs", []):
                name = r.get("name") or ""
                fields = self.parse_run_name(name)
                rows.append({
                    **fields,
                    "id": r["id"],
                    "name": name,
                    "status": r["status"],
                    "conclusion": r.get("conclusion"),
                    "created_at": r.get("run_started_at") or r.get("created_at") or "",
                    "actor": (r.get("triggering_actor") or {}).get("login", ""),
                    "url": r["html_url"],
                })

        if failures == len(targets):
            raise GitHubError(
                "Could not read run history from GitHub - this is not the same "
                "as there being no deployments."
            )

        rows.sort(key=lambda x: x["created_at"], reverse=True)
        return rows

    def run_logs(self, run_id: int, tail: int = 80) -> str:
        """
        Last N lines, for the SRE agent. Never the whole log - Groq's free
        tier allows 12k tokens a minute and a CI log is far larger than that.
        """
        try:
            r = self.s.get(f"{API}/repos/{self.repo}/actions/runs/{run_id}/logs",
                           timeout=60, allow_redirects=True)
        except requests.exceptions.RequestException:
            # No logs is a degraded explanation, not a reason to fail the page.
            return ""
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
