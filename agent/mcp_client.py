"""
A synchronous MCP client, so the Streamlit UI can reach the platform through
the same tool layer an outside agent uses.

Why this exists: today the UI imports service.py and an outside agent goes
through MCP, which is two callers of one implementation. They can drift. If
the UI speaks MCP too, there is one interface by construction - whatever the
UI can do, a customer's agent can do.

Why it is synchronous: Streamlit re-executes the whole script on every
interaction and has no event loop of its own. The MCP SDK is async, so each
call opens a short-lived stdio session inside asyncio.run(). That costs an
interpreter start per call - a second or so - and buys immunity from the
subprocess-lifetime problem a persistent session would bring. For a handful
of calls per deployment that is the right trade. Revisit it if the UI starts
making calls in a loop.

Nothing here holds a cloud credential; the server it spawns does not either.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 45.0


class MCPUnavailable(Exception):
    """The tool server could not be reached or did not answer in time."""


def _params():
    from mcp.client.stdio import StdioServerParameters

    # The same interpreter that is running the UI, so the server sees the
    # same virtualenv without anyone having to configure a path.
    env = dict(os.environ)
    env.setdefault("BASE_BRANCH", os.getenv("BASE_BRANCH", "main"))

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=str(ROOT),
        env=env,
    )


def _payload(result: Any) -> dict:
    """
    Pull the dict back out of a CallToolResult.

    Prefers structuredContent when the server provides it, and falls back to
    parsing the text block. A tool that returned neither is a bug worth
    seeing rather than hiding, so it surfaces as a dict with "raw".
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        # MCPServer wraps a bare return value under "result".
        if set(structured.keys()) == {"result"} and isinstance(
                structured["result"], dict):
            return structured["result"]
        return structured

    for block in (getattr(result, "content", None) or []):
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"ok": False, "reason": "tool did not return JSON",
                        "raw": text[:400]}

    return {"ok": False, "reason": "tool returned no content",
            "raw": str(result)[:400]}


async def _call_async(name: str, args: dict, timeout: float) -> dict:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                name, args, read_timeout_seconds=timeout)
            return _payload(result)


async def _list_async(timeout: float) -> list[dict]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            out = []
            for t in listed.tools:
                ro = bool(getattr(t.annotations, "read_only_hint", False)) \
                    if t.annotations else False
                out.append({"name": t.name, "read_only": ro,
                            "description": (t.description or "")[:160]})
            return out


def _run(coro):
    """
    asyncio.run, but survives being called from a thread that already has a
    loop - which Streamlit sometimes does.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError as err:
        if "already running" not in str(err).lower():
            raise
        import threading

        box: dict = {}

        def worker():
            try:
                box["value"] = asyncio.run(coro)
            except BaseException as e:                        # noqa: BLE001
                box["error"] = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join()
        if "error" in box:
            raise box["error"]
        return box["value"]


def call(name: str, timeout: float = DEFAULT_TIMEOUT, **args) -> dict:
    """
    Call one tool and return its dict.

    Transport failures become MCPUnavailable; a tool that ran and refused
    returns {"ok": False, "reason": ...} like any other answer, because a
    refusal is data the caller should show, not an error it should swallow.
    """
    clean = {k: v for k, v in args.items() if v is not None and v != ""}
    try:
        return _run(_call_async(name, clean, timeout))
    except MCPUnavailable:
        raise
    except Exception as err:                                  # noqa: BLE001
        raise MCPUnavailable(
            f"could not reach the tool server for {name!r}: {err}"
        ) from err


def list_tools(timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    try:
        return _run(_list_async(timeout))
    except Exception as err:                                  # noqa: BLE001
        raise MCPUnavailable(f"could not list tools: {err}") from err


def available() -> bool:
    """Whether the tool server answers at all. For a status indicator."""
    try:
        return len(list_tools(timeout=20.0)) > 0
    except MCPUnavailable:
        return False


__all__ = ["call", "list_tools", "available", "MCPUnavailable"]
