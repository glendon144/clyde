#!/usr/bin/env python3
"""
broker_cli.py

Command line front end for AI Broker.

    python3 broker_cli.py --strict run --type mcp --intent search_thoughts \
        --payload '{"query": "ai broker"}' --role maintainer

    python3 broker_cli.py --permissive run ...     # denied requests may fall
                                                   # back to mcp_proxy
    python3 broker_cli.py review ...               # decide, do not execute
    python3 broker_cli.py rules ...                # test admission rules only
    python3 broker_cli.py fallbacks                # the edge-case worklist
    python3 broker_cli.py status --admin

The intended workflow is: run permissive for a while so nothing breaks, then
read `fallbacks` to see exactly which requests the policy does not yet cover,
tighten policy.json, and switch to strict once the report is empty.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from ai_broker import (
    DEFAULT_AUDIT_LOG,
    DEFAULT_POLICY_PATH,
    ENFORCEMENT_PERMISSIVE,
    ENFORCEMENT_STRICT,
    AIBroker,
)
from policy_rules import RuleEngine, build_context

FALLBACK_EVENTS = {"fallback_to_mcp_proxy", "fallback_refused"}


def build_task(args: argparse.Namespace) -> Dict[str, Any]:
    if args.task_json:
        task = json.loads(args.task_json)
        if not isinstance(task, dict):
            raise SystemExit("--task-json must decode to an object")
        return task

    if not args.type:
        raise SystemExit("--type is required (or pass --task-json)")

    payload: Dict[str, Any] = {}
    if args.payload:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise SystemExit("--payload must decode to an object")
    if args.command:
        payload["command"] = args.command

    task: Dict[str, Any] = {"type": args.type, "payload": payload}
    if args.intent:
        task["intent"] = args.intent
    if args.cwd:
        task["cwd"] = args.cwd
    if args.user:
        task["user"] = args.user
    return task


def resolve_enforcement(args: argparse.Namespace) -> Optional[str]:
    if args.strict and args.permissive:
        raise SystemExit("--strict and --permissive are mutually exclusive")
    if args.strict:
        return ENFORCEMENT_STRICT
    if args.permissive:
        return ENFORCEMENT_PERMISSIVE
    return None


def make_broker(args: argparse.Namespace) -> AIBroker:
    return AIBroker(policy_path=args.policy, enforcement=resolve_enforcement(args))


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def cmd_run(args: argparse.Namespace) -> int:
    broker = make_broker(args)
    task = build_task(args)
    result = broker.process_task(task, user_role=args.role)
    emit(result)
    return 0 if result.get("ok") else 1


def cmd_review(args: argparse.Namespace) -> int:
    broker = make_broker(args)
    task = build_task(args)
    decision = broker.review_task(task, user_role=args.role)
    emit(decision)
    return 0 if decision.get("approved") else 1


def cmd_rules(args: argparse.Namespace) -> int:
    """Evaluate only the admission rules, without touching providers."""
    broker = make_broker(args)
    task = build_task(args)
    context = build_context(
        task=task,
        user_role=args.role,
        user=str(task.get("user") or broker.default_user),
        mode=broker.mode(),
        enforcement=broker.enforcement(),
        risk_tier=broker.task_risk_tier(task),
    )
    engine = RuleEngine(broker.policy.get("admission_rules"))
    decision = engine.evaluate(context)
    emit(
        {
            "context": context,
            "decision": decision.as_dict(),
            "configured": engine.configured,
            "rule_count": len(engine.rules),
            "load_errors": engine.load_errors,
        }
    )
    return 0 if decision.allowed else 1


def read_audit(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    entries: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def cmd_fallbacks(args: argparse.Namespace) -> int:
    """Summarise every request that needed the open proxy.

    Each row is a case the broker rejected but the user still needed, i.e. a
    policy gap to close before strict mode can be the steady state.
    """
    entries = [e for e in read_audit(args.audit_log) if e.get("event") in FALLBACK_EVENTS]

    if args.since:
        entries = [e for e in entries if str(e.get("timestamp", "")) >= args.since]

    if args.json:
        emit(entries)
        return 0

    if not entries:
        print("No fallback events recorded. Strict mode should be clean.")
        return 0

    grouped = Counter(
        (
            str(e.get("event")),
            str(e.get("task_type")),
            str(e.get("intent")),
            str(e.get("broker_stage")),
            str(e.get("broker_reason")),
        )
        for e in entries
    )

    print(f"{len(entries)} fallback event(s) across {len(grouped)} distinct case(s)\n")
    for (event, task_type, intent, stage, reason), count in grouped.most_common():
        marker = "FORWARDED" if event == "fallback_to_mcp_proxy" else "REFUSED  "
        print(f"[{marker}] x{count}  {task_type}/{intent}  (stage: {stage})")
        print(f"             broker said: {reason}\n")

    print("Each line above is a policy gap. Close it in policy.json, then re-run strict.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    broker = make_broker(args)
    report = broker.build_status_report(user_role=args.role, public=not args.admin)
    emit(report)
    return 0


def add_task_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", help="task type, e.g. shell, llm, mcp, ocr, dream")
    parser.add_argument("--intent", help="task intent; for MCP this is the tool name")
    parser.add_argument("--command", help="shell command, for --type shell")
    parser.add_argument("--payload", help="JSON object payload")
    parser.add_argument("--cwd", help="working directory for shell tasks")
    parser.add_argument("--user", help="requesting user (defaults to the process user)")
    parser.add_argument("--task-json", help="a complete task object as JSON")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="broker_cli.py",
        description="AI Broker command line interface",
    )
    parser.add_argument("--policy", default=DEFAULT_POLICY_PATH, help="path to policy.json")
    parser.add_argument("--role", default="developer", help="role to evaluate the request as")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="broker decision is final; denials are rejected and logged",
    )
    parser.add_argument(
        "--permissive",
        action="store_true",
        help="denied requests may fall back to mcp_proxy, and are logged as gaps",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="review and execute a task")
    add_task_arguments(run_parser)
    run_parser.set_defaults(func=cmd_run)

    review_parser = sub.add_parser("review", help="decide on a task without executing")
    add_task_arguments(review_parser)
    review_parser.set_defaults(func=cmd_review)

    rules_parser = sub.add_parser("rules", help="evaluate admission rules only")
    add_task_arguments(rules_parser)
    rules_parser.set_defaults(func=cmd_rules)

    fallback_parser = sub.add_parser("fallbacks", help="report requests that needed the open proxy")
    fallback_parser.add_argument("--audit-log", default=DEFAULT_AUDIT_LOG)
    fallback_parser.add_argument("--since", help="only events at or after this timestamp")
    fallback_parser.add_argument("--json", action="store_true", help="raw JSON output")
    fallback_parser.set_defaults(func=cmd_fallbacks)

    status_parser = sub.add_parser("status", help="print the broker status report")
    status_parser.add_argument("--admin", action="store_true", help="include internal detail")
    status_parser.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
