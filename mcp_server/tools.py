"""
The platform's capabilities as plain functions.

Deliberately free of any MCP import: every function here is callable and
testable without a client, and server.py is a thin binding over them. When
the SDK changes shape - as it did between mcp 1.x and 2.x - only the binding
moves, not the logic.

Every function returns a JSON-serialisable dict. Failures are returned as
data with a reason, never raised, because a tool that throws gives the
calling model nothing to reason about.
"""

import os
import re
from pathlib import Path

import yaml

from agent import catalog, planner, service
from agent.github_ops import GitHub, GitHubError

POLICY = Path("policy/policy.yaml")
ENVS = Path("envs")


# --------------------------------------------------------------- helpers --
def _ok(**kw) -> dict:
    return {"ok": True, **kw}


def _no(reason: str, **kw) -> dict:
    return {"ok": False, "reason": reason, **kw}


def _policy() -> dict:
    if not POLICY.exists():
        return {}
    return yaml.safe_load(POLICY.read_text()) or {}


def _tfvars(path: Path) -> dict:
    text = path.read_text()

    def field(key: str, default: str = "") -> str:
        m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', text, re.M)
        return m.group(1) if m else default

    def number(key: str, default=None):
        m = re.search(rf'^\s*{key}\s*=\s*(\d+)', text, re.M)
        return int(m.group(1)) if m else default

    return {
        "env_stem": path.stem,
        "app": field("app"),
        "customer": field("customer"),
        "environment": field("env_name"),
        "region": field("region"),
        "ttl_hours": number("ttl_hours"),
        "protected": bool(re.search(r'^\s*protected\s*=\s*true', text, re.M)),
        "service": f'{field("app")}-{field("customer")}-{field("env_name")}',
    }


# ----------------------------------------------------------- catalog ------
def list_blueprints() -> dict:
    """Everything this platform is registered to deploy."""
    try:
        bps = catalog.available_blueprints()
    except Exception as err:                                  # noqa: BLE001
        return _no(f"could not read the catalog: {err}")

    return _ok(blueprints=[
        {
            "key": f"{b['app']}-{b['cloud']}",
            "app": b["app"],
            "cloud": b["cloud"],
            "display_name": b.get("display_name", ""),
            "language": b.get("language", ""),
            "platform": b.get("platform", ""),
            "data_store": b.get("data_store", ""),
            "regions": b.get("regions", []),
        }
        for b in bps
    ])


def check_supported(app: str, cloud: str, region: str = "") -> dict:
    """
    Whether a combination can be deployed, and if not, exactly why.

    The reason matters more than the verdict: an unregistered target should
    tell the caller what is missing, not merely that the answer is no.
    """
    try:
        _, res = service.resolve(app=app, cloud=cloud,
                                 region=region or None)
    except Exception as err:                                  # noqa: BLE001
        return _no(f"{app!r} on {cloud!r} is not a value this platform accepts: {err}")

    if not res.ok:
        # The alternatives travel with the refusal: a caller told "no" should
        # be able to say what the answer would have been.
        return _no(res.reason.strip(), app=app, cloud=cloud,
                   region=region or None,
                   alternatives=list(res.available_alternatives or []))
    return _ok(app=app, cloud=cloud, region=region or None,
               blueprint=res.blueprint_key)


# --------------------------------------------------------- environments ---
def list_environments() -> dict:
    """
    Registered environments, read from the committed tfvars files.

    Registered is not the same as deployed - a torn-down environment keeps
    its file so it can be stood up again.
    """
    if not ENVS.is_dir():
        return _ok(environments=[], note="no envs/ directory")

    rows = [_tfvars(f) for f in sorted(ENVS.glob("*.tfvars"))]

    # Two files can describe the same service name while keeping separate
    # Terraform state. Worth surfacing: deploying both would make them fight.
    seen: dict = {}
    for r in rows:
        seen.setdefault(r["service"], []).append(r["env_stem"])
    for r in rows:
        clash = [s for s in seen[r["service"]] if s != r["env_stem"]]
        if clash:
            r["collides_with"] = clash

    return _ok(environments=rows, count=len(rows))


# -------------------------------------------------------------- policy ----
def check_policy(app: str = "", cloud: str = "gcp", region: str = "",
                 min_instance_count: int | None = None,
                 max_instance_count: int | None = None,
                 memory: str = "", cpu: str = "",
                 resource_type: str = "") -> dict:
    """
    Would these values survive the policy gate?

    Answered against policy.yaml without running anything - no plan, no
    cloud call, no cost. This is the one thing the chat UI cannot do today.

    It is an advisory pre-check, not the gate. The gate runs against the
    real plan JSON in CI and is the only authority.
    """
    pol = _policy()
    if not pol:
        return _no(f"policy file not found at {POLICY}")

    violations: list[dict] = []

    denied = pol.get("denied_resource_types", {}) or {}
    allowed = set(pol.get("allowed_resource_types", []) or [])

    if resource_type:
        if resource_type in denied:
            violations.append({"field": "resource_type",
                               "value": resource_type,
                               "reason": denied[resource_type]})
        elif allowed and resource_type not in allowed:
            violations.append({"field": "resource_type",
                               "value": resource_type,
                               "reason": "not on the allowlist"})

    rules = (pol.get("value_rules", {}) or {}).get(
        "google_cloud_run_v2_service", [])

    # Map the caller's friendly names onto the plan paths the rules use.
    supplied = {
        "template.scaling[0].min_instance_count": min_instance_count,
        "template.scaling[0].max_instance_count": max_instance_count,
        "template.containers[0].resources[0].limits.memory": memory or None,
        "template.containers[0].resources[0].limits.cpu": cpu or None,
        "location": region or None,
    }

    for rule in rules:
        got = supplied.get(rule["path"])
        if got is None:
            continue
        op, want = rule["op"], rule["value"]
        bad = None
        if op == "equals" and got != want:
            bad = f"must be {want!r}"
        elif op == "max" and float(got) > float(want):
            bad = f"maximum is {want}"
        elif op == "min" and float(got) < float(want):
            bad = f"minimum is {want}"
        elif op == "in" and got not in want:
            bad = f"allowed values: {want}"
        if bad:
            violations.append({"field": rule["path"], "value": got,
                               "reason": f"{bad}. {rule.get('reason','')}".strip()})

    if app and cloud:
        sup = check_supported(app, cloud, region)
        if not sup["ok"]:
            violations.append({"field": "blueprint", "value": f"{app}+{cloud}",
                               "reason": sup["reason"]})

    if violations:
        return _no(f"{len(violations)} violation(s)", violations=violations,
                   checked_against=str(POLICY))
    return _ok(violations=[], checked_against=str(POLICY),
               note="advisory pre-check; the gate in CI runs against the real plan")


def estimate_cost(app: str = "app1", cloud: str = "gcp", env: str = "dev",
                  customer: str = "demo", region: str = "") -> dict:
    """What this environment will cost, and why."""
    try:
        req, res = service.resolve(app=app, cloud=cloud, env=env,
                                   customer=customer, region=region or None)
    except Exception as err:                                  # noqa: BLE001
        return _no(str(err))
    if not res.ok:
        return _no(res.reason.strip())

    d = catalog.defaults()
    summary = planner.summary(req, res.blueprint_key)
    return _ok(
        monthly="$0.00",
        basis=[
            f"min_instance_count = {d['min_instance_count']} (scale to zero, "
            f"so no instance exists between requests)",
            "cpu_idle = true (CPU billed per request, not per instance lifetime)",
            f"{d['memory']} / {d['cpu']} vCPU within the always-free allowance",
            "Firestore within the free daily quota",
        ],
        service_name=summary["Service name"],
        caveat="Cross-region reads cost fractions of a cent if the service "
               "and Firestore are in different regions.",
    )


# ------------------------------------------------------------ pipeline ----
def render_config(app: str, cloud: str = "gcp", env: str = "dev",
                  customer: str = "demo", region: str = "",
                  ttl_hours: int = 24) -> dict:
    """
    Render the Terraform variables for a request without committing anything.

    Exists so a human can review the configuration BEFORE it is committed.
    prepare_deployment renders and commits in one call, which is fine for an
    agent but would put the review after the commit if the UI used it - and
    the review preceding the commit is the whole point of the first gate.

    The correlation id is returned so the caller can pass the same one to
    prepare_deployment, guaranteeing the bytes reviewed are the bytes
    committed rather than a second render that merely ought to match.
    """
    try:
        req, res = service.resolve(app=app, cloud=cloud, env=env,
                                   customer=customer, region=region or None,
                                   ttl_hours=ttl_hours)
    except Exception as err:                                  # noqa: BLE001
        return _no(f"invalid request: {err}")

    if not res.ok:
        return _no(res.reason.strip())

    cid = service.correlation_id(customer)
    path, content = planner.render(req, res.blueprint_key, cid)
    summary = planner.summary(req, res.blueprint_key)

    return _ok(
        correlation_id=cid,
        tfvars_path=path,
        tfvars=content,
        env_stem=path.rsplit("/", 1)[-1].removesuffix(".tfvars"),
        summary=summary,
        blueprint=res.blueprint_key,
        note="Nothing has been created or committed. Pass this correlation_id "
             "to prepare_deployment to commit exactly these bytes.",
    )


def prepare_deployment(app: str, cloud: str = "gcp", env: str = "dev",
                       customer: str = "demo", region: str = "",
                       ttl_hours: int = 24,
                       correlation_id: str = "") -> dict:
    """
    Open a deploy PR and start a plan. Creates no infrastructure.

    This is the only tool that writes anything, and what it writes is a
    branch and a pull request. The plan it starts stops at the policy gate;
    applying requires a human approval that is deliberately not exposed as
    a tool.
    """
    try:
        req, res = service.resolve(app=app, cloud=cloud, env=env,
                                   customer=customer, region=region or None,
                                   ttl_hours=ttl_hours)
    except Exception as err:                                  # noqa: BLE001
        return _no(f"invalid request: {err}")

    if not res.ok:
        return _no(res.reason.strip())

    try:
        out = service.prepare(req, res.blueprint_key,
                              cid=correlation_id or None)
    except GitHubError as err:
        return _no(f"GitHub rejected the request: {err}")
    except Exception as err:                                  # noqa: BLE001
        return _no(f"could not prepare the deployment: {err}")

    return _ok(
        **out,
        next_step="A human approves in the Pipeline Pilot UI. This tool "
                  "cannot apply, and no approval tool exists.",
    )


def get_run_status(correlation_id: str = "", run_id: int = 0,
                   workflow: str = "plan.yml") -> dict:
    """Where a plan or apply has got to, by run id or correlation id."""
    try:
        g = GitHub()
        if run_id:
            run = g.get_run(run_id)
        elif correlation_id:
            run = g.find_run(workflow, correlation_id, timeout=6)
            if run is None:
                return _no("no run carrying that correlation id has started yet",
                           correlation_id=correlation_id)
        else:
            return _no("pass either run_id or correlation_id")

        steps = []
        for job in g.run_jobs(run.id):
            for s in job["steps"]:
                steps.append({"name": s["name"], "status": s["status"],
                              "conclusion": s["conclusion"]})

        return _ok(run_id=run.id, name=run.name, status=run.status,
                   conclusion=run.conclusion, url=run.url,
                   finished=run.finished, passed=run.ok, steps=steps)
    except GitHubError as err:
        return _no(str(err))


def get_audit_trail(limit: int = 20) -> dict:
    """
    Who requested and who approved each deployment.

    Read from GitHub's run history. The approver is written into the run
    name by GitHub when the workflow is queued, so it cannot be altered
    afterwards - not by this server, and not from this machine.
    """
    try:
        rows = GitHub().list_runs()
    except GitHubError as err:
        return _no(str(err))

    rows = [r for r in rows if r.get("action")][:limit]
    return _ok(count=len(rows), entries=[
        {
            "action": r["action"],
            "environment": r["env"],
            "approved_by": r["approver"] or None,
            "requested_by": r["actor"] or None,
            "correlation_id": r["cid"],
            "result": r["conclusion"],
            "at": r["created_at"],
            "run": r["url"],
        }
        for r in rows
    ])
