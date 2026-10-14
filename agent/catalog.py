"""
Catalog lookup. Entirely deterministic - no LLM involved.

This is where "deploy App1 on AWS" is rejected. The agent cannot invent a
blueprint it has never seen, and the rejection carries a specific reason
rather than a generic failure.
"""

from functools import lru_cache
from pathlib import Path

import yaml

from .schema import DeployRequest, Resolution

CATALOG_PATH = Path(__file__).resolve().parents[1] / "catalog" / "blueprints.yaml"


@lru_cache(maxsize=1)
def load() -> dict:
    with open(CATALOG_PATH) as fh:
        return yaml.safe_load(fh)


def available_blueprints() -> list[dict]:
    cat = load()
    return [
        {"key": k, **v}
        for k, v in cat["blueprints"].items()
        if v.get("available", True)
    ]


def describe_options() -> str:
    """Human-readable list of what can actually be deployed, for rejections."""
    lines = []
    for bp in available_blueprints():
        lines.append(
            f"  {bp['app']} on {bp['cloud']}  -  {bp['display_name']}"
            f" ({bp['language']}, {bp['platform']})"
        )
    unavailable = [
        f"  {v['app']} on {v['cloud']}  -  defined but not yet implemented"
        for v in load()["blueprints"].values()
        if not v.get("available", True)
    ]
    return "\n".join(lines + unavailable)


def resolve(req: DeployRequest) -> Resolution:
    """
    Map a request to a blueprint. Fails closed: anything not explicitly
    present is rejected.
    """
    cat = load()
    key = f"{req.app.value}-{req.cloud.value}"

    bp = cat["blueprints"].get(key)
    if bp is None:
        reason = cat.get("known_unsupported", {}).get(
            key,
            f"No blueprint registered for {req.app.value} on {req.cloud.value}.",
        )
        return Resolution(
            ok=False,
            reason=reason,
            available_alternatives=[
                f"{b['app']} on {b['cloud']}" for b in available_blueprints()
            ],
        )

    if not bp.get("available", True):
        return Resolution(
            ok=False,
            reason=(
                f"{bp['display_name']} is defined in the catalog but its "
                f"Terraform module is not built yet."
            ),
            available_alternatives=[
                f"{b['app']} on {b['cloud']}" for b in available_blueprints()
            ],
        )

    if req.region and req.region not in bp["regions"]:
        return Resolution(
            ok=False,
            reason=(
                f"Region {req.region} is not allowlisted for {key}. "
                f"Allowed: {', '.join(bp['regions'])}."
            ),
        )

    return Resolution(ok=True, blueprint_key=key)


def blueprint(key: str) -> dict:
    return {"key": key, **load()["blueprints"][key]}


def defaults() -> dict:
    return load()["defaults"]


def env_config(env_name: str) -> dict:
    return load()["environments"][env_name]
