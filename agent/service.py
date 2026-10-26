"""
The orchestration the UI used to do inline, as plain functions.

Extracted so there is exactly one implementation of "prepare a deployment"
and two callers: the Streamlit UI and the MCP server. Without this, the MCP
path would be a second, drifting copy of the same six GitHub calls.

Nothing here imports Streamlit, and nothing here holds a cloud credential.
"""

import os
import uuid
from pathlib import Path

from . import catalog, planner
from .github_ops import GitHub, GitHubError
from .schema import DeployRequest


# Where deploy branches are cut from and where their PRs merge back to.
# Defaults to main; the MCP demo sets this to its own branch so nothing it
# does lands on the branch the working demo runs from.
def base_branch() -> str:
    return os.getenv("BASE_BRANCH", "main")


def correlation_id(customer: str) -> str:
    return f"{planner.slug(customer)}-{uuid.uuid4().hex[:6]}"


def prepare(req: DeployRequest, blueprint_key: str, cid: str | None = None,
            rendered: tuple[str, str] | None = None) -> dict:
    """
    Open a deploy PR and start a plan. Creates no infrastructure.

    `rendered` is the (path, content) the caller has already shown to a human.
    Passing it guarantees that the bytes reviewed are the bytes committed,
    rather than relying on render() being deterministic for the same inputs.
    The MCP path has nobody to show it to, so it renders here.

    Returns the handles a caller needs to follow it: correlation id, branch,
    environment stem, PR url and number. The plan run id is not returned
    because a workflow_dispatch does not report one - the caller polls for a
    run whose name carries the correlation id.
    """
    g = GitHub()
    base = base_branch()

    cid = cid or correlation_id(req.customer)
    path, content = rendered or planner.render(req, blueprint_key, cid)
    stem = Path(path).stem
    branch = f"deploy/{cid}"

    g.create_branch(branch, base=base)
    g.put_file(branch, path, content,
               f"Deploy {req.app.value} for {planner.slug(req.customer)}")
    pr = g.open_pr(
        branch,
        f"Deploy {req.app.value} · {planner.slug(req.customer)} · {req.env_name.value}",
        f"Requested through the agent.\n\nCorrelation `{cid}`\n\n"
        f"The policy gate runs against the plan. Nothing is applied "
        f"until a human approves.",
        base=base,
    )
    g.dispatch("plan.yml", branch, {"env_name": stem, "correlation_id": cid})

    return {
        "correlation_id": cid,
        "branch": branch,
        "env_stem": stem,
        "tfvars_path": path,
        "tfvars": content,
        "pr_url": pr["html_url"],
        "pr_number": pr["number"],
        "base_branch": base,
    }


def resolve(app: str, cloud: str, env: str = "dev", customer: str = "demo",
            region: str | None = None, ttl_hours: int = 24) -> tuple:
    """
    Validate and resolve a request against the catalog.

    Returns (request, resolution). The resolution carries the refusal reason
    when the combination is not registered - that reason is the whole point,
    so it is never flattened to a boolean.
    """
    req = DeployRequest(
        app=app, cloud=cloud, env_name=env,
        customer=customer, region=region, ttl_hours=ttl_hours,
    )
    return req, catalog.resolve(req)


__all__ = ["prepare", "resolve", "correlation_id", "base_branch", "GitHubError"]
