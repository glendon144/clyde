"""
policy_rules.py

Deny-by-default admission rules for AI Broker.

Rules are single-expression predicates written in a deliberately small subset
of Python.  The subset is enforced at parse time by an AST whitelist: no
imports, no attribute access, no lambdas, no comprehensions, no assignment, no
f-strings, and no calls other than the fixed helper set defined below.

This lets policy.json carry statements like

    user == "gross" and task_type == "mcp"

without policy.json becoming a place where arbitrary code can run.  The policy
file is normally written by an administrator, so the whitelist is mostly
defence in depth -- but the broker is precisely the component that should not
trust its inputs more than it has to.

Evaluation is deny-by-default.  A request is allowed only when some rule
matches and that rule's decision is "allow".  A ruleset that matches nothing
denies.  A rule that raises is skipped and recorded, which also ends in deny.

Note on regular expressions: matches() compiles patterns from the policy file.
Python's re has no execution timeout, so a pathological pattern can burn CPU.
Patterns are length-capped and cached; treat policy.json as trusted input.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Any, Dict, List, Optional, Tuple

MAX_EXPRESSION_CHARS = 2000
MAX_EXPRESSION_NODES = 400
MAX_REGEX_CHARS = 500

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
VALID_DECISIONS = {DECISION_ALLOW, DECISION_DENY}


class RuleError(Exception):
    """Raised when a rule is malformed or evaluates unsafely."""


# --------------------------------------------------------------------------
# Helper functions available inside rule expressions.
# --------------------------------------------------------------------------

_REGEX_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _fn_startswith(value: Any, prefix: Any) -> bool:
    return _as_text(value).startswith(_as_text(prefix))


def _fn_endswith(value: Any, suffix: Any) -> bool:
    return _as_text(value).endswith(_as_text(suffix))


def _fn_contains(haystack: Any, needle: Any) -> bool:
    if isinstance(haystack, (list, tuple, set, dict)):
        return needle in haystack
    return _as_text(needle) in _as_text(haystack)


def _fn_matches(value: Any, pattern: Any) -> bool:
    text_pattern = _as_text(pattern)
    if len(text_pattern) > MAX_REGEX_CHARS:
        raise RuleError(f"Regex pattern exceeds {MAX_REGEX_CHARS} characters")
    compiled = _REGEX_CACHE.get(text_pattern)
    if compiled is None:
        try:
            compiled = re.compile(text_pattern)
        except re.error as exc:
            raise RuleError(f"Invalid regex {text_pattern!r}: {exc}") from exc
        _REGEX_CACHE[text_pattern] = compiled
    return compiled.fullmatch(_as_text(value)) is not None


def _fn_lower(value: Any) -> str:
    return _as_text(value).lower()


def _fn_upper(value: Any) -> str:
    return _as_text(value).upper()


def _fn_len(value: Any) -> int:
    try:
        return len(value)
    except TypeError as exc:
        raise RuleError(f"len() is not defined for {type(value).__name__}") from exc


def _fn_any_of(value: Any, options: Any) -> bool:
    if not isinstance(options, (list, tuple, set)):
        raise RuleError("any_of() expects a list as its second argument")
    return value in options


def _fn_path_under(path: Any, base: Any) -> bool:
    """True when path resolves to base or something beneath it."""
    resolved = os.path.realpath(os.path.expanduser(_as_text(path)))
    parent = os.path.realpath(os.path.expanduser(_as_text(base)))
    return resolved == parent or resolved.startswith(parent + os.sep)


def _fn_get(mapping: Any, key: Any, default: Any = None) -> Any:
    if isinstance(mapping, dict):
        return mapping.get(key, default)
    raise RuleError("get() expects a mapping as its first argument")


SAFE_FUNCTIONS = {
    "startswith": _fn_startswith,
    "endswith": _fn_endswith,
    "contains": _fn_contains,
    "matches": _fn_matches,
    "lower": _fn_lower,
    "upper": _fn_upper,
    "len": _fn_len,
    "any_of": _fn_any_of,
    "path_under": _fn_path_under,
    "get": _fn_get,
}


# --------------------------------------------------------------------------
# Expression whitelist.
# --------------------------------------------------------------------------

_ALLOWED_NODES: Tuple[type, ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Call,
    ast.Subscript,
)


def _validate_tree(tree: ast.AST) -> None:
    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if node_count > MAX_EXPRESSION_NODES:
            raise RuleError(f"Expression exceeds {MAX_EXPRESSION_NODES} nodes")

        if not isinstance(node, _ALLOWED_NODES):
            raise RuleError(
                f"Expression uses unsupported syntax: {type(node).__name__}"
            )

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise RuleError("Only direct calls to named helpers are allowed")
            if node.func.id not in SAFE_FUNCTIONS:
                raise RuleError(f"Unknown function '{node.func.id}'")
            if node.keywords:
                raise RuleError("Keyword arguments are not supported in rules")

        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            raise RuleError("Assignment is not allowed in rules")


class SafeExpression:
    """A single rule predicate, parsed and whitelisted at construction."""

    def __init__(self, source: str):
        if not isinstance(source, str) or not source.strip():
            raise RuleError("Rule expression must be a non-empty string")
        if len(source) > MAX_EXPRESSION_CHARS:
            raise RuleError(
                f"Rule expression exceeds {MAX_EXPRESSION_CHARS} characters"
            )
        self.source = source
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise RuleError(f"Rule expression is not valid syntax: {exc}") from exc
        _validate_tree(tree)
        self._code = compile(tree, filename="<policy-rule>", mode="eval")

    def evaluate(self, context: Dict[str, Any]) -> bool:
        names = dict(SAFE_FUNCTIONS)
        names.update(context)
        try:
            result = eval(self._code, {"__builtins__": {}}, names)  # noqa: S307
        except RuleError:
            raise
        except NameError as exc:
            raise RuleError(f"Rule refers to an unknown value: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - surfaced as a rule error
            raise RuleError(f"Rule evaluation failed: {exc}") from exc
        return bool(result)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SafeExpression({self.source!r})"


# --------------------------------------------------------------------------
# Rules and the engine that walks them.
# --------------------------------------------------------------------------


class Rule:
    def __init__(self, spec: Dict[str, Any], index: int):
        if not isinstance(spec, dict):
            raise RuleError(f"Rule #{index} must be an object")
        self.name = str(spec.get("name") or f"rule_{index}")
        decision = str(spec.get("decision", DECISION_DENY)).lower()
        if decision not in VALID_DECISIONS:
            raise RuleError(
                f"Rule '{self.name}' has invalid decision '{decision}'; "
                f"expected one of {sorted(VALID_DECISIONS)}"
            )
        self.decision = decision
        self.reason = str(spec.get("reason") or f"matched rule '{self.name}'")
        self.enabled = bool(spec.get("enabled", True))
        self.expression = SafeExpression(str(spec.get("when", "")))

    @property
    def allows(self) -> bool:
        return self.decision == DECISION_ALLOW


class RuleDecision:
    def __init__(
        self,
        allowed: bool,
        reason: str,
        rule: Optional[str] = None,
        errors: Optional[List[str]] = None,
    ):
        self.allowed = allowed
        self.reason = reason
        self.rule = rule
        self.errors = errors or []

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "allowed": self.allowed,
            "reason": self.reason,
            "rule": self.rule,
        }
        if self.errors:
            payload["rule_errors"] = self.errors
        return payload


class RuleEngine:
    """Ordered, first-match-wins, deny-by-default admission rules.

    Construction never raises on a bad individual rule: the rule is recorded in
    load_errors and dropped.  A ruleset that fails to load therefore denies
    everything rather than silently allowing it.
    """

    def __init__(self, config: Optional[Dict[str, Any]]):
        self.configured = bool(config)
        config = config or {}
        self.default = str(config.get("default", DECISION_DENY)).lower()
        if self.default not in VALID_DECISIONS:
            self.default = DECISION_DENY
        self.load_errors: List[str] = []
        self.rules: List[Rule] = []

        for index, spec in enumerate(config.get("rules", []) or []):
            try:
                rule = Rule(spec, index)
            except RuleError as exc:
                name = spec.get("name") if isinstance(spec, dict) else f"#{index}"
                self.load_errors.append(f"{name}: {exc}")
                continue
            if rule.enabled:
                self.rules.append(rule)

    def evaluate(self, context: Dict[str, Any]) -> RuleDecision:
        if not self.configured:
            # No ruleset in policy.json: this gate is not in use, so it does
            # not participate in the decision at all.  The broker's role,
            # provider, budget, and agent checks still apply.
            return RuleDecision(True, "admission rules not configured", rule=None)

        errors = list(self.load_errors)

        for rule in self.rules:
            try:
                matched = rule.expression.evaluate(context)
            except RuleError as exc:
                errors.append(f"{rule.name}: {exc}")
                continue
            if matched:
                return RuleDecision(
                    allowed=rule.allows,
                    reason=rule.reason,
                    rule=rule.name,
                    errors=errors,
                )

        if self.default == DECISION_ALLOW:
            return RuleDecision(True, "no rule matched; default is allow", None, errors)
        return RuleDecision(
            False,
            "no admission rule matched this request (deny by default)",
            None,
            errors,
        )


def build_context(
    task: Dict[str, Any],
    user_role: str,
    user: str,
    mode: str,
    enforcement: str,
    risk_tier: str,
) -> Dict[str, Any]:
    """Flatten a task into the names a rule expression may refer to."""
    payload = task.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    command = str(payload.get("command", "") or "")
    argv = command.split()

    provider = str(task.get("provider", "") or "")
    mcp_server = provider[len("mcp_"):] if provider.startswith("mcp_") else ""

    return {
        "user": user,
        "role": user_role,
        "task_type": str(task.get("type", "") or ""),
        "intent": str(task.get("intent", "") or ""),
        "provider": provider,
        "mcp_server": mcp_server,
        "command": command,
        "argv": argv,
        "cwd": os.path.abspath(str(task.get("cwd", ".") or ".")),
        "risk_tier": risk_tier,
        "mode": mode,
        "enforcement": enforcement,
        "agent_count": int(task.get("agent_count", 0) or 0),
        "payload": payload,
        "payload_keys": sorted(payload.keys()),
    }
