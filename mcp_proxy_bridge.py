"""
mcp_proxy_bridge.py

Bridge from AI Broker to the standalone mcp_proxy (the "open connectivity"
path historically served by mcp_local_proxy.py on 127.0.0.1:3000).

mcp_proxy is an unmediated transport: it injects upstream auth headers and
forwards whatever it is given.  That is useful precisely because it keeps
working while the broker's policy is still being tuned, but it means every
call through this bridge is a call that bypassed the broker's own controls.

The bridge therefore exists only to serve --permissive mode, where a request
the broker would have rejected is forwarded anyway so that work continues and
the gap is recorded.  Every forward is audited as a fallback event, and that
audit trail is the worklist for tightening policy until --strict mode passes.

Hard rule, not configurable: shell tasks never fall back.  A denied shell
command falling through to an unmediated executor would defeat the entire
point of the broker, so the guard lives in code rather than in policy.json.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

DEFAULT_PROXY_URL = "http://127.0.0.1:3000"
DEFAULT_TIMEOUT = 30.0

# Task types that may never be forwarded to the open proxy, whatever the
# policy file says.
NEVER_FALLBACK_TASK_TYPES = {"shell", "fixture"}

# Task types eligible for fallback unless policy narrows it further.
DEFAULT_FALLBACK_TASK_TYPES = ("mcp",)


def fallback_config(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Read the fallback block from policy.json, with defaults applied."""
    config = policy.get("fallback") or {}
    proxy = config.get("mcp_proxy") or {}
    return {
        "enabled": bool(config.get("enabled", False)),
        "url": str(proxy.get("url", DEFAULT_PROXY_URL)).rstrip("/"),
        "timeout": float(proxy.get("timeout", DEFAULT_TIMEOUT)),
        "allow_task_types": list(
            config.get("allow_task_types", DEFAULT_FALLBACK_TASK_TYPES)
        ),
        "on_policy_denial": bool(config.get("on_policy_denial", True)),
        "on_execution_failure": bool(config.get("on_execution_failure", False)),
    }


def is_eligible(task: Dict[str, Any], config: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Decide whether a task may be forwarded to the open proxy at all."""
    if not config.get("enabled", False):
        return False, "fallback is disabled in policy"

    task_type = str(task.get("type", "") or "")

    if task_type in NEVER_FALLBACK_TASK_TYPES:
        return False, f"task type '{task_type}' may never use the open proxy"

    allowed = config.get("allow_task_types") or []
    if allowed and task_type not in allowed:
        return False, f"task type '{task_type}' is not configured for fallback"

    if not str(task.get("intent", "") or ""):
        return False, "fallback requires an intent to use as the MCP tool name"

    return True, None


def decode_mcp_response(raw: str) -> Dict[str, Any]:
    """Decode an MCP reply that may be plain JSON or SSE-framed.

    The OpenBrain Supabase endpoint answers tools/call with Content-Type
    text/event-stream, so the JSON-RPC object arrives as

        event: message
        data: {"result": ...}

    Callers that assume response.json() see a decode error instead of the
    result.  Accept both shapes.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty response body")

    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return decoded
    except json.JSONDecodeError:
        pass

    # SSE framing: take the last complete `data:` payload that parses, which
    # is the final message of the stream.
    last_error: Optional[Exception] = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        candidate = line[len("data:"):].strip()
        if not candidate or candidate == "[DONE]":
            continue
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(decoded, dict):
            return decoded

    raise ValueError(
        f"response was neither JSON nor SSE-framed JSON{f': {last_error}' if last_error else ''}"
    )


def _parse_mcp_response(data: Dict[str, Any], url: str) -> Dict[str, Any]:
    if "error" in data:
        err = data["error"]
        message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return {"ok": False, "error": f"MCP error via proxy: {message}"}

    result = data.get("result", {}) or {}
    content_blocks = result.get("content", []) or []
    text_out = "\n".join(
        block.get("text", "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )

    if result.get("isError"):
        return {"ok": False, "error": text_out or "MCP tool returned an error"}

    return {
        "ok": True,
        "result": text_out,
        "metadata": {
            "transport": "mcp_proxy",
            "proxy_url": url,
            "mediated": False,
        },
    }


def forward(task: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """POST the task to mcp_proxy as a JSON-RPC tools/call.

    The proxy supplies upstream credentials itself, so no token is read or
    sent from here.
    """
    url = config.get("url", DEFAULT_PROXY_URL)
    timeout = float(config.get("timeout", DEFAULT_TIMEOUT))

    rpc_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": task.get("intent", ""),
            "arguments": task.get("payload") or {},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        import httpx

        with httpx.Client(timeout=timeout) as http:
            response = http.post(url, json=rpc_body, headers=headers)
            response.raise_for_status()
            data = decode_mcp_response(response.text)
    except ImportError:
        import urllib.error
        import urllib.request

        body = json.dumps(rpc_body).encode()
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                data = decode_mcp_response(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"mcp_proxy HTTP {exc.code}: {exc.reason}"}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"mcp_proxy unreachable: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return {"ok": False, "error": f"mcp_proxy call failed: {exc}"}

    if not isinstance(data, dict):
        return {"ok": False, "error": "mcp_proxy returned a non-object response"}

    return _parse_mcp_response(data, url)
