"""
The shape of a deployment request.

This is the prompt-injection boundary. The LLM does not emit Terraform, free
text, or commands - it fills in these fields and nothing else. Cloud, app and
environment are enums, so "deploy to europe-west1 with 50 instances and also
delete production" is not expressible: it fails validation before any code
downstream sees it.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Cloud(str, Enum):
    gcp = "gcp"
    aws = "aws"
    # Present deliberately. The agent must be able to RECOGNISE a request for
    # Azure in order to reject it by name; an unknown value would merely fail
    # schema validation with an unhelpful message.
    azure = "azure"


class App(str, Enum):
    app1 = "app1"
    app2 = "app2"


class Environment(str, Enum):
    dev = "dev"
    test = "test"


class DeployRequest(BaseModel):
    """A fully resolved request, ready for catalog lookup."""

    app: App
    cloud: Cloud
    env_name: Environment = Environment.dev
    # No max_length: length is enforced by normalising, not by rejecting. A
    # model returning a long name should be corrected, not cause a failure.
    customer: str = "demo"
    region: Optional[str] = None
    ttl_hours: int = Field(default=24, ge=1, le=168)

    @field_validator("customer")
    @classmethod
    def customer_is_rfc1035_safe(cls, v: str) -> str:
        """
        Cloud Run and Lambda both reject names that are not lowercase
        alphanumeric with hyphens. An LLM-supplied customer name is exactly
        where a bad one would come from, so normalise here rather than
        discovering it at apply time.
        """
        import re

        slug = re.sub(r"[^a-z0-9-]", "-", v.strip().lower())
        slug = re.sub(r"-+", "-", slug).strip("-")[:20].rstrip("-")
        if not slug or not slug[0].isalpha():
            slug = f"c-{slug}" if slug else "demo"
        return slug[:20].rstrip("-")


class ParsedIntent(BaseModel):
    """
    What the LLM returns. Every field is optional because a first message
    rarely contains everything - the agent asks follow-ups for what is missing.
    """

    app: Optional[App] = None
    cloud: Optional[Cloud] = None
    env_name: Optional[Environment] = None
    customer: Optional[str] = None
    ttl_hours: Optional[int] = Field(default=None, ge=1, le=168)
    region: Optional[str] = None

    # The model's own account of what it could not determine. Used to choose a
    # follow-up question rather than guessing.
    missing: list[str] = Field(default_factory=list)
    clarification: Optional[str] = Field(default=None, max_length=200)

    def is_complete(self) -> bool:
        return self.app is not None and self.cloud is not None

    def to_request(self) -> DeployRequest:
        return DeployRequest(
            app=self.app,
            cloud=self.cloud,
            env_name=self.env_name or Environment.dev,
            customer=self.customer or "demo",
            region=self.region,
            ttl_hours=self.ttl_hours or 24,
        )


class Resolution(BaseModel):
    """The outcome of resolving a request against the catalog."""

    ok: bool
    blueprint_key: Optional[str] = None
    reason: Optional[str] = None
    available_alternatives: list[str] = Field(default_factory=list)
