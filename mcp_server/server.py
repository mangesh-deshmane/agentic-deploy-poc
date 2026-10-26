"""
Pipeline Pilot as an MCP server.

    python -m mcp_server.server

Exposes the platform's catalog, policy, pipeline and audit surface as scoped
tools, so any MCP client can drive it. This is the Integration / Tool layer
from the reference architecture: the agent cannot call arbitrary APIs, only
these.

What is NOT here, on purpose:

  * no approval tool. An agent can prepare a deployment and read its plan;
    only a human can apply it, in the UI or in GitHub. Exposing approval
    would let an agent approve its own work, which is the one thing the
    whole design exists to prevent.
  * no apply, no destroy, no IAM, no secret access.
  * no Terraform authoring. prepare_deployment takes parameters and renders
    the tfvars from a reviewed template server-side - the model never writes
    infrastructure code.

Every tool returns {"ok": bool, ...}. Failures come back as data with a
reason rather than as exceptions, because a refusal a model can read is
worth more than a stack trace.
"""

import os
import sys
from pathlib import Path

# Run from the repository root whichever way the client launches us, so the
# relative paths the agent modules use (envs/, policy/, catalog/) resolve.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
except ImportError:
    pass

from mcp.server.mcpserver import MCPServer          # noqa: E402
from mcp.types import ToolAnnotations               # noqa: E402

from mcp_server import tools                        # noqa: E402

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                         idempotent_hint=False)

mcp = MCPServer(
    name="pipeline-pilot",
    title="Pipeline Pilot",
    version="0.1.0",
    instructions=(
        "Deployment platform for per-customer demo environments on GCP.\n\n"
        "Use check_supported before proposing anything: unregistered "
        "combinations are refused with a specific reason, and that reason is "
        "the answer. Use check_policy to find out whether values would be "
        "allowed without running anything.\n\n"
        "prepare_deployment opens a pull request and starts a Terraform plan. "
        "It creates no infrastructure and cannot apply. Applying requires a "
        "human approval in the Pipeline Pilot UI; there is deliberately no "
        "tool for it. Do not tell the user you can deploy - you can prepare a "
        "deployment for them to approve."
    ),
)


# ------------------------------------------------------------- catalog ----
@mcp.tool(
    description="List everything this platform is registered to deploy: "
                "applications, clouds, runtimes and permitted regions.",
    annotations=READ_ONLY,
)
def list_blueprints() -> dict:
    return tools.list_blueprints()


@mcp.tool(
    description="Check whether an application can be deployed to a cloud and "
                "region. When it cannot, returns the specific reason - which "
                "account is not federated, which module does not exist - "
                "rather than a bare refusal.",
    annotations=READ_ONLY,
)
def check_supported(app: str, cloud: str, region: str = "") -> dict:
    return tools.check_supported(app=app, cloud=cloud, region=region)


# -------------------------------------------------------- environments ----
@mcp.tool(
    description="List registered environments with their app, customer, "
                "region and TTL. Registered is not the same as deployed: a "
                "torn-down environment keeps its configuration so it can be "
                "stood up again.",
    annotations=READ_ONLY,
)
def list_environments() -> dict:
    return tools.list_environments()


# -------------------------------------------------------------- policy ----
@mcp.tool(
    description="Ask whether a set of values would pass the policy gate, "
                "without running anything - no plan, no cloud call, no cost. "
                "Returns each violation with the rule and the reason behind "
                "it. Advisory: the gate in CI runs against the real "
                "Terraform plan and is the only authority.",
    annotations=READ_ONLY,
)
def check_policy(app: str = "", cloud: str = "gcp", region: str = "",
                 min_instance_count: int | None = None,
                 max_instance_count: int | None = None,
                 memory: str = "", cpu: str = "",
                 resource_type: str = "") -> dict:
    return tools.check_policy(
        app=app, cloud=cloud, region=region,
        min_instance_count=min_instance_count,
        max_instance_count=max_instance_count,
        memory=memory, cpu=cpu, resource_type=resource_type,
    )


@mcp.tool(
    description="Monthly cost for an environment, with the reasons it is "
                "what it is - scale to zero, idle CPU billing, free-tier "
                "allowances.",
    annotations=READ_ONLY,
)
def estimate_cost(app: str = "app1", cloud: str = "gcp", env: str = "dev",
                  customer: str = "demo", region: str = "") -> dict:
    return tools.estimate_cost(app=app, cloud=cloud, env=env,
                               customer=customer, region=region)


# ------------------------------------------------------------ pipeline ----
@mcp.tool(
    description="Render the Terraform variables for a request without "
                "committing anything, so a human can review the exact "
                "configuration before it reaches the repository. Returns a "
                "correlation id to pass to prepare_deployment, which "
                "guarantees the bytes reviewed are the bytes committed.",
    annotations=READ_ONLY,
)
def render_config(app: str, cloud: str = "gcp", env: str = "dev",
                  customer: str = "demo", region: str = "",
                  ttl_hours: int = 24) -> dict:
    return tools.render_config(app=app, cloud=cloud, env=env,
                               customer=customer, region=region,
                               ttl_hours=ttl_hours)


@mcp.tool(
    description="Prepare a deployment: render the Terraform variables from a "
                "reviewed template, open a pull request, and start a plan "
                "that stops at the policy gate. Creates no infrastructure. "
                "Cannot apply - a human approves in the Pipeline Pilot UI.",
    annotations=WRITES,
)
def prepare_deployment(app: str, cloud: str = "gcp", env: str = "dev",
                       customer: str = "demo", region: str = "",
                       ttl_hours: int = 24,
                       correlation_id: str = "") -> dict:
    return tools.prepare_deployment(app=app, cloud=cloud, env=env,
                                    customer=customer, region=region,
                                    ttl_hours=ttl_hours,
                                    correlation_id=correlation_id)


@mcp.tool(
    description="Status of a plan or apply run, by run id or by the "
                "correlation id prepare_deployment returned, including which "
                "step is running and which failed.",
    annotations=READ_ONLY,
)
def get_run_status(correlation_id: str = "", run_id: int = 0,
                   workflow: str = "plan.yml") -> dict:
    return tools.get_run_status(correlation_id=correlation_id,
                                run_id=run_id, workflow=workflow)


@mcp.tool(
    description="Deployment history: who requested each one, who approved "
                "it, the correlation id and the result. Read from GitHub's "
                "run history, where the approver is recorded at queue time "
                "and cannot be altered afterwards.",
    annotations=READ_ONLY,
)
def get_audit_trail(limit: int = 20) -> dict:
    return tools.get_audit_trail(limit=limit)


# ----------------------------------------------------------- resources ----
# The rules themselves, so a client can read what is allowed instead of
# inferring it from refusals.
@mcp.resource(
    "pipeline-pilot://policy",
    name="Policy rules",
    description="The policy gate's rules: allowed and denied resource types, "
                "and the value constraints, each with its reason.",
    mime_type="text/yaml",
)
def policy_resource() -> str:
    p = ROOT / "policy" / "policy.yaml"
    return p.read_text() if p.exists() else "# policy.yaml not found"


@mcp.resource(
    "pipeline-pilot://catalog",
    name="Service catalog",
    description="The blueprints this platform can deploy, and the "
                "combinations deliberately absent with the reason for each.",
    mime_type="text/yaml",
)
def catalog_resource() -> str:
    for rel in ("catalog/blueprints.yaml", "agent/blueprints.yaml"):
        p = ROOT / rel
        if p.exists():
            return p.read_text()
    return "# blueprints.yaml not found"


if __name__ == "__main__":
    mcp.run(transport="stdio")
