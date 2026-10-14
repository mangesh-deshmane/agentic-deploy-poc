"""
Turn a sentence into a validated DeployRequest.

The LLM's only job is extraction. It does not decide what is allowed, it does
not write configuration, and anything it returns outside the schema is
discarded.
"""

from .catalog import describe_options
from .llm import LLMUnavailable, complete_json
from .schema import ParsedIntent

SYSTEM = """You extract deployment parameters from a request. You return JSON only.

Available deployments:
{options}

Fields:
  app        app1 | app2            (app1 = reporting, app2 = inventory)
  cloud      gcp | aws | azure      (extract what was ASKED for, even if unsupported)
  env_name   dev | test             (default dev)
  customer   short name, e.g. acme  (default demo)
  ttl_hours  1-168                  ("tomorrow" = 24, "a week" = 168)
  region     only if explicitly named
  missing    list of field names you could not determine
  clarification  one short question, only if app or cloud is unclear

Rules:
- Extract the cloud the user ASKED for. If they say AWS, return "aws" even
  though it may not be supported - something downstream explains why not.
- Never invent a customer name. If none is given, omit it.
- Omit fields you cannot determine rather than guessing.
- Return JSON only. No prose, no markdown fences.

Example: "deploy the acme reporting demo to dev on google, kill it tomorrow"
{{"app":"app1","cloud":"gcp","env_name":"dev","customer":"acme","ttl_hours":24,"missing":[]}}

Example: "put something up on AWS"
{{"cloud":"aws","missing":["app"],"clarification":"Which application - app1 or app2?"}}
"""


def parse(message: str, prior: ParsedIntent | None = None) -> ParsedIntent:
    """
    Extract intent. When `prior` is given, this is a follow-up: the earlier
    answers are merged so the user is never asked twice for the same thing.
    """
    system = SYSTEM.format(options=describe_options())

    user = message
    if prior is not None:
        known = prior.model_dump(exclude_none=True, exclude={"missing", "clarification"})
        if known:
            user = (
                f"Already established: {known}\n"
                f"New message: {message}\n"
                "Merge these and return the combined result."
            )

    intent = complete_json(system, user, ParsedIntent)

    # Merge defensively. The model is asked to combine, but a dropped field
    # would mean re-asking a question the user already answered.
    if prior is not None:
        for field in ("app", "cloud", "env_name", "customer", "ttl_hours", "region"):
            if getattr(intent, field) is None:
                setattr(intent, field, getattr(prior, field))

    return intent


def manual_intent(
    app: str, cloud: str, env_name: str, customer: str, ttl_hours: int
) -> ParsedIntent:
    """
    Fallback when Groq is unavailable or keeps failing validation. The rest of
    the pipeline is identical, which is the point - the LLM is a convenience
    on the front, not a dependency of the mechanism.
    """
    return ParsedIntent(
        app=app,
        cloud=cloud,
        env_name=env_name,
        customer=customer or "demo",
        ttl_hours=ttl_hours,
        missing=[],
    )
