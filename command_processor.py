"""Compatibility wrapper around the upgraded AI Broker.

Existing callers that send a shell-style JSON payload can continue to do so.
"""

from typing import Any, Dict

from ai_broker import AIBroker


def process_request(json_input: Dict[str, Any], user_role: str = "developer", cwd: str = ".") -> Dict[str, Any]:
    command = json_input.get("arguments", {}).get("command")
    task = {
        "type": "shell",
        "intent": "execute",
        "cwd": cwd,
        "payload": {"command": command},
    }
    broker = AIBroker()
    return broker.process_task(task, user_role=user_role)
