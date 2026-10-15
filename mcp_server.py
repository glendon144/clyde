#!/usr/bin/env python3
"""
mcp_server.py

AI Broker's inbound MCP face.

Until now the broker could only be called from Python (run_example.py, bsh).
This module gives it a front door that MCP clients such as Claude Desktop can
connect to, so that agent tool calls arrive *through* policy, budget, cache,
and audit instead of alongside them.

It is the inbound half of the mcp_proxy merge.  The FastMCP scaffolding is
adapted from mcp_proxy's clones_brain_mcp.py; the difference is that every
tool here resolves to broker.process_task() rather than to a direct upstream
call.

Run it:

    python3 mcp_server.py --strict                 # broker decision is final
    python3 mcp_server.py --permissive             # denials may reach mcp_proxy
    python3 mcp_server.py --transport sse --port 3002

Note on --role: MCP tasks are only permitted for the maintainer role in the
shipped policy.json, so that is the default here.  Narrowing this to a
dedicated, less privileged role is a sensible follow-up -- see README.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, Optional

from ai_broker import (
    DEFAULT_POLICY_PATH,
    ENFORCEMENT_PERMISSIVE,
    ENFORCEMENT_STRICT,
    AIBroker,
)


def _format(result: Dict[str, Any]) -> str:
    """Render a broker result for an MCP client.

    Denials are returned as readable text rather than raised, so the calling
    agent sees *why* it was refused and can adjust, and so the refusal is
    visible in the transcript instead of looking like a transport error.
    """
    if result.get("ok"):
        payload = result.get("result")
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
        assurance = result.get("assurance") or {}
        if assurance.get("requires_human_approval"):
            signals = ", ".join(
                str(item.get("code"))
                for item in assurance.get("signals", [])
                if isinstance(item, dict)
            ) or "unspecified assurance warning"
            text += (
                "\n\n[AI Broker assurance warning: human approval recommended; "
                f"{signals}. This output is not verified complete.]"
            )
        if result.get("fallback", {}).get("used"):
            note = (
                "\n\n[via mcp_proxy fallback -- this request did NOT pass broker "
                f"policy: {result['fallback'].get('broker_reason')}]"
            )
            return f"{text}{note}"
        return text

    stage = result.get("stage", "unknown")
    error = result.get("error", "no reason given")
    rule = result.get("matched_rule")
    lines = [f"DENIED at stage '{stage}': {error}"]
    if rule:
        lines.append(f"Matched admission rule: {rule}")
    return "\n".join(lines)


def build_server(broker: AIBroker, default_role: str):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("ai_broker")

    async def _submit(task: Dict[str, Any], role: Optional[str]) -> str:
        # The broker is synchronous; keep it off the event loop.
        result = await asyncio.to_thread(
            broker.process_task, task, user_role=role or default_role
        )
        return _format(result)

    @mcp.tool()
    async def broker_run(
        type: str,
        intent: str = "",
        payload: Optional[Dict[str, Any]] = None,
        cwd: str = "",
        role: str = "",
    ) -> str:
        """Submit any task to AI Broker for policy review and execution.

        type: shell, llm, mcp, ocr, or dream.
        intent: the task intent; for MCP tasks this is the upstream tool name.
        payload: task arguments; for shell tasks use {"command": "..."}.
        """
        task: Dict[str, Any] = {"type": type, "payload": payload or {}}
        if intent:
            task["intent"] = intent
        if cwd:
            task["cwd"] = cwd
        return await _submit(task, role)

    @mcp.tool()
    async def broker_review(
        type: str,
        intent: str = "",
        payload: Optional[Dict[str, Any]] = None,
        cwd: str = "",
        role: str = "",
    ) -> str:
        """Ask AI Broker whether a task would be allowed, without running it."""
        task: Dict[str, Any] = {"type": type, "payload": payload or {}}
        if intent:
            task["intent"] = intent
        if cwd:
            task["cwd"] = cwd
        decision = await asyncio.to_thread(
            broker.review_task, task, user_role=role or default_role
        )
        return json.dumps(decision, indent=2, sort_keys=True, default=str)

    @mcp.tool()
    async def broker_status() -> str:
        """Report broker policy, provider health, and budget state. Spends nothing."""
        report = await asyncio.to_thread(
            broker.build_status_report, default_role, True
        )
        return json.dumps(report, indent=2, sort_keys=True, default=str)

    # Convenience tools for the memory surface that mcp_proxy used to expose
    # directly.  Same tool names as before, now mediated.

    @mcp.tool()
    async def capture_thought(content: str, tags: Optional[list] = None) -> str:
        """Store a thought in OpenBrain, through broker policy."""
        payload: Dict[str, Any] = {"content": content}
        if tags:
            payload["tags"] = tags
        return await _submit({"type": "mcp", "intent": "capture_thought", "payload": payload}, None)

    @mcp.tool()
    async def search_thoughts(query: str, limit: int = 10) -> str:
        """Search OpenBrain semantically, through broker policy."""
        return await _submit(
            {
                "type": "mcp",
                "intent": "search_thoughts",
                "payload": {"query": query, "limit": limit},
            },
            None,
        )

    @mcp.tool()
    async def list_thoughts(limit: int = 20) -> str:
        """List recent OpenBrain thoughts, through broker policy."""
        return await _submit(
            {"type": "mcp", "intent": "list_thoughts", "payload": {"limit": limit}},
            None,
        )

    @mcp.tool()
    async def thought_stats() -> str:
        """Report OpenBrain statistics, through broker policy."""
        return await _submit(
            {"type": "mcp", "intent": "thought_stats", "payload": {}}, None
        )

    return mcp


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_server.py", description="AI Broker MCP server"
    )
    parser.add_argument("--policy", default=DEFAULT_POLICY_PATH)
    parser.add_argument(
        "--role",
        default="maintainer",
        help="role used for incoming tool calls (default: maintainer)",
    )
    parser.add_argument("--strict", action="store_true", help="broker decision is final")
    parser.add_argument(
        "--permissive",
        action="store_true",
        help="denied requests may fall back to mcp_proxy, and are logged as gaps",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse"],
        help="stdio for Claude Desktop; sse to listen on a port",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3002)
    args = parser.parse_args(argv)

    if args.strict and args.permissive:
        raise SystemExit("--strict and --permissive are mutually exclusive")
    enforcement = (
        ENFORCEMENT_STRICT
        if args.strict
        else ENFORCEMENT_PERMISSIVE
        if args.permissive
        else None
    )

    broker = AIBroker(policy_path=args.policy, enforcement=enforcement)

    try:
        server = build_server(broker, default_role=args.role)
    except ImportError:
        print(
            "The 'mcp' package is required for the MCP server.\n"
            "  pip install mcp",
            file=sys.stderr,
        )
        return 1

    # Announce on stderr so it does not corrupt the stdio JSON-RPC stream.
    print(
        f"AI Broker MCP server starting: enforcement={broker.enforcement()} "
        f"role={args.role} transport={args.transport}",
        file=sys.stderr,
    )

    if args.transport == "sse":
        import os

        os.environ.setdefault("FASTMCP_HOST", args.host)
        os.environ.setdefault("FASTMCP_PORT", str(args.port))
        server.run(transport="sse")
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
