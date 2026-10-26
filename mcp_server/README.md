# Pipeline Pilot MCP server

Exposes the platform's catalog, policy, pipeline and audit surface as MCP
tools. Additive: a separate process that imports the same agent modules the
Streamlit UI uses. If it fails, the UI does not notice.

## Install

    pip install "mcp>=2.2,<3"

## Run

    python -m mcp_server.server          # stdio

## Client config (Claude Code / Claude Desktop)

    {
      "mcpServers": {
        "pipeline-pilot": {
          "command": "python",
          "args": ["-m", "mcp_server.server"],
          "cwd": "/home/<you>/agentic-deploy-poc",
          "env": { "BASE_BRANCH": "mcp-demo" }
        }
      }
    }

`BASE_BRANCH` keeps deploy PRs off main. Credentials come from the repo's
`.env` (GITHUB_REPO, GITHUB_TOKEN), loaded at startup.

## Tools

| Tool | Access |
|---|---|
| list_blueprints | read |
| check_supported | read |
| list_environments | read |
| check_policy | read |
| estimate_cost | read |
| render_config | read - the tfvars, rendered and NOT committed |
| get_run_status | read |
| get_audit_trail | read |
| prepare_deployment | writes a branch and a PR; creates no infrastructure |

## Resources

- `pipeline-pilot://policy` - the gate's rules
- `pipeline-pilot://catalog` - the blueprints, and what is deliberately absent

## Not exposed, on purpose

No approval tool. No apply, destroy, IAM or secret access. No Terraform
authoring - `prepare_deployment` takes parameters and renders the tfvars from
a reviewed template server-side.

An agent can prepare a deployment and read its plan. Only a human can apply it.

## The UI uses these too

`agent/mcp_client.py` is a synchronous MCP client, so the Streamlit UI reaches
the platform through this same server. One interface, not two: whatever the UI
can do, an outside agent can do.

Each call opens a short-lived stdio session (~1.4s). That is the cost of not
holding a subprocess across Streamlit's reruns, and it is the right trade for
a handful of calls per deployment.

### What deliberately does NOT go through a tool

| | Why |
|---|---|
| Approving a plan | No approval tool exists. An agent can prepare; only a person here can apply. |
| Dispatching apply.yml | Follows from the above - it is the act of approving. |
| Dispatching destroy.yml | Destructive, and gated on a typed confirmation from a human. |
| Polling a run's steps | Performance, not principle: the watcher polls every 4s and a subprocess per poll would be absurd. |

The first three are the architecture. The fourth is an optimisation, and would
move behind `get_run_status` if the client ever held a persistent session.
