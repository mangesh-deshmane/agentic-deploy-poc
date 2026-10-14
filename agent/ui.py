"""
Pipeline Pilot - a chat interface to the agentic deployment platform.

    streamlit run agent/ui.py

Single conversation thread. You type a request, the agent resolves it against
the catalog, shows you what it will create, and waits. Nothing reaches a cloud
until a human approves the plan the policy gate checked.

Environment is loaded BEFORE the agent modules import, because those modules
read configuration at import time.
"""

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must precede the agent imports - they read env at module level.
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

from agent import catalog, intake, planner                        # noqa: E402
from agent.github_ops import GitHub, GitHubError                   # noqa: E402
from agent.llm import LLMUnavailable, available as llm_available   # noqa: E402

st.set_page_config(page_title="Pipeline Pilot", page_icon="▲", layout="centered")

RUNS_LOG = Path("runs.jsonl")

# ------------------------------------------------------------------ style --
st.markdown("""
<style>
  .block-container{max-width:780px;padding-top:2rem}
  [data-testid="stChatMessage"]{background:transparent}
  .spec{border:1px solid rgba(128,128,128,.25);border-radius:10px;
        padding:12px 14px;margin:6px 0 2px}
  .spec .row{display:flex;justify-content:space-between;gap:16px;padding:3px 0;font-size:13.5px}
  .spec .row .k{opacity:.6}
  .spec .row .v{font-family:ui-monospace,Menlo,monospace;text-align:right}
  .step{font-size:13px;padding:1px 0;opacity:.85}
  .muted{opacity:.6;font-size:12.5px}
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------ state --
def init():
    for k, v in {
        "msgs": [],
        "intent": None, "request": None, "resolution": None,
        "tfvars": None, "cid": None, "branch": None, "env_stem": None,
        "pr_url": None, "plan_run": None, "apply_run": None,
        "stage": "idle", "clarify": 0, "failure": None,
    }.items():
        st.session_state.setdefault(k, v)


init()


def reset_request():
    for k in ("intent", "request", "resolution", "tfvars", "cid", "branch",
              "env_stem", "pr_url", "plan_run", "apply_run", "failure"):
        st.session_state[k] = None
    st.session_state.stage = "idle"
    st.session_state.clarify = 0


def say(role, text, spec=None):
    st.session_state.msgs.append({"role": role, "text": text, "spec": spec})


def log(event):
    import json
    event["at"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(RUNS_LOG, "a") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        pass


def spec_html(d):
    rows = "".join(
        f'<div class="row"><span class="k">{k}</span><span class="v">{v}</span></div>'
        for k, v in d.items()
    )
    return f'<div class="spec">{rows}</div>'


# ---------------------------------------------------------------- sidebar --
with st.sidebar:
    st.markdown("### Status")
    st.markdown(f"{'🟢' if llm_available() else '🔴'} Groq")
    st.markdown(f"{'🟢' if os.getenv('GITHUB_TOKEN') else '🔴'} GitHub")
    st.caption(f"model · `{os.getenv('GROQ_MODEL_INTENT', 'default')}`")

    st.markdown("### Catalog")
    for bp in catalog.available_blueprints():
        st.markdown(f"`{bp['app']}` → `{bp['cloud']}` · {bp['language']}")
    st.caption("Anything not listed is rejected without reaching a cloud.")

    st.divider()
    if st.button("Clear conversation", use_container_width=True):
        st.session_state.msgs = []
        reset_request()
        st.rerun()

    st.caption(
        "The agent has no cloud credentials. It opens pull requests and reads "
        "run status. Deployments run in GitHub Actions under federated "
        "identity, after the policy gate and a human approval."
    )

# ------------------------------------------------------------------ header -
st.title("Pipeline Pilot")
st.caption("Describe what you want deployed. Nothing is created without your approval.")

for m in st.session_state.msgs:
    with st.chat_message(m["role"]):
        st.markdown(m["text"])
        if m.get("spec"):
            st.markdown(spec_html(m["spec"]), unsafe_allow_html=True)


def gh():
    return GitHub()


# =========================================================== conversation ===
stage = st.session_state.stage

# ---- 1. accept a request ---------------------------------------------------
if stage in ("idle", "clarify"):
    prompt = st.chat_input("deploy app1 to dev on gcp for acme")
    if prompt:
        say("user", prompt)
        try:
            intent = intake.parse(prompt, prior=st.session_state.intent)
            st.session_state.intent = intent

            if not intent.is_complete():
                st.session_state.clarify += 1
                if st.session_state.clarify > 2:
                    say("assistant",
                        "I still can't tell which application and cloud you mean. "
                        "Try naming both, for example **deploy app1 on gcp**.")
                    st.session_state.stage = "idle"
                    st.session_state.intent = None
                    st.session_state.clarify = 0
                else:
                    say("assistant", intent.clarification
                        or "Which application, and which cloud?")
                    st.session_state.stage = "clarify"
                st.rerun()

            req = intent.to_request()
            res = catalog.resolve(req)
            st.session_state.request, st.session_state.resolution = req, res

            if not res.ok:
                opts = " · ".join(f"`{a}`" for a in res.available_alternatives)
                say("assistant",
                    f"**I can't deploy that.**\n\n{res.reason.strip()}\n\n"
                    f"What I can deploy: {opts}\n\n"
                    f"_No pull request was opened and no cloud API was called._")
                log({"event": "rejected", "app": req.app.value,
                     "cloud": req.cloud.value, "reason": res.reason.strip()})
                st.session_state.stage = "idle"
                st.session_state.intent = None
                st.rerun()

            cid = f"{planner.slug(req.customer)}-{uuid.uuid4().hex[:6]}"
            st.session_state.cid = cid
            st.session_state.tfvars = planner.render(req, res.blueprint_key, cid)
            s = planner.summary(req, res.blueprint_key)

            say("assistant",
                f"I'll deploy **{s['Application']}** for **{s['Customer']}** "
                f"into **{s['Environment']}** on {s['Cloud']}.",
                spec={
                    "service": s["Service name"],
                    "runtime": s["Runtime"],
                    "region": s["Region"],
                    "scaling": s["Scaling"],
                    "ttl": s["TTL"],
                    "cost": s["Estimated cost"],
                })
            st.session_state.stage = "confirm"
            st.rerun()

        except LLMUnavailable as err:
            say("assistant",
                f"I couldn't reach the language model.\n\n`{err}`\n\n"
                f"You can still deploy using the form below.")
            st.session_state.stage = "manual"
            st.rerun()

# ---- 2. manual fallback ----------------------------------------------------
if stage == "manual":
    with st.chat_message("assistant"):
        with st.form("manual", border=False):
            c1, c2, c3 = st.columns(3)
            app = c1.selectbox("Application", ["app1", "app2"])
            cloud = c2.selectbox("Cloud", ["gcp", "aws"])
            env = c3.selectbox("Environment", ["dev", "test"])
            c4, c5 = st.columns(2)
            cust = c4.text_input("Customer", "acme")
            ttl = c5.number_input("TTL (hours)", 1, 168, 24)
            if st.form_submit_button("Resolve", type="primary"):
                intent = intake.manual_intent(app, cloud, env, cust, int(ttl))
                req = intent.to_request()
                res = catalog.resolve(req)
                st.session_state.intent = intent
                st.session_state.request, st.session_state.resolution = req, res
                say("user", f"deploy {app} on {cloud} for {cust} to {env}")
                if not res.ok:
                    say("assistant", f"**I can't deploy that.**\n\n{res.reason.strip()}")
                    st.session_state.stage = "idle"
                else:
                    cid = f"{planner.slug(req.customer)}-{uuid.uuid4().hex[:6]}"
                    st.session_state.cid = cid
                    st.session_state.tfvars = planner.render(req, res.blueprint_key, cid)
                    s = planner.summary(req, res.blueprint_key)
                    say("assistant",
                        f"I'll deploy **{s['Application']}** for **{s['Customer']}**.",
                        spec={"service": s["Service name"], "runtime": s["Runtime"],
                              "region": s["Region"], "cost": s["Estimated cost"]})
                    st.session_state.stage = "confirm"
                st.rerun()

# ---- 3. gate A: confirm the request ---------------------------------------
if stage == "confirm":
    path, content = st.session_state.tfvars
    with st.chat_message("assistant"):
        with st.expander(f"Configuration I'll commit · `{path}`"):
            st.code(content, language="hcl")
            st.caption("Rendered from a template. The model chose values for a "
                       "fixed set of fields; it did not write this file.")
        a, b = st.columns([1, 1])
        if a.button("Prepare deployment", type="primary", use_container_width=True):
            try:
                g = gh()
                req = st.session_state.request
                cid = st.session_state.cid
                branch = f"deploy/{cid}"
                stem = Path(path).stem

                g.create_branch(branch)
                g.put_file(branch, path, content,
                           f"Deploy {req.app.value} for {planner.slug(req.customer)}")
                pr = g.open_pr(
                    branch,
                    f"Deploy {req.app.value} · {planner.slug(req.customer)} · {req.env_name.value}",
                    f"Requested through the agent.\n\nCorrelation `{cid}`\n\n"
                    f"The policy gate runs against the plan. Nothing is applied "
                    f"until a human approves.")
                g.dispatch("plan.yml", branch,
                           {"env_name": stem, "correlation_id": cid})

                st.session_state.branch = branch
                st.session_state.env_stem = stem
                st.session_state.pr_url = pr["html_url"]
                say("assistant",
                    f"Opened [pull request #{pr['number']}]({pr['html_url']}) and "
                    f"started the plan. Building the image, planning the change, "
                    f"then the policy gate.")
                log({"event": "planning", "cid": cid, "pr": pr["html_url"],
                     "app": req.app.value, "env": stem})
                st.session_state.stage = "planning"
                st.rerun()
            except GitHubError as err:
                say("assistant", f"GitHub rejected the request.\n\n`{err}`")
                st.session_state.stage = "idle"
                st.rerun()

        if b.button("Cancel", use_container_width=True):
            say("assistant", "Cancelled. Nothing was created.")
            reset_request()
            st.rerun()


# ---- shared: render a run's steps, and explain a failure ------------------
def show_run(run_id, title):
    """Live step list. On failure, name the step and show the real error."""
    g = gh()
    run = g.get_run(run_id)
    st.markdown(f"**{title}** · [run #{run.id}]({run.url}) · `{run.status}`")

    failed_step = None
    for job in g.run_jobs(run.id):
        for s in job["steps"]:
            icon = {"success": "✅", "failure": "❌", "skipped": "⬜",
                    "cancelled": "⬜"}.get(s["conclusion"], "⏳")
            st.markdown(f"<div class='step'>{icon} {s['name']}</div>",
                        unsafe_allow_html=True)
            if s["conclusion"] == "failure" and failed_step is None:
                failed_step = s["name"]
    return run, failed_step


def explain_failure(run_id, failed_step):
    """
    Turn a failed run into something readable: which step, and the lines of
    the log that actually matter. Never the whole log - it is enormous and
    the signal is in the error lines.
    """
    g = gh()
    raw = g.run_logs(run_id, tail=400)
    keep = []
    for line in raw.splitlines():
        low = line.lower()
        if any(t in low for t in ("error", "denied", "failed", "forbidden",
                                  "not found", "violation", "policy")):
            # strip the timestamp prefix GitHub adds to every log line
            keep.append(line[29:] if len(line) > 29 and line[4] == "-" else line)
    return failed_step, keep[-12:]


# ---- 4. plan running -------------------------------------------------------
if stage == "planning":
    with st.chat_message("assistant"):
        @st.fragment(run_every=4)
        def watch():
            try:
                g = gh()
                if st.session_state.plan_run is None:
                    r = g.find_run("plan.yml", st.session_state.cid, timeout=4)
                    if not r:
                        st.caption("waiting for the run to start…")
                        return
                    st.session_state.plan_run = r.id

                run, failed = show_run(st.session_state.plan_run, "Plan")
                if run.finished:
                    if run.ok:
                        st.session_state.stage = "approve"
                    else:
                        st.session_state.failure = explain_failure(
                            st.session_state.plan_run, failed)
                        st.session_state.stage = "plan_failed"
                    st.rerun()
            except GitHubError as err:
                st.error(str(err))
        watch()

# ---- 5. gate B: approve the plan ------------------------------------------
if stage == "approve":
    with st.chat_message("assistant"):
        s = planner.summary(st.session_state.request,
                            st.session_state.resolution.blueprint_key)
        st.markdown(
            f"**The policy gate passed.** Ready to deploy `{s['Service name']}`.")
        st.caption(
            "Approving applies the exact plan that was just checked — the apply "
            "stage does not re-plan. Public ingress, dev only.")
        a, b = st.columns(2)
        approver = os.getenv("APPROVER", "operator")
        if a.button("Approve & deploy", type="primary", use_container_width=True):
            try:
                gh().dispatch("apply.yml", st.session_state.branch, {
                    "env_name": st.session_state.env_stem,
                    "plan_run_id": str(st.session_state.plan_run),
                    "approver": approver,
                    "correlation_id": st.session_state.cid,
                })
                say("assistant", f"Approved by **{approver}**. Deploying.")
                log({"event": "approved", "cid": st.session_state.cid,
                     "approver": approver, "plan_run": st.session_state.plan_run})
                st.session_state.stage = "applying"
                st.rerun()
            except GitHubError as err:
                st.error(str(err))
        if b.button("Reject", use_container_width=True):
            say("assistant",
                "Rejected. Nothing was created. The decision is recorded in the "
                "audit trail alongside the plan it refused.")
            log({"event": "rejected_by_approver", "cid": st.session_state.cid})
            reset_request()
            st.rerun()

# ---- 6. apply running ------------------------------------------------------
if stage == "applying":
    with st.chat_message("assistant"):
        @st.fragment(run_every=4)
        def watch_apply():
            try:
                g = gh()
                if st.session_state.apply_run is None:
                    r = g.find_run("apply.yml", st.session_state.cid, timeout=4)
                    if not r:
                        st.caption("waiting for the run to start…")
                        return
                    st.session_state.apply_run = r.id

                run, failed = show_run(st.session_state.apply_run, "Deploy")
                if run.finished:
                    if run.ok:
                        st.session_state.stage = "done"
                    else:
                        st.session_state.failure = explain_failure(
                            st.session_state.apply_run, failed)
                        st.session_state.stage = "apply_failed"
                    st.rerun()
            except GitHubError as err:
                st.error(str(err))
        watch_apply()

# ---- 7. outcomes -----------------------------------------------------------
if stage == "done":
    req = st.session_state.request
    name = f"{req.app.value}-{planner.slug(req.customer)}-{req.env_name.value}"
    with st.chat_message("assistant"):
        st.success(f"**{name}** is live and verified.")
        st.caption(
            "Health check passed, and /api/info reports the app, customer and "
            "environment that were planned — so the deployment is parameterised, "
            "not just created.")
        st.markdown(
            f"[Open the run](https://github.com/{os.getenv('GITHUB_REPO','')}"
            f"/actions/runs/{st.session_state.apply_run}) for the service URL.")
        if st.button("Deploy something else", type="primary"):
            reset_request()
            st.rerun()

if stage in ("plan_failed", "apply_failed"):
    step, lines = st.session_state.failure or ("unknown", [])
    denied = stage == "plan_failed" and "policy" in (step or "").lower()
    with st.chat_message("assistant"):
        if denied:
            st.error("**Blocked by policy.**")
            st.caption(
                "No plan artifact was produced, so the deploy stage has nothing "
                "to apply even if it were triggered. Nothing was created.")
        else:
            st.error(f"**Failed at: {step}**")
        if lines:
            st.markdown("Why:")
            st.code("\n".join(lines), language="text")
        else:
            st.caption("No error detail could be read from the log.")
        rid = (st.session_state.plan_run if stage == "plan_failed"
               else st.session_state.apply_run)
        st.markdown(
            f"[Full log](https://github.com/{os.getenv('GITHUB_REPO','')}"
            f"/actions/runs/{rid})")
        if st.button("Try again", type="primary"):
            reset_request()
            st.rerun()
