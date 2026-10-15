from typing import Any, Dict

from provider_adapters import estimate_cost


def choose_provider(
    task: Dict[str, Any],
    policy: Dict[str, Any],
    provider_status: Dict[str, Any],
    budget_ledger: Dict[str, Any],
) -> str:
    routing = policy.get("routing", {})
    task_type = task.get("type")
    task_intent = task.get("intent")

    if task_type == "shell":
        return "shell"

    preferred = routing.get("task_routes", {}).get(task_type, [])
    if task_intent:
        preferred = routing.get("intent_routes", {}).get(task_intent, preferred)

    if not preferred:
        preferred = routing.get("fallback_order", ["local"])

    limits = policy.get("budget", {})
    global_limit = float(limits.get("daily_global_limit", 0.0))
    spent_total = sum(float(v) for v in budget_ledger.get("spent", {}).values())

    for provider in preferred:
        status = provider_status.get(provider, {"ok": True})
        if not status.get("ok", True):
            continue

        estimated = estimate_cost(provider, task)
        if global_limit > 0 and spent_total + estimated > global_limit:
            continue
        return provider

    return routing.get("fallback_order", ["local"])[-1]
