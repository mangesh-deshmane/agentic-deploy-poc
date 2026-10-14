"""
Streamlit front end for the agentic deploy platform.

Run:  streamlit run agent/ui.py

The whole flow lives here: a sentence becomes an intent, the intent resolves
against the catalog, the catalog renders Terraform variables, a human confirms,
a pull request opens, the pipeline plans, the policy gate runs, a human
approves, the pipeline applies.

Two gates are deliberate. Gate A confirms the request. Gate B approves the
actual plan. The second is the one that matters - it is enforced by the
pipeline refusing to upload a plan artifact that failed policy, not by
anything in this file.
"""

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import catalog, intake, planner                      # noqa: E402
from agent.github_ops import GitHub, GitHubError                 # noqa: E402
from agent.llm import LLMUnavailable, available as llm_available # noqa: E402
from agent.schema import ParsedIntent                            # noqa: E402

# ---------------------------------------------------------------- setup ----
st.set_page_config(page_title="Deploy", page_icon="▲", layout="wide")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

RUNS_LOG = Path("runs.jsonl")


def init_state():
    d = {
        "messages": [],          # chat transcript
        "intent": None,          # ParsedIntent accumulated across turns
        "resolution": None,      # catalog Resolution
        "request": None,         # validated DeployRequest
        "tfvars": None,          # (path, content)
        "correlation_id": None,
        "stage": "input",        # input | confirm | planning | approve | applying | done | blocked
        "plan_run_id": None,
        "apply_run_id": None,
        "service_url": None,
        "env_stem": None,
        "branch": None,
        "error": None,
        "clarify_count": 0,
    }
    for k, v in d.items():
        st.session_state.setdefault(k, v)


init_state()


def reset():
    for k in ("intent", "resolution", "request", "tfvars", "correlation_id",
              "plan_run_id", "apply_run_id", "service_url", "error",
              "env_stem", "branch"):
        st.session_state[k] = None
    st.session_state.stage = "input"
    st.session_state.clarify_count = 0


def say(role: str, text: str):
    st.session_state.messages.append({"role": role, "text": text})


def log_run(event: dict):
    """Append-only audit trail. Every run is reconstructable from this file."""
    import json
    event["at"] = datetime.now(timezone.utc).isoformat()
    with open(RUNS_LOG, "a") as fh:
        fh.write(json.dumps(event) + "\n")


def gh() -> GitHub:
    return GitHub()


# -------------------------------------------------------------- sidebar ----
with st.sidebar:
    st.markdown("### Platform")

    ok_llm = llm_available()
    ok_gh = bool(os.getenv("GITHUB_TOKEN") and os.getenv("GITHUB_REPO"))

    st.markdown(f"{'🟢' if ok_llm else '🔴'} Groq "
                f"{'' if ok_llm else '— set GROQ_API_KEY'}")
    st.markdown(f"{'🟢' if ok_gh else '🔴'} GitHub "
                f"{'' if ok_gh else '— set GITHUB_TOKEN / GITHUB_REPO'}")

    st.divider()
    st.markdown("### Catalog")
    for bp in catalog.available_blueprints():
        st.markdown(
            f"**{bp['app']}** → {bp['cloud']}  \n"
            f"<small>{bp['language']} · {bp['platform']} · {bp['data_store']}</small>",
            unsafe_allow_html=True,
        )
    for k, v in catalog.load()["blueprints"].items():
        if not v.get("available", True):
            st.markdown(
                f"**{v['app']}** → {v['cloud']}  \n<small>not yet implemented</small>",
                unsafe_allow_html=True,
            )

    st.divider()
    st.caption(
        "The agent holds no cloud credentials. It opens pull requests and "
        "reads run status. Everything that touches a cloud runs in GitHub "
        "Actions under federated identity, after the policy gate."
    )

    if st.button("Start over", use_container_width=True):
        st.session_state.messages = []
        reset()
        st.rerun()

# ----------------------------------------------------------------- head ----
st.title("Deploy")
st.caption("Describe what you want. A human approves before anything is created.")

left, right = st.columns([3, 2], gap="large")

# ----------------------------------------------------------------- chat ----
with left:
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["text"])

    if st.session_state.stage == "input":
        prompt = st.chat_input("e.g. deploy the acme reporting demo to dev on GCP")
        if prompt:
            say("user", prompt)
            try:
                intent = intake.parse(prompt, prior=st.session_state.intent)
                st.session_state.intent = intent

                if not intent.is_complete():
                    st.session_state.clarify_count += 1
                    if st.session_state.clarify_count > 2:
                        say("assistant",
                            "I still don't have enough to go on. Use the manual "
                            "form on the right.")
                        st.session_state.stage = "manual"
                    else:
                        say("assistant", intent.clarification
                            or "Which application, and which cloud?")
                    st.rerun()

                req = intent.to_request()
                res = catalog.resolve(req)
                st.session_state.request = req
                st.session_state.resolution = res

                if not res.ok:
                    # The rejection path. Nothing was created, no cloud API was
                    # called, no pull request opened.
                    alts = "\n".join(f"- {a}" for a in res.available_alternatives)
                    say("assistant",
                        f"**Rejected.** {res.reason}\n\n"
                        f"Registered deployments:\n{alts}\n\n"
                        f"_Nothing was created._")
                    st.session_state.stage = "blocked"
                    log_run({"event": "rejected", "reason": res.reason,
                             "app": req.app.value, "cloud": req.cloud.value})
                    st.rerun()

                cid = f"{req.customer}-{uuid.uuid4().hex[:6]}"
                st.session_state.correlation_id = cid
                st.session_state.tfvars = planner.render(
                    req, res.blueprint_key, cid
                )
                say("assistant",
                    "Resolved against the catalog. Review the plan on the "
                    "right, then confirm.")
                st.session_state.stage = "confirm"
                st.rerun()

            except LLMUnavailable as err:
                say("assistant",
                    f"The language model is unavailable ({err}). "
                    f"Use the manual form on the right.")
                st.session_state.stage = "manual"
                st.rerun()

    elif st.session_state.stage == "blocked":
        if st.button("Try something else"):
            reset()
            st.rerun()

# ---------------------------------------------------------------- panel ----
with right:
    stage = st.session_state.stage

    # -- manual fallback -----------------------------------------------
    if stage == "manual":
        st.markdown("#### Manual request")
        st.caption("The pipeline below is identical. The model only fills this in.")
        with st.form("manual"):
            app = st.selectbox("Application", ["app1", "app2"])
            cloud = st.selectbox("Cloud", ["gcp", "aws"])
            env = st.selectbox("Environment", ["dev", "test"])
            cust = st.text_input("Customer", "demo")
            ttl = st.number_input("TTL (hours)", 1, 168, 24)
            if st.form_submit_button("Resolve", type="primary"):
                intent = intake.manual_intent(app, cloud, env, cust, int(ttl))
                st.session_state.intent = intent
                req = intent.to_request()
                res = catalog.resolve(req)
                st.session_state.request, st.session_state.resolution = req, res
                if not res.ok:
                    say("assistant", f"**Rejected.** {res.reason}")
                    st.session_state.stage = "blocked"
                else:
                    cid = f"{req.customer}-{uuid.uuid4().hex[:6]}"
                    st.session_state.correlation_id = cid
                    st.session_state.tfvars = planner.render(req, res.blueprint_key, cid)
                    st.session_state.stage = "confirm"
                st.rerun()

    # -- gate A: confirm the request -----------------------------------
    if stage == "confirm" and st.session_state.request:
        st.markdown("#### Resolved request")
        summary = planner.summary(
            st.session_state.request, st.session_state.resolution.blueprint_key
        )
        for k, v in summary.items():
            c1, c2 = st.columns([2, 3])
            c1.caption(k)
            c2.markdown(f"`{v}`" if k in ("Service name", "Region") else v)

        path, content = st.session_state.tfvars
        with st.expander(f"Generated `{path}`", expanded=False):
            st.code(content, language="hcl")
        st.caption(
            "Rendered from a template, not written by the model. The model "
            "chose values for a fixed set of fields."
        )

        a, b = st.columns(2)
        if a.button("Confirm and plan", type="primary", use_container_width=True):
            try:
                g = gh()
                cid = st.session_state.correlation_id
                branch = f"deploy/{cid}"
                req = st.session_state.request

                st.session_state.branch = branch
                g.create_branch(branch)
                g.put_file(branch, path, content,
                           f"Deploy {req.app.value} for {req.customer} to {req.env_name.value}")
                pr = g.open_pr(
                    branch,
                    f"Deploy {req.app.value} · {req.customer} · {req.env_name.value}",
                    f"Requested via the agent.\n\n"
                    f"Correlation id: `{cid}`\n\n"
                    f"The policy gate runs on the plan. Nothing is applied "
                    f"until a human approves.",
                )
                # The agent writes envs/<stem>.tfvars; the pipeline must
                # plan that exact file, and its state prefix derives from it,
                # so two customers never share Terraform state.
                env_stem = Path(path).stem
                st.session_state.env_stem = env_stem
                g.dispatch("plan.yml", branch,
                           {"env_name": env_stem, "correlation_id": cid})

                st.session_state.pr_url = pr["html_url"]
                st.session_state.stage = "planning"
                log_run({"event": "dispatched", "correlation_id": cid,
                         "pr": pr["html_url"], "app": req.app.value,
                         "cloud": req.cloud.value, "env": req.env_name.value})
                say("assistant",
                    f"Pull request opened and the plan is running. "
                    f"[View PR]({pr['html_url']})")
                st.rerun()
            except GitHubError as err:
                st.error(f"GitHub: {err}")

        if b.button("Cancel", use_container_width=True):
            reset()
            st.rerun()

    # -- plan running ---------------------------------------------------
    if stage == "planning":
        st.markdown("#### Planning")
        st.caption("Build → Terraform plan → policy gate. Nothing is created yet.")

        @st.fragment(run_every=5)
        def watch_plan():
            try:
                g = gh()
                cid = st.session_state.correlation_id
                if st.session_state.plan_run_id is None:
                    run = g.find_run("plan.yml", cid, timeout=5)
                    if run:
                        st.session_state.plan_run_id = run.id
                    else:
                        st.info("Waiting for the run to appear…")
                        return

                run = g.get_run(st.session_state.plan_run_id)
                st.markdown(f"[Run #{run.id}]({run.url}) — `{run.status}`")

                for job in g.run_jobs(run.id):
                    for s in job["steps"]:
                        icon = {"success": "✅", "failure": "❌",
                                "skipped": "⬛"}.get(s["conclusion"], "⏳")
                        st.markdown(
                            f"<small>{icon} {s['name']}</small>",
                            unsafe_allow_html=True,
                        )

                if run.finished:
                    if run.ok:
                        st.session_state.stage = "approve"
                    else:
                        st.session_state.stage = "denied"
                    st.rerun()
            except GitHubError as err:
                st.error(str(err))

        watch_plan()

    # -- gate B: approve the plan ---------------------------------------
    if stage == "approve":
        st.success("Policy gate passed. The plan is ready for approval.")
        st.markdown("#### Approve this plan")
        st.caption(
            "Approving applies the exact plan that was just checked. The "
            "apply stage does not re-plan, so what you approve is what runs."
        )

        summary = planner.summary(
            st.session_state.request, st.session_state.resolution.blueprint_key
        )
        st.markdown(
            f"**{summary['Service name']}** · {summary['Cloud']} · "
            f"{summary['Estimated cost']}"
        )
        st.caption("Public ingress — dev only.")

        a, b = st.columns(2)
        approver = os.getenv("APPROVER", "operator")

        if a.button("Approve and deploy", type="primary", use_container_width=True):
            try:
                g = gh()
                cid = st.session_state.correlation_id
                g.dispatch("apply.yml", st.session_state.get("branch", "main"), {
                    "env_name": st.session_state.get("env_stem", "dev"),
                    "plan_run_id": str(st.session_state.plan_run_id),
                    "approver": approver,
                    "correlation_id": cid,
                })
                st.session_state.stage = "applying"
                log_run({"event": "approved", "correlation_id": cid,
                         "approver": approver,
                         "plan_run_id": st.session_state.plan_run_id})
                say("assistant", f"Approved by {approver}. Applying.")
                st.rerun()
            except GitHubError as err:
                st.error(str(err))

        if b.button("Reject", use_container_width=True):
            log_run({"event": "rejected_by_approver",
                     "correlation_id": st.session_state.correlation_id})
            say("assistant",
                "Rejected. Nothing was created. The decision is recorded in "
                "the audit trail along with the plan it refused.")
            st.session_state.stage = "blocked"
            st.rerun()

    # -- apply running --------------------------------------------------
    if stage == "applying":
        st.markdown("#### Deploying")

        @st.fragment(run_every=5)
        def watch_apply():
            try:
                g = gh()
                cid = st.session_state.correlation_id
                if st.session_state.apply_run_id is None:
                    run = g.find_run("apply.yml", cid, timeout=5)
                    if run:
                        st.session_state.apply_run_id = run.id
                    else:
                        st.info("Waiting for the run to appear…")
                        return

                run = g.get_run(st.session_state.apply_run_id)
                st.markdown(f"[Run #{run.id}]({run.url}) — `{run.status}`")

                for job in g.run_jobs(run.id):
                    for s in job["steps"]:
                        icon = {"success": "✅", "failure": "❌",
                                "skipped": "⬛"}.get(s["conclusion"], "⏳")
                        st.markdown(f"<small>{icon} {s['name']}</small>",
                                    unsafe_allow_html=True)

                if run.finished:
                    st.session_state.stage = "done" if run.ok else "failed"
                    st.rerun()
            except GitHubError as err:
                st.error(str(err))

        watch_apply()

    # -- outcome ---------------------------------------------------------
    if stage == "done":
        st.success("Deployed and verified.")
        req = st.session_state.request
        name = f"{req.app.value}-{planner.slug(req.customer)}-{req.env_name.value}"
        st.markdown(f"**{name}**")
        st.caption(
            "Health check passed and /api/info reports the right customer and "
            "environment — the deployment is parameterised, not just created."
        )
        st.markdown(
            f"[Open the apply run]"
            f"(https://github.com/{os.getenv('GITHUB_REPO','')}/actions/runs/"
            f"{st.session_state.apply_run_id}) for the URL."
        )
        if st.button("New deployment", type="primary"):
            reset()
            st.rerun()

    if stage == "denied":
        st.error("The policy gate refused this plan.")
        st.caption(
            "No plan artifact was produced, so the apply stage has nothing to "
            "consume even if it were dispatched. Nothing was created."
        )
        st.markdown(
            f"[See why](https://github.com/{os.getenv('GITHUB_REPO','')}/"
            f"actions/runs/{st.session_state.plan_run_id})"
        )
        if st.button("Start again"):
            reset()
            st.rerun()

    if stage == "failed":
        st.error("The apply failed.")
        st.markdown(
            f"[Open the run](https://github.com/{os.getenv('GITHUB_REPO','')}/"
            f"actions/runs/{st.session_state.apply_run_id})"
        )
        if st.button("Start again"):
            reset()
            st.rerun()
