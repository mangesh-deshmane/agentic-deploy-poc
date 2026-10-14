"""
Groq client with a hard rule: the model's output is never trusted.

Everything it returns is validated against a Pydantic schema. If validation
fails, we retry once with the error fed back; if that fails, the caller falls
back to a manual form. A demo must never hard-fail because an LLM was having
a bad day.
"""

import json
import os
import time
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Free tier: 30 req/min, 1,000 req/day, 12k tokens/min. A deployment uses
# 3-5 calls, so the ceiling is roughly 200 deploys a day.
MODEL_INTENT = os.getenv("GROQ_MODEL_INTENT", "llama-3.3-70b-versatile")
MODEL_FAST = os.getenv("GROQ_MODEL_FAST", "llama-3.1-8b-instant")


class LLMUnavailable(Exception):
    """Raised when the model cannot be reached or keeps returning nonsense."""


_client = None


def client():
    global _client
    if _client is None:
        from groq import Groq

        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise LLMUnavailable("GROQ_API_KEY is not set. Add it to .env")
        _client = Groq(api_key=key)
    return _client


def complete_json(
    system: str,
    user: str,
    model_cls: Type[T],
    model: Optional[str] = None,
    retries: int = 1,
) -> T:
    """
    Ask for JSON matching model_cls. Validates, retries once with the
    validation error appended, then gives up so the caller can fall back.
    """
    model = model or MODEL_INTENT
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = client().chat.completions.create(
                model=model,
                messages=messages,
                # Deterministic: the same request should resolve the same way
                # every time, which matters when demoing.
                temperature=0,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content
            return model_cls.model_validate_json(raw)

        except ValidationError as err:
            last_error = err
            # Feed the failure back rather than retrying blind.
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (
                    "That did not match the required schema. Fix these errors "
                    f"and return ONLY valid JSON:\n{err}"
                ),
            })

        except json.JSONDecodeError as err:
            last_error = err
            messages.append({
                "role": "user",
                "content": "That was not valid JSON. Return ONLY a JSON object.",
            })

        except Exception as err:
            msg = str(err).lower()
            if "rate" in msg or "429" in msg:
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise LLMUnavailable(
                    "Groq rate limit reached. Use the manual form below."
                ) from err
            raise LLMUnavailable(f"Groq request failed: {err}") from err

    raise LLMUnavailable(f"Model did not produce valid output: {last_error}")


def summarise(system: str, user: str, max_tokens: int = 300) -> str:
    """
    Plain-text completion for the SRE agent. Uses the small model to conserve
    the 12k tokens-per-minute budget.
    """
    try:
        resp = client().chat.completions.create(
            model=MODEL_FAST,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()
    except Exception as err:
        raise LLMUnavailable(str(err)) from err


def available() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))
