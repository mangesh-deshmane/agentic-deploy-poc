"""
Exercise every MCP tool against the real repository, without a client.

    python -m mcp_server.selftest

Runs the server's own registry - the same list_tools / call_tool a client
would use - so a pass here means the protocol surface is sound, not merely
that the underlying Python works. Read-only by default; nothing is created.

Use it before a demo, and as the fallback if a client will not connect:
the output shows the tool surface and real answers from it.
"""

import asyncio
import json
import os
import sys

READ_TOOLS = [
    ("list_blueprints", {}, "what the platform can deploy"),
    ("check_supported", {"app": "app1", "cloud": "gcp"}, "a supported target"),
    ("check_supported", {"app": "app1", "cloud": "aws"},
     "AWS - expect a refusal with the reason"),
    ("check_supported", {"app": "app2", "cloud": "gcp", "region": "europe-west1"},
     "unlisted region - expect a refusal"),
    ("check_policy", {"min_instance_count": 1},
     "a warm instance - expect a denial"),
    ("check_policy", {"resource_type": "google_container_cluster"},
     "GKE - expect a denial with the cost reason"),
    ("check_policy", {"min_instance_count": 0, "memory": "512Mi",
                      "cpu": "1", "region": "us-central1"},
     "compliant values - expect a pass"),
    ("estimate_cost", {"app": "app1", "customer": "acme"}, "cost and why"),
    ("render_config", {"app": "app2", "customer": "acme"},
     "the tfvars, rendered and NOT committed"),
    ("list_environments", {}, "registered environments"),
    ("get_audit_trail", {"limit": 5}, "who approved what (needs GitHub)"),
]


def unwrap(result):
    """call_tool returns (content, structured) - take whichever carries data."""
    if isinstance(result, tuple) and len(result) > 1 and result[1] is not None:
        return result[1]
    content = getattr(result, "content", None) or (
        result[0] if isinstance(result, tuple) else result)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {"raw": str(result)[:300]}


async def main() -> int:
    from mcp_server.server import mcp

    print("=" * 68)
    print("  Pipeline Pilot MCP server - self test")
    print("=" * 68)

    tl = await mcp.list_tools()
    print(f"\nTOOLS ({len(tl)})")
    for t in tl:
        ro = bool(getattr(t.annotations, "read_only_hint", False)) if t.annotations else False
        print(f"  {'read ' if ro else 'WRITE'}  {t.name}")

    rl = await mcp.list_resources()
    print(f"\nRESOURCES ({len(rl)})")
    for r in rl:
        print(f"  {r.uri}")

    failures = []
    print("\n" + "-" * 68)
    for name, args, note in READ_TOOLS:
        arg_s = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "-"
        print(f"\n{name}({arg_s})\n  # {note}")
        try:
            data = unwrap(await mcp.call_tool(name, args))
        except Exception as err:                              # noqa: BLE001
            print(f"  ERROR {type(err).__name__}: {err}")
            failures.append(f"{name}: {err}")
            continue

        ok = data.get("ok")
        print(f"  ok={ok}")
        if data.get("reason"):
            print(f"  reason: {data['reason'][:140]}")
        for v in (data.get("violations") or [])[:3]:
            print(f"    - {v['field']} = {v['value']!r}")
            print(f"      {v['reason'][:110]}")
        for k in ("blueprints", "environments", "entries"):
            if k in data:
                print(f"  {k}: {len(data[k])}")
        if "monthly" in data:
            print(f"  monthly: {data['monthly']}")
        if "correlation_id" in data:
            print(f"  correlation_id: {data['correlation_id']}"
                  f"  stem: {data.get('env_stem')}  (nothing committed)")

    print("\n" + "=" * 68)
    if failures:
        print(f"  {len(failures)} tool(s) errored:")
        for f in failures:
            print(f"    - {f}")
        print("\n  get_audit_trail needs GITHUB_TOKEN and GITHUB_REPO in .env;")
        print("  the rest run offline.")
        return 1

    print("  All tools responded. The surface is sound.")
    print("\n  Note: prepare_deployment is NOT exercised here - it opens a")
    print("  real pull request. Call it from a client when you mean to.")
    return 0


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(asyncio.run(main()))
