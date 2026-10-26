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

# catalog and planner remain only for the sidebar's blueprint list and the
# tfvars reader; everything on the deploy path now goes through mcp_client.
from agent import catalog, intake, planner                         # noqa: E402
from agent import mcp_client                                       # noqa: E402
from agent.github_ops import GitHub, GitHubError                   # noqa: E402
from agent.llm import LLMUnavailable, available as llm_available   # noqa: E402
from agent.mcp_client import MCPUnavailable                        # noqa: E402

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
        "pr_url": None, "pr_number": None, "merge_note": None,
        "plan_run": None, "apply_run": None,
        "stage": "idle", "clarify": 0, "failure": None,
        "destroy_env": None, "destroy_run": None, "destroy_cid": None,
        "blueprint": None, "summary": None, "trace": [],
    }.items():
        st.session_state.setdefault(k, v)


init()


def reset_request():
    for k in ("intent", "request", "resolution", "tfvars", "cid", "branch",
              "env_stem", "pr_url", "pr_number", "merge_note",
              "plan_run", "apply_run", "failure", "blueprint", "summary"):
        st.session_state[k] = None
    st.session_state.stage = "idle"
    st.session_state.clarify = 0
    st.session_state.trace = []


# ------------------------------------------------------- the tool layer ---
# Every read and every write below goes through the MCP server, the same
# interface an outside agent uses. The UI holds no privileged shortcut: if
# something cannot be done through a tool, the UI cannot do it either.
#
# The two exceptions are deliberate and are the whole point of the design:
# approving a plan and applying it are NOT tools, so they stay on the UI's
# own code path. An agent can prepare; only a person here can apply.
def tool(name: str, **args) -> dict:
    """Call an MCP tool and record it, so the UI can show what it called."""
    import time

    t0 = time.time()
    try:
        out = mcp_client.call(name, **args)
    except MCPUnavailable as err:
        st.session_state.trace.append(
            {"tool": name, "ok": None, "ms": 0, "error": str(err)})
        raise
    st.session_state.trace.append({
        "tool": name,
        "ok": out.get("ok"),
        "ms": int((time.time() - t0) * 1000),
    })
    return out


def resolve_and_render(req, ttl_hours: int) -> tuple[bool, dict]:
    """
    Check the request against the catalog and render its configuration,
    both through the tool layer.

    Returns (ok, payload). On refusal the payload carries the reason and the
    alternatives; on success it carries the correlation id, the rendered
    tfvars and the summary - so the bytes shown to the approver are the ones
    prepare_deployment will commit against the same correlation id.
    """
    args = dict(app=req.app.value, cloud=req.cloud.value,
                env=req.env_name.value, customer=req.customer,
                region=req.region or "", ttl_hours=ttl_hours)

    sup = tool("check_supported", app=args["app"], cloud=args["cloud"],
               region=args["region"])
    if not sup.get("ok"):
        return False, sup

    rc = tool("render_config", **args)
    if not rc.get("ok"):
        return False, rc
    return True, rc


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

    # Checked once per session - each probe spawns the server, which costs a
    # second, and a sidebar is redrawn on every interaction.
    if "mcp_tools" not in st.session_state:
        try:
            st.session_state.mcp_tools = mcp_client.list_tools()
        except MCPUnavailable:
            st.session_state.mcp_tools = []
    n = len(st.session_state.mcp_tools)
    st.markdown(f"{'🟢' if n else '🔴'} MCP tool layer")
    if n:
        st.caption(f"{n} tools · every action below goes through them")
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
            st.session_state.request = req
            st.session_state.trace = []

            ok, out = resolve_and_render(req, intent.ttl_hours or 24)
            st.session_state.resolution = out

            if not ok:
                opts = " · ".join(f"`{a}`" for a in out.get("alternatives", []))
                say("assistant",
                    f"**I can't deploy that.**\n\n{out['reason'].strip()}"
                    + (f"\n\nWhat I can deploy: {opts}" if opts else "")
                    + "\n\n_No pull request was opened and no cloud API was called._")
                log({"event": "rejected", "app": req.app.value,
                     "cloud": req.cloud.value, "reason": out["reason"].strip()})
                st.session_state.stage = "idle"
                st.session_state.intent = None
                st.rerun()

            st.session_state.cid = out["correlation_id"]
            st.session_state.tfvars = (out["tfvars_path"], out["tfvars"])
            st.session_state.blueprint = out["blueprint"]
            st.session_state.summary = out["summary"]
            s = out["summary"]

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
        except MCPUnavailable as err:
            say("assistant",
                f"The tool server didn't answer.\n\n`{err}`\n\n"
                f"Nothing was created. Check that `mcp_server` is importable "
                f"from this directory.")
            st.session_state.stage = "idle"
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
                st.session_state.intent = intent
                st.session_state.request = req
                st.session_state.trace = []
                say("user", f"deploy {app} on {cloud} for {cust} to {env}")
                try:
                    ok, out = resolve_and_render(req, int(ttl))
                except MCPUnavailable as err:
                    say("assistant", f"The tool server didn't answer.\n\n`{err}`")
                    st.session_state.stage = "idle"
                    st.rerun()

                st.session_state.resolution = out
                if not ok:
                    say("assistant", f"**I can't deploy that.**\n\n{out['reason'].strip()}")
                    st.session_state.stage = "idle"
                else:
                    st.session_state.cid = out["correlation_id"]
                    st.session_state.tfvars = (out["tfvars_path"], out["tfvars"])
                    st.session_state.blueprint = out["blueprint"]
                    st.session_state.summary = out["summary"]
                    s = out["summary"]
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
        if st.session_state.trace:
            calls = " → ".join(
                f"{t['tool']}{'' if t['ok'] else ' ✕'}"
                for t in st.session_state.trace)
            st.caption(f"via the tool layer: `{calls}`")

        with st.expander(f"Configuration I'll commit · `{path}`"):
            st.code(content, language="hcl")
            st.caption("Rendered from a template. The model chose values for a "
                       "fixed set of fields; it did not write this file.")
        a, b = st.columns([1, 1])
        if a.button("Prepare deployment", type="primary", use_container_width=True):
            try:
                req = st.session_state.request
                cid = st.session_state.cid

                # Through the tool layer, with the correlation id from
                # render_config - so what gets committed is exactly what was
                # shown above, not a second render that ought to match.
                out = tool("prepare_deployment",
                           app=req.app.value, cloud=req.cloud.value,
                           env=req.env_name.value, customer=req.customer,
                           region=req.region or "",
                           ttl_hours=req.ttl_hours,
                           correlation_id=cid)

                if not out.get("ok"):
                    say("assistant",
                        f"**Could not prepare the deployment.**\n\n"
                        f"{out.get('reason', 'unknown')}")
                    st.session_state.stage = "idle"
                    st.rerun()

                st.session_state.branch = out["branch"]
                st.session_state.env_stem = out["env_stem"]
                st.session_state.pr_url = out["pr_url"]
                st.session_state.pr_number = out["pr_number"]
                say("assistant",
                    f"Opened [pull request #{out['pr_number']}]({out['pr_url']}) and "
                    f"started the plan. Building the image, planning the change, "
                    f"then the policy gate.")
                log({"event": "planning", "cid": cid, "pr": out["pr_url"],
                     "app": req.app.value, "env": out["env_stem"]})
                st.session_state.stage = "planning"
                st.rerun()
            except MCPUnavailable as err:
                say("assistant",
                    f"The tool server didn't answer.\n\n`{err}`\n\n"
                    f"Nothing was created.")
                st.session_state.stage = "idle"
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
        # The summary render_config already returned - no second computation
        # that could disagree with what the approver reviewed.
        s = st.session_state.summary or {}
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
                        # The apply succeeded, so this environment now exists.
                        # Merging makes main say so - until this, main had no
                        # record of anything that had been deployed, and the
                        # Environments list could not know about it.
                        if st.session_state.pr_number:
                            st.session_state.merge_note = g.merge_pr(
                                st.session_state.pr_number)
                            log({"event": "pr_merged",
                                 "cid": st.session_state.cid,
                                 "pr": st.session_state.pr_number,
                                 "result": st.session_state.merge_note})
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
        if st.session_state.merge_note:
            st.caption(st.session_state.merge_note)
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


# ===================================================== environments & audit ==
# Both panels are read-only views onto systems of record: the committed tfvars
# files, and GitHub's own run history. Neither invents state of its own.

ENVS_DIR = Path("envs")


def local_envs() -> list[dict]:
    """
    Registered environments, read from the committed tfvars files.

    A file here means the environment is *registered*, not necessarily that it
    is deployed - a destroyed environment keeps its tfvars so it can be stood
    up again. The destroy workflow fails loudly if the state is already empty,
    which is the honest way to find out.
    """
    import re

    rows = []
    if not ENVS_DIR.is_dir():
        return rows

    for f in sorted(ENVS_DIR.glob("*.tfvars")):
        text = f.read_text()

        def field(key, default=""):
            m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', text, re.M)
            return m.group(1) if m else default

        protected = bool(re.search(r'^\s*protected\s*=\s*true', text, re.M))
        rows.append({
            "stem": f.stem,
            "app": field("app"),
            "customer": field("customer"),
            "env": field("env_name"),
            "region": field("region"),
            "protected": protected,
            "service": f'{field("app")}-{field("customer")}-{field("env_name")}',
        })
    return rows


def environments_panel():
    # Through the tool layer, so this panel shows exactly what an outside
    # agent calling list_environments would see.
    try:
        out = mcp_client.call("list_environments")
        envs = [
            {"stem": e["env_stem"], "app": e["app"], "customer": e["customer"],
             "env": e["environment"], "region": e["region"],
             "protected": e["protected"], "service": e["service"]}
            for e in out.get("environments", [])
        ] if out.get("ok") else []
    except Exception as err:                                  # noqa: BLE001
        st.warning(f"Could not list environments — {err}")
        envs = local_envs()          # fall back to reading the files directly

    if not envs:
        st.caption("No environments registered yet.")
        return

    # Two tfvars files can describe the same app, customer and environment and
    # therefore the same service name, while keeping separate Terraform state.
    # The stem is the real identity - it is the state prefix, and it is what
    # the destroy button acts on - so the stem is the heading.
    seen = {}
    for e in envs:
        seen.setdefault(e["service"], []).append(e["stem"])

    for e in envs:
        c1, c2 = st.columns([4, 1])
        with c1:
            # The service name leads, because that is what a person thinks of
            # as "the thing that is running". The stem follows, because it is
            # the Terraform state prefix and therefore what Destroy acts on -
            # and when two environments collide, it is the only difference.
            st.markdown(f"**`{e['service']}`**")
            st.caption(
                f"env `{e['stem']}` · {e['app']} · {e['customer']} · "
                f"{e['env']} · {e['region']}"
                + (" · protected" if e["protected"] else "")
            )
            clash = [s for s in seen[e["service"]] if s != e["stem"]]
            if clash:
                st.caption(
                    f"⚠️ shares the service name `{e['service']}` with "
                    f"`{', '.join(clash)}` — deploying both would make them "
                    f"fight over one Cloud Run service from separate states."
                )
        with c2:
            if e["protected"]:
                st.button("Protected", key=f"d-{e['stem']}", disabled=True,
                          use_container_width=True)
            elif st.button("Destroy", key=f"d-{e['stem']}",
                           use_container_width=True):
                st.session_state.destroy_env = e
                st.session_state.stage = "destroy_confirm"
                st.rerun()

    st.caption(
        "A listed environment is one with a committed tfvars file. After a "
        "teardown the file stays, so the environment can be recreated - "
        "remove it in a pull request to retire it for good."
    )


def audit_panel():
    # A panel must never be able to take the page down with it. The audit
    # trail is the least important thing on screen and the only one that
    # needs the network on every render, so it fails quietly and in place.
    try:
        out = mcp_client.call("get_audit_trail", limit=25)
        if not out.get("ok"):
            st.warning(f"Audit trail unavailable — {out.get('reason')}")
            return
        rows = [
            {"action": e["action"], "env": e["environment"],
             "approver": e["approved_by"] or "", "actor": e["requested_by"] or "",
             "cid": e["correlation_id"], "conclusion": e["result"],
             "created_at": e["at"], "url": e["run"]}
            for e in out["entries"]
        ]
    except Exception as err:                                  # noqa: BLE001
        st.warning(f"Audit trail unavailable — {type(err).__name__}: {err}")
        return

    rows = [r for r in rows if r["action"]]
    if not rows:
        st.caption("No deployment activity yet.")
        return

    icon = {"success": "✅", "failure": "❌", "cancelled": "⬜"}
    table = []
    for r in rows[:25]:
        table.append({
            "": icon.get(r["conclusion"], "⏳"),
            "Action": r["action"],
            "Environment": r["env"],
            "Approved by": r["approver"] or "—",
            "Requested by": r["actor"] or "—",
            "Correlation": r["cid"],
            "When": (r["created_at"] or "")[:16].replace("T", " "),
            "Run": r["url"],
        })

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={"Run": st.column_config.LinkColumn("Run", display_text="open")},
    )
    st.caption(
        "Read from GitHub's run history. The approver is written into the run "
        "name by GitHub when the workflow is queued, so it cannot be altered "
        "afterwards - not by this agent, and not from this machine."
    )


# ---- 8. destroy: confirm --------------------------------------------------
if stage == "destroy_confirm":
    e = st.session_state.destroy_env
    with st.chat_message("assistant"):
        st.warning(f"**Destroy `{e['service']}`?**")
        st.caption(f"Environment `{e['stem']}` in `{e['region']}`.")
        st.caption(
            "This removes the Cloud Run service and everything else in that "
            "environment's Terraform state. Documents already written to "
            "Firestore are not removed by this."
        )
        typed = st.text_input(
            f"Type `{e['stem']}` to confirm", key="destroy_confirm_text")
        a, b = st.columns(2)
        approver = os.getenv("APPROVER", "operator")

        if a.button("Destroy environment", type="primary",
                    use_container_width=True,
                    disabled=(typed != e["stem"])):
            try:
                cid = f"{e['customer']}-{uuid.uuid4().hex[:6]}"
                st.session_state.destroy_cid = cid
                st.session_state.destroy_run = None
                gh().dispatch("destroy.yml", "main", {
                    "env_name": e["stem"],
                    "confirm": e["stem"],
                    "approver": approver,
                    "correlation_id": cid,
                })
                say("user", f"destroy {e['service']}")
                say("assistant", f"Teardown approved by **{approver}**.")
                log({"event": "destroy_requested", "cid": cid,
                     "env": e["stem"], "approver": approver})
                st.session_state.stage = "destroying"
                st.rerun()
            except GitHubError as err:
                st.error(str(err))

        if b.button("Cancel", use_container_width=True):
            st.session_state.destroy_env = None
            st.session_state.stage = "idle"
            st.rerun()

# ---- 9. destroy: running --------------------------------------------------
if stage == "destroying":
    with st.chat_message("assistant"):
        @st.fragment(run_every=4)
        def watch_destroy():
            try:
                g = gh()
                if st.session_state.destroy_run is None:
                    r = g.find_run("destroy.yml", st.session_state.destroy_cid,
                                   timeout=4)
                    if not r:
                        st.caption("waiting for the run to start…")
                        return
                    st.session_state.destroy_run = r.id

                run, failed = show_run(st.session_state.destroy_run, "Destroy")
                if run.finished:
                    if run.ok:
                        st.session_state.stage = "destroyed"
                    else:
                        st.session_state.failure = explain_failure(
                            st.session_state.destroy_run, failed)
                        st.session_state.stage = "destroy_failed"
                    st.rerun()
            except GitHubError as err:
                st.error(str(err))
        watch_destroy()

# ---- 10. destroy: outcome -------------------------------------------------
if stage == "destroyed":
    e = st.session_state.destroy_env or {}
    with st.chat_message("assistant"):
        st.success(f"**`{e.get('service','the environment')}` is gone.**")
        st.caption(
            "Terraform destroyed the resources and the workflow then confirmed "
            "with gcloud that the service no longer exists - a destroy that "
            "reports success without checking is not a teardown."
        )
        if st.button("Done", type="primary"):
            st.session_state.destroy_env = None
            st.session_state.stage = "idle"
            st.rerun()

if stage == "destroy_failed":
    step, lines = st.session_state.failure or ("unknown", [])
    with st.chat_message("assistant"):
        st.error(f"**Teardown failed at: {step}**")
        if lines:
            st.code("\n".join(lines), language="text")
        st.caption(
            "Resources may be partly removed. Run `./scripts/destroy.sh` "
            "locally - it sweeps by label rather than by Terraform state, so "
            "it catches whatever this left behind."
        )
        st.markdown(
            f"[Full log](https://github.com/{os.getenv('GITHUB_REPO','')}"
            f"/actions/runs/{st.session_state.destroy_run})")
        if st.button("Back", type="primary"):
            st.session_state.destroy_env = None
            st.session_state.stage = "idle"
            st.rerun()

# ---- panels, shown when nothing is in flight ------------------------------
if stage in ("idle", "clarify"):
    st.divider()
    t1, t2 = st.tabs(["Environments", "Audit trail"])
    with t1:
        environments_panel()
    with t2:
        audit_panel()
