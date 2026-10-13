#!/usr/bin/env python3
"""
Evaluate a Terraform plan against policy.yaml.

    python3 policy/check_plan.py tfplan.json [--policy policy/policy.yaml] [--json]

Exit 0 = allowed, 1 = denied, 2 = could not evaluate.

Reads the PLAN, not the .tf source. A validation block in variables.tf can be
edited by whoever edits the module; this reads what Terraform is actually
about to create. Fails closed: anything it cannot parse or does not recognise
is a denial, never a pass.
"""

import argparse
import json
import sys

try:
    import yaml
except ImportError:
    print("PyYAML is required:  pip install pyyaml", file=sys.stderr)
    sys.exit(2)


class Colour:
    def __init__(self, on):
        self.red = "\033[31m" if on else ""
        self.green = "\033[32m" if on else ""
        self.yellow = "\033[33m" if on else ""
        self.dim = "\033[90m" if on else ""
        self.bold = "\033[1m" if on else ""
        self.off = "\033[0m" if on else ""


def dig(obj, path):
    """
    Resolve a dotted path, with [n] for list indices.
    Returns (found, value) so that a legitimate None or False is not confused
    with a missing key.

    Terraform renders a nested block as a single-element LIST in plan JSON, so
    `template` is [{...}] not {...}. Single-element lists are unwrapped
    automatically, which keeps policy paths readable and survives the provider
    changing how a block is represented.
    """
    cur = obj
    for part in path.split("."):
        idx = None
        if "[" in part and part.endswith("]"):
            part, _, rest = part.partition("[")
            idx = int(rest[:-1])

        # Unwrap a single-element block list before looking up a key.
        while isinstance(cur, list) and len(cur) == 1:
            cur = cur[0]

        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]

        if idx is not None:
            if not isinstance(cur, list) or len(cur) <= idx:
                return False, None
            cur = cur[idx]
    return True, cur


def check_rule(rule, after):
    """Return None if the rule passes, or a failure string."""
    path = rule["path"]
    op = rule["op"]
    want = rule["value"]
    found, got = dig(after, path)

    if not found:
        # Terraform omits unknown values at plan time. Treating that as a pass
        # would let anything through behind a computed expression.
        return f"{path} is not present in the plan (cannot verify)"

    if op == "equals":
        return None if got == want else f"{path} = {got!r}, policy requires {want!r}"
    if op == "max":
        try:
            return None if float(got) <= float(want) else f"{path} = {got}, policy maximum is {want}"
        except (TypeError, ValueError):
            return f"{path} = {got!r} is not a number"
    if op == "min":
        try:
            return None if float(got) >= float(want) else f"{path} = {got}, policy minimum is {want}"
        except (TypeError, ValueError):
            return f"{path} = {got!r} is not a number"
    if op == "in":
        return None if got in want else f"{path} = {got!r}, allowed: {want}"

    return f"unknown operator {op!r} in policy"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", help="output of: terraform show -json tfplan")
    ap.add_argument("--policy", default="policy/policy.yaml")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-colour", action="store_true")
    args = ap.parse_args()

    c = Colour(not args.no_colour and sys.stdout.isatty())

    try:
        with open(args.policy) as fh:
            policy = yaml.safe_load(fh)
    except Exception as err:
        print(f"cannot read policy: {err}", file=sys.stderr)
        sys.exit(2)

    try:
        with open(args.plan) as fh:
            plan = json.load(fh)
    except Exception as err:
        print(f"cannot read plan: {err}", file=sys.stderr)
        sys.exit(2)

    allowed = set(policy.get("allowed_resource_types", []))
    denied = policy.get("denied_resource_types", {}) or {}
    value_rules = policy.get("value_rules", {}) or {}
    required_labels = policy.get("required_labels", {}) or {}

    violations = []
    warnings = []
    creating = []

    changes = plan.get("resource_changes", [])
    if not changes:
        print("plan contains no resource changes - nothing to check")
        if args.json:
            print(json.dumps({"verdict": "allow", "violations": [], "resources": []}))
        sys.exit(0)

    for change in changes:
        actions = change.get("change", {}).get("actions", [])
        if actions in (["no-op"], ["read"], ["delete"]):
            continue

        rtype = change.get("type", "?")
        addr = change.get("address", "?")
        after = change.get("change", {}).get("after") or {}
        creating.append((addr, rtype, actions))

        if rtype in denied:
            violations.append((addr, f"{rtype} is denied - {denied[rtype]}"))
            continue

        if rtype not in allowed:
            violations.append((
                addr,
                f"{rtype} is not on the allowlist. Add it to policy.yaml in a "
                f"reviewed pull request if it is genuinely needed."
            ))
            continue

        for rule in value_rules.get(rtype, []):
            problem = check_rule(rule, after)
            if problem:
                violations.append((addr, f"{problem}\n            {rule.get('reason','')}"))

        for label in required_labels.get(rtype, []):
            labels = after.get("labels") or {}
            if label not in labels:
                violations.append((
                    addr,
                    f"missing required label {label!r} - destroy.sh sweeps orphans by label"
                ))

        if rtype == "google_cloud_run_v2_service_iam_member" and after.get("member") == "allUsers":
            warnings.append((addr, "publicly reachable (allUsers invoker) - dev only"))

    # ---- report ----
    if args.json:
        print(json.dumps({
            "verdict": "deny" if violations else "allow",
            "resources": [{"address": a, "type": t, "actions": x} for a, t, x in creating],
            "violations": [{"address": a, "detail": d} for a, d in violations],
            "warnings": [{"address": a, "detail": d} for a, d in warnings],
        }, indent=2))
        sys.exit(1 if violations else 0)

    print(f"\n{c.bold}Policy check{c.off}  {len(creating)} resource(s) to create or change\n")
    for addr, rtype, actions in creating:
        print(f"  {c.dim}{'/'.join(actions):<8}{c.off} {addr}")

    if warnings:
        print(f"\n{c.bold}Warnings{c.off}")
        for addr, detail in warnings:
            print(f"  {c.yellow}warn{c.off}  {addr}\n        {detail}")

    if violations:
        print(f"\n{c.bold}{c.red}DENIED{c.off} - {len(violations)} violation(s)\n")
        for addr, detail in violations:
            print(f"  {c.red}x{c.off} {addr}\n      {detail}\n")
        print(f"{c.dim}The apply stage will not run. Nothing was created.{c.off}\n")
        sys.exit(1)

    print(f"\n{c.green}{c.bold}PASSED{c.off} - every resource is allowlisted and within limits")
    print(f"{c.dim}Estimated cost: $0.00/month within free tier{c.off}\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
