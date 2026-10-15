import getpass
import hashlib
import json
import os
import shlex
import time
from typing import Any, Dict, List, Optional, Tuple

import mcp_proxy_bridge
from epistemic_validator import assess_output
from policy_rules import RuleEngine, build_context
from provider_adapters import call_provider, estimate_cost
from router import choose_provider


DEFAULT_POLICY_PATH = "policy.json"
DEFAULT_STATE_DIR = "state"
DEFAULT_CACHE_PATH = os.path.join("cache", "responses.json")
DEFAULT_BUDGET_LEDGER = os.path.join(DEFAULT_STATE_DIR, "budget_ledger.json")
DEFAULT_PROVIDER_STATUS = os.path.join(DEFAULT_STATE_DIR, "provider_status.json")
DEFAULT_AUDIT_LOG = os.path.join(DEFAULT_STATE_DIR, "audit_log.jsonl")
DEFAULT_STATUS_REPORT = os.path.join(DEFAULT_STATE_DIR, "status_report.json")

STATUS_BLOCKED_VALUES = {"blocked", "disabled", "disabled_by_policy", "banned"}

# Enforcement modes.
#
#   strict      - the broker's decision is final.  A rejected request is
#                 rejected and logged.  This is the mode that surfaces the
#                 edge cases policy still needs to cover.
#   permissive  - a rejected request may be forwarded to the standalone
#                 mcp_proxy so work continues, with every such forward logged
#                 as a fallback event.  Use it to build the worklist, not as a
#                 steady state.
ENFORCEMENT_STRICT = "strict"
ENFORCEMENT_PERMISSIVE = "permissive"
VALID_ENFORCEMENT_MODES = {ENFORCEMENT_STRICT, ENFORCEMENT_PERMISSIVE}


SAFE_SHELL_METACHARS = set(";&|`$<>\n\r")
READ_ONLY_DENY_COMMANDS = {
    "rm", "rmdir", "mv", "cp", "touch", "mkdir", "chmod", "chown", "chgrp",
    "dd", "tee", "truncate", "python", "python3", "perl", "ruby", "bash", "sh",
    "zsh", "fish", "pip", "pip3", "git", "curl", "wget",
}
READ_ONLY_DENY_ARGUMENTS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf"},
}
SHELL_VALUE_TYPES = {"any", "int", "path"}
TRANSIENT_ERROR_MARKERS = (
    "rate limit",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "temporary failure",
    "network error",
    "connection error",
    "connection reset",
    "try again",
    "retry later",
    "overloaded",
    "unavailable",
)


class AIBroker:
    def __init__(
        self,
        policy_path: str = DEFAULT_POLICY_PATH,
        cache_path: str = DEFAULT_CACHE_PATH,
        budget_ledger_path: str = DEFAULT_BUDGET_LEDGER,
        provider_status_path: str = DEFAULT_PROVIDER_STATUS,
        audit_log_path: str = DEFAULT_AUDIT_LOG,
        enforcement: Optional[str] = None,
    ):
        self.policy_path = policy_path
        self.cache_path = cache_path
        self.budget_ledger_path = budget_ledger_path
        self.provider_status_path = provider_status_path
        self.audit_log_path = audit_log_path
        self.policy = self.load_json(policy_path, default={})
        self._enforcement_override = self._normalize_enforcement(enforcement)
        self.rule_engine = RuleEngine(self.policy.get("admission_rules"))
        self.default_user = self._current_user()
        self.cache = self.load_json(cache_path, default={})
        self.budget_ledger = self.load_json(
            budget_ledger_path, default={"date": self.today(), "spent": {}}
        )
        self.provider_status = self.load_json(provider_status_path, default={})
        self._normalize_budget_ledger()

    @staticmethod
    def now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def today() -> str:
        return time.strftime("%Y-%m-%d")

    @staticmethod
    def load_json(path: str, default: Any) -> Any:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def save_json(path: str, payload: Any) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)

    @staticmethod
    def _json_safe(payload: Any) -> Any:
        try:
            json.dumps(payload)
            return payload
        except TypeError:
            return str(payload)

    @staticmethod
    def _current_user() -> str:
        try:
            return getpass.getuser()
        except Exception:  # noqa: BLE001 - no controlling terminal, etc.
            return os.environ.get("USER", "unknown")

    @staticmethod
    def _normalize_enforcement(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized not in VALID_ENFORCEMENT_MODES:
            raise ValueError(
                f"Unknown enforcement mode '{value}'; "
                f"expected one of {sorted(VALID_ENFORCEMENT_MODES)}"
            )
        return normalized

    def enforcement(self) -> str:
        """Effective enforcement mode: constructor arg, then policy, then strict."""
        if self._enforcement_override:
            return self._enforcement_override
        configured = self.policy.get("enforcement", {})
        if isinstance(configured, str):
            candidate = configured.strip().lower()
        else:
            candidate = str(configured.get("mode", ENFORCEMENT_STRICT)).strip().lower()
        if candidate not in VALID_ENFORCEMENT_MODES:
            return ENFORCEMENT_STRICT
        return candidate

    def reload_policy(self) -> None:
        self.policy = self.load_json(self.policy_path, default={})
        self.rule_engine = RuleEngine(self.policy.get("admission_rules"))

    def audit(self, event: str, record: Dict[str, Any]) -> None:
        """Append one structured event to the broker audit trail."""
        os.makedirs(os.path.dirname(self.audit_log_path) or ".", exist_ok=True)
        entry = {
            "timestamp": self.now(),
            "event": event,
            **record,
        }
        with open(self.audit_log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(self._json_safe(entry), sort_keys=True) + "\n")

    def _normalize_budget_ledger(self) -> None:
        if self.budget_ledger.get("date") != self.today():
            self.budget_ledger = {"date": self.today(), "spent": {}}
        self.budget_ledger.setdefault("spent", {})

    def spent_today_total(self) -> float:
        self._normalize_budget_ledger()
        return round(sum(float(v) for v in self.budget_ledger["spent"].values()), 6)

    def spent_today_for_provider(self, provider: str) -> float:
        self._normalize_budget_ledger()
        return float(self.budget_ledger["spent"].get(provider, 0.0))

    def add_spend(self, provider: str, amount: float) -> None:
        self._normalize_budget_ledger()
        self.budget_ledger["spent"][provider] = round(
            self.spent_today_for_provider(provider) + float(amount), 6
        )
        self.save_json(self.budget_ledger_path, self.budget_ledger)

    def mode(self) -> str:
        return str(self.policy.get("mode", "normal")).lower()

    def role_config(self, user_role: str) -> Optional[Dict[str, Any]]:
        return self.policy.get("roles", {}).get(user_role)

    def provider_registry(self) -> Dict[str, Dict[str, Any]]:
        """Return the broker's policy-declared provider registry.

        The registry is optional so older policies continue to work.  It is the
        right place for explicit negative policy, public status summaries, and
        provider-level metadata that should be visible in status reports.
        """
        registry = self.policy.get("provider_registry", {})
        if isinstance(registry, dict):
            return registry
        return {}

    def provider_registry_entry(self, provider: str) -> Dict[str, Any]:
        entry = self.provider_registry().get(provider, {})
        if isinstance(entry, dict):
            return entry
        return {}

    def provider_block_reason(self, provider: str) -> Optional[str]:
        entry = self.provider_registry_entry(provider)
        status = str(entry.get("status", "")).strip().lower()
        enabled = entry.get("enabled", True)
        allow_routing = entry.get("policy", {}).get("allow_routing", True)

        if enabled is False or allow_routing is False or status in STATUS_BLOCKED_VALUES:
            return str(
                entry.get("summary")
                or entry.get("reason")
                or f"Provider '{provider}' is blocked by provider registry policy"
            )
        return None

    def provider_ok(self, provider: str) -> bool:
        status = self.provider_status.get(provider, {"ok": True})
        disabled_until = float(status.get("disabled_until_epoch", 0) or 0)
        if disabled_until and time.time() < disabled_until:
            return False
        return bool(status.get("ok", True))

    def set_provider_status(
        self,
        provider: str,
        ok: bool,
        note: str = "",
        cooldown_seconds: Optional[int] = None,
    ) -> None:
        status = {
            "ok": bool(ok),
            "note": note,
            "updated_at": self.now(),
        }
        if cooldown_seconds and cooldown_seconds > 0:
            status["disabled_until_epoch"] = round(time.time() + cooldown_seconds, 3)
            status["disabled_until"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(status["disabled_until_epoch"])
            )
        self.provider_status[provider] = status
        self.save_json(self.provider_status_path, self.provider_status)

    def clear_provider_status(self, provider: str) -> bool:
        if provider not in self.provider_status:
            return False
        previous = self.provider_status.pop(provider)
        self.save_json(self.provider_status_path, self.provider_status)
        self.audit("provider_status_cleared", {"provider": provider, "previous_status": previous})
        return True

    def task_risk_tier(self, task: Dict[str, Any]) -> str:
        explicit = task.get("risk_tier")
        if explicit:
            return str(explicit)

        task_type = task.get("type")
        intent = task.get("intent")
        if task_type == "fixture":
            return "file_read"
        if task_type == "shell":
            return "shell_safe"
        if task_type == "mcp" and intent == "capture_thought":
            return "external_write"
        if task_type == "mcp":
            return "external_read"
        if task_type == "dream":
            return "external_write"
        if task_type == "ocr":
            return "file_read"
        if task_type == "llm":
            return "external_inference"
        return "unknown"

    def _payload_digest(self, task: Dict[str, Any]) -> str:
        payload = task.get("payload", {})
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def normalize_task_for_cache(self, task: Dict[str, Any]) -> Dict[str, Any]:
        payload = task.get("payload", {})
        # For image-based tasks, replace the file path with a SHA-256 of the
        # image content so the cache key tracks what the image contains rather
        # than where it lives.
        if isinstance(payload, dict) and "image_path" in payload:
            image_path = payload.get("image_path", "")
            try:
                with open(image_path, "rb") as fh:
                    content_hash = hashlib.sha256(fh.read()).hexdigest()
                payload = {**payload, "image_path": f"sha256:{content_hash}"}
            except (OSError, IOError):
                pass  # unreadable file — fall back to path string
        normalized = {
            "type": task.get("type"),
            "intent": task.get("intent"),
            "provider": task.get("provider"),
            "risk_tier": self.task_risk_tier(task),
            "payload": payload,
            "cwd": os.path.abspath(task.get("cwd", ".")),
        }
        return normalized

    def task_cache_key(self, task: Dict[str, Any]) -> str:
        normalized = self.normalize_task_for_cache(task)
        blob = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def cache_lookup(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        task_type = task.get("type")
        task_policy = self.policy.get("task_defaults", {}).get(task_type, {})
        ttl_seconds = int(task_policy.get("cache_ttl_seconds", 0))
        if ttl_seconds <= 0:
            return None

        key = self.task_cache_key(task)
        entry = self.cache.get(key)
        if not entry:
            return None

        age = time.time() - float(entry.get("created_at_epoch", 0))
        if age > ttl_seconds:
            return None

        entry["hit_count"] = int(entry.get("hit_count", 0)) + 1
        self.cache[key] = entry
        self.save_json(self.cache_path, self.cache)
        return entry

    def cache_store(self, task: Dict[str, Any], response: Dict[str, Any]) -> None:
        task_type = task.get("type")
        task_policy = self.policy.get("task_defaults", {}).get(task_type, {})
        ttl_seconds = int(task_policy.get("cache_ttl_seconds", 0))
        if ttl_seconds <= 0:
            return

        key = self.task_cache_key(task)
        self.cache[key] = {
            "task": self.normalize_task_for_cache(task),
            "response": response,
            "created_at": self.now(),
            "created_at_epoch": time.time(),
            "hit_count": 0,
            "ttl_seconds": ttl_seconds,
        }
        self.save_json(self.cache_path, self.cache)

    def _allowed_path(self, cwd: str, allowed_paths: List[str]) -> bool:
        cwd_abs = os.path.abspath(cwd)
        for allowed in allowed_paths:
            allowed_abs = os.path.abspath(allowed)
            try:
                if os.path.commonpath([cwd_abs, allowed_abs]) == allowed_abs:
                    return True
            except ValueError:
                continue
        return False

    def _parse_shell_command(self, command: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
        if any(ch in command for ch in SAFE_SHELL_METACHARS):
            return None, None, "Shell metacharacters are not allowed through the broker"
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return None, None, f"Cannot parse shell command: {exc}"
        if not parts:
            return None, None, "Missing shell command"
        return parts[0], parts, None

    def _check_read_only_shell_args(self, cmd_name: str, parsed: List[str]) -> Optional[str]:
        denied_args = READ_ONLY_DENY_ARGUMENTS.get(cmd_name, set())
        for arg in parsed[1:]:
            if arg in denied_args:
                return (
                    f"Shell command '{cmd_name}' is not allowed to use '{arg}' "
                    "in read-only mode"
                )
        return None

    def _validate_shell_value(self, cmd_name: str, flag: str, raw_value: str, spec: Any) -> Optional[str]:
        if spec is None or spec is True:
            return None

        if isinstance(spec, list):
            if raw_value not in spec:
                return (
                    f"Shell command '{cmd_name}' option '{flag}' must be one of "
                    f"{spec!r}"
                )
            return None

        if not isinstance(spec, dict):
            return f"Shell command '{cmd_name}' has invalid schema for option '{flag}'"

        value_type = str(spec.get("type", "any"))
        if value_type not in SHELL_VALUE_TYPES:
            return (
                f"Shell command '{cmd_name}' option '{flag}' has unsupported type "
                f"'{value_type}'"
            )

        if value_type == "int":
            try:
                int(raw_value)
            except ValueError:
                return f"Shell command '{cmd_name}' option '{flag}' requires an integer"
        elif value_type == "path":
            normalized = os.path.normpath(raw_value)
            if os.path.isabs(raw_value) or normalized.startswith(".."):
                return (
                    f"Shell command '{cmd_name}' option '{flag}' requires a relative path "
                    "inside the current working tree"
                )

        allowed_values = spec.get("values")
        if allowed_values is not None and raw_value not in allowed_values:
            return (
                f"Shell command '{cmd_name}' option '{flag}' must be one of "
                f"{allowed_values!r}"
            )
        return None

    def _validate_shell_schema(self, cmd_name: str, parsed: List[str], schema: Dict[str, Any]) -> Optional[str]:
        allowed_flags = set(schema.get("allowed_flags", []))
        options_with_values = schema.get("options_with_values", {})
        allow_positionals = bool(schema.get("allow_positionals", True))
        min_positionals = int(schema.get("min_positionals", 0))
        max_positionals = schema.get("max_positionals")
        allow_dashdash = bool(schema.get("allow_dashdash", False))

        if max_positionals is not None:
            max_positionals = int(max_positionals)

        positional_count = 0
        pending_value_for: Optional[str] = None
        parsing_options = True

        for arg in parsed[1:]:
            if pending_value_for is not None:
                error = self._validate_shell_value(
                    cmd_name, pending_value_for, arg, options_with_values[pending_value_for]
                )
                if error:
                    return error
                pending_value_for = None
                continue

            if parsing_options and arg == "--":
                if not allow_dashdash:
                    return f"Shell command '{cmd_name}' is not allowed to use '--'"
                parsing_options = False
                continue

            if parsing_options and arg.startswith("-") and arg != "-":
                if arg in allowed_flags:
                    continue
                if arg in options_with_values:
                    pending_value_for = arg
                    continue
                return f"Shell command '{cmd_name}' option '{arg}' is not allowed"

            positional_count += 1

        if pending_value_for is not None:
            return f"Shell command '{cmd_name}' option '{pending_value_for}' requires a value"
        if not allow_positionals and positional_count > 0:
            return f"Shell command '{cmd_name}' does not allow positional arguments"
        if positional_count < min_positionals:
            return (
                f"Shell command '{cmd_name}' requires at least {min_positionals} "
                "positional argument(s)"
            )
        if max_positionals is not None and positional_count > max_positionals:
            return (
                f"Shell command '{cmd_name}' allows at most {max_positionals} "
                "positional argument(s)"
            )
        return None

    def _shell_rule_for_command(
        self,
        task_rules: Any,
        cmd_name: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        if isinstance(task_rules, list):
            if "*" in task_rules or cmd_name in task_rules:
                return True, None, None
            return False, None, f"Shell command '{cmd_name}' is not allowed"

        if isinstance(task_rules, dict):
            if cmd_name in task_rules:
                rule = task_rules[cmd_name]
            elif "*" in task_rules:
                rule = task_rules["*"]
            else:
                return False, None, f"Shell command '{cmd_name}' is not allowed"

            if rule is False:
                return False, None, f"Shell command '{cmd_name}' is disabled"
            if rule is True or rule is None:
                return True, None, None
            if not isinstance(rule, dict):
                return False, None, f"Shell command '{cmd_name}' has an invalid policy schema"
            return True, rule, None

        return False, None, "Shell task policy must be a list or object"

    def _check_admission_rules(
        self, task: Dict[str, Any], user_role: str, risk_tier: str
    ) -> Dict[str, Any]:
        """Evaluate the deny-by-default admission rules from policy.json.

        This runs before the role, agent, provider, and budget checks and does
        not replace any of them: a request must satisfy the rules *and* every
        existing control.  When policy.json carries no admission_rules block
        the gate is inert.
        """
        context = build_context(
            task=task,
            user_role=user_role,
            user=str(task.get("user") or self.default_user),
            mode=self.mode(),
            enforcement=self.enforcement(),
            risk_tier=risk_tier,
        )
        decision = self.rule_engine.evaluate(context)
        if decision.errors:
            self.audit(
                "admission_rule_error",
                {"errors": decision.errors, "user_role": user_role},
            )
        return decision.as_dict()

    def _check_role_policy(self, user_role: str, task: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        role = self.role_config(user_role)
        if not role:
            return False, "Unknown role"

        task_type = task.get("type")
        allow_tasks = role.get("allow_tasks", {})
        if task_type not in allow_tasks:
            return False, f"Task type '{task_type}' is not allowed for role '{user_role}'"

        task_rules = allow_tasks[task_type]
        if task_rules is False:
            return False, f"Task type '{task_type}' is disabled"

        if task_type == "shell":
            command = str(task.get("payload", {}).get("command", "")).strip()
            cmd_name, parsed, error = self._parse_shell_command(command)
            if error:
                return False, error

            allowed_command, shell_schema, shell_error = self._shell_rule_for_command(
                task_rules, cmd_name
            )
            if not allowed_command:
                return False, shell_error
            if shell_schema:
                schema_error = self._validate_shell_schema(cmd_name, parsed, shell_schema)
                if schema_error:
                    return False, schema_error

            cwd = os.path.abspath(task.get("cwd", "."))
            allowed_paths = [os.path.abspath(p) for p in role.get("allow_paths", [])]
            if allowed_paths and not self._allowed_path(cwd, allowed_paths):
                return False, f"Path '{cwd}' is not allowed"

            if role.get("read_only", False) and cmd_name in READ_ONLY_DENY_COMMANDS:
                return False, f"Shell command '{cmd_name}' is not allowed in read-only mode"
            if role.get("read_only", False):
                read_only_error = self._check_read_only_shell_args(cmd_name, parsed)
                if read_only_error:
                    return False, read_only_error
        else:
            intent = task.get("intent")
            if isinstance(task_rules, list) and intent not in task_rules:
                return False, f"Intent '{intent}' is not allowed for task type '{task_type}'"

        return True, None

    def _check_provider_policy(self, user_role: str, provider: str) -> Tuple[bool, Optional[str]]:
        role = self.role_config(user_role)
        if not role:
            return False, "Unknown role"
        blocked_reason = self.provider_block_reason(provider)
        if blocked_reason:
            return False, f"Provider '{provider}' is blocked by policy: {blocked_reason}"
        allowed = role.get("allow_providers", [])
        denied = set(role.get("deny_providers", []))
        if provider in denied:
            return False, f"Provider '{provider}' is denied by policy"
        if allowed and "*" not in allowed and provider not in allowed:
            return False, f"Provider '{provider}' is not allowed for role '{user_role}'"
        return True, None

    def _check_mode_policy(self, provider: str) -> Tuple[bool, Optional[str]]:
        mode = self.mode()
        if mode == "isolation" and provider not in {"local", "shell"}:
            return False, f"Provider '{provider}' is disabled in isolation mode"
        if mode == "economy" and provider not in {"local", "shell", "mcp_openbrain"}:
            # Economy mode is a preference, not an absolute ban.  A policy may
            # override this by setting economy_allow_external=true.
            if not self.policy.get("economy_allow_external", False):
                return False, f"Provider '{provider}' is disabled in economy mode"
        return True, None

    def _check_budget_policy(
        self,
        provider: str,
        estimated_cost: float,
        task: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str]]:
        limits = self.policy.get("budget", {})
        daily_global = float(limits.get("daily_global_limit", 0.0))
        per_provider = float(limits.get("provider_daily_limits", {}).get(provider, daily_global))

        task = task or {}
        risk_tier = self.task_risk_tier(task)
        per_task_global = float(limits.get("per_task_limit", 0.0) or 0.0)
        per_task_provider = float(
            limits.get("provider_per_task_limits", {}).get(provider, per_task_global) or 0.0
        )
        per_task_risk = float(
            limits.get("risk_tier_per_task_limits", {}).get(risk_tier, per_task_provider) or 0.0
        )

        projected_global = self.spent_today_total() + estimated_cost
        projected_provider = self.spent_today_for_provider(provider) + estimated_cost

        if per_task_risk > 0 and estimated_cost > per_task_risk:
            return False, (
                f"Estimated task cost ${estimated_cost:.4f} exceeds per-task budget "
                f"for risk tier '{risk_tier}' (${per_task_risk:.4f})"
            )
        if per_task_provider > 0 and estimated_cost > per_task_provider:
            return False, (
                f"Estimated task cost ${estimated_cost:.4f} exceeds per-task budget "
                f"for provider '{provider}' (${per_task_provider:.4f})"
            )
        if per_task_global > 0 and estimated_cost > per_task_global:
            return False, (
                f"Estimated task cost ${estimated_cost:.4f} exceeds global per-task budget "
                f"(${per_task_global:.4f})"
            )
        if daily_global > 0 and projected_global > daily_global:
            return False, (
                f"Projected spend ${projected_global:.4f} exceeds global daily budget "
                f"${daily_global:.4f}"
            )
        if per_provider > 0 and projected_provider > per_provider:
            return False, (
                f"Projected spend ${projected_provider:.4f} exceeds provider budget "
                f"for '{provider}' (${per_provider:.4f})"
            )
        return True, None

    def _declared_agent_fanout(self, task: Dict[str, Any]) -> int:
        payload = task.get("payload", {}) if isinstance(task.get("payload"), dict) else {}
        agent = task.get("agent", {}) if isinstance(task.get("agent"), dict) else {}
        candidates = (
            task.get("child_task_count"),
            task.get("agent_count"),
            agent.get("child_task_count"),
            agent.get("agent_count"),
            payload.get("child_task_count"),
            payload.get("agent_count"),
        )
        fanout = 0
        for value in candidates:
            if value is None:
                continue
            try:
                fanout = max(fanout, int(value))
            except (TypeError, ValueError):
                continue
        return max(0, fanout)

    def _agent_swarm_approved(self, task: Dict[str, Any]) -> bool:
        agent = task.get("agent", {}) if isinstance(task.get("agent"), dict) else {}
        payload = task.get("payload", {}) if isinstance(task.get("payload"), dict) else {}
        return bool(
            task.get("swarm_approved")
            or task.get("user_approved_swarm")
            or agent.get("swarm_approved")
            or agent.get("user_approved_swarm")
            or payload.get("swarm_approved")
            or payload.get("user_approved_swarm")
        )

    def _check_agent_policy(self, task: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        controls = self.policy.get("agent_controls", {})
        if not controls:
            return True, None

        fanout = self._declared_agent_fanout(task)
        max_child_tasks = int(controls.get("default_max_child_tasks", 0) or 0)
        require_approval = bool(controls.get("require_explicit_swarm_approval", True))

        if fanout <= max_child_tasks:
            return True, None
        if require_approval and not self._agent_swarm_approved(task):
            return False, (
                f"Declared agent fan-out ({fanout}) exceeds default limit "
                f"({max_child_tasks}); explicit swarm approval is required"
            )

        hard_limit = controls.get("hard_max_child_tasks")
        if hard_limit is not None and fanout > int(hard_limit):
            return False, (
                f"Declared agent fan-out ({fanout}) exceeds hard limit "
                f"({int(hard_limit)})"
            )
        return True, None

    def _preferred_providers(self, task: Dict[str, Any]) -> List[str]:
        task_type = task.get("type")
        if task_type == "shell":
            return ["shell"]

        explicit = task.get("provider")
        routing = self.policy.get("routing", {})
        task_intent = task.get("intent")

        preferred = routing.get("task_routes", {}).get(task_type, [])
        if task_intent:
            preferred = routing.get("intent_routes", {}).get(task_intent, preferred)
        if not preferred:
            preferred = routing.get("fallback_order", ["local"])

        if explicit:
            preferred = [explicit] + [p for p in preferred if p != explicit]

        # Use the existing router's selected provider as a strong hint, but do
        # not let it override an explicit provider requested by the caller.
        try:
            routed = choose_provider(task, self.policy, self.provider_status, self.budget_ledger)
            if explicit:
                preferred = [explicit] + [p for p in preferred if p not in {explicit, routed}]
                if routed != explicit:
                    preferred.append(routed)
            else:
                preferred = [routed] + [p for p in preferred if p != routed]
        except Exception:
            pass

        fallback = routing.get("fallback_order", [])
        preferred.extend(p for p in fallback if p not in preferred)

        if self.mode() == "economy":
            preferred.sort(key=lambda p: (estimate_cost(p, task) > 0, estimate_cost(p, task), p))

        return preferred

    def _select_provider(self, task: Dict[str, Any], user_role: str) -> Tuple[Optional[str], float, List[Dict[str, Any]]]:
        attempts: List[Dict[str, Any]] = []
        for provider in self._preferred_providers(task):
            attempt: Dict[str, Any] = {"provider": provider}

            provider_ok, provider_reason = self._check_provider_policy(user_role, provider)
            if not provider_ok:
                attempt.update({"accepted": False, "stage": "provider_policy", "reason": provider_reason})
                attempts.append(attempt)
                explicit_provider = task.get("provider")
                entry_policy = self.provider_registry_entry(provider).get("policy", {})
                if provider == explicit_provider and (
                    self.provider_block_reason(provider)
                    or entry_policy.get("allow_fallback") is False
                    or entry_policy.get("allow_routing") is False
                ):
                    return None, 0.0, attempts
                continue

            mode_ok, mode_reason = self._check_mode_policy(provider)
            if not mode_ok:
                attempt.update({"accepted": False, "stage": "mode", "reason": mode_reason})
                attempts.append(attempt)
                continue

            if not self.provider_ok(provider):
                attempt.update({"accepted": False, "stage": "provider_health", "reason": "provider unavailable or cooling down"})
                attempts.append(attempt)
                continue

            estimated = estimate_cost(provider, task)
            budget_ok, budget_reason = self._check_budget_policy(provider, estimated, task)
            if not budget_ok:
                attempt.update({"accepted": False, "stage": "budget", "estimated_cost": estimated, "reason": budget_reason})
                attempts.append(attempt)
                continue

            attempt.update({"accepted": True, "estimated_cost": estimated})
            attempts.append(attempt)
            return provider, estimated, attempts

        return None, 0.0, attempts

    def _approved_provider_candidates(
        self,
        task: Dict[str, Any],
        user_role: str,
        exclude: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        excluded = set(exclude or [])
        candidates: List[Dict[str, Any]] = []
        for provider in self._preferred_providers(task):
            if provider in excluded:
                continue

            provider_ok, _ = self._check_provider_policy(user_role, provider)
            if not provider_ok:
                continue

            mode_ok, _ = self._check_mode_policy(provider)
            if not mode_ok:
                continue

            if not self.provider_ok(provider):
                continue

            estimated = estimate_cost(provider, task)
            budget_ok, _ = self._check_budget_policy(provider, estimated, task)
            if not budget_ok:
                continue

            candidates.append({"provider": provider, "estimated_cost": estimated})
        return candidates

    def _retry_attempts_for_provider(self, provider: str) -> int:
        retries = int(self.policy.get("provider_retry_max_attempts", 1))
        per_provider = self.policy.get("provider_retry_attempts", {})
        if provider in per_provider:
            retries = int(per_provider[provider])
        return max(1, retries)

    def _is_transient_provider_error(self, provider: str, error: str) -> bool:
        if provider in {"shell", "local"}:
            return False
        normalized = str(error or "").strip().lower()
        return any(marker in normalized for marker in TRANSIENT_ERROR_MARKERS)

    def review_task(self, task: Dict[str, Any], user_role: str = "developer") -> Dict[str, Any]:
        """Validate a task and choose a provider without executing it."""
        self.reload_policy()
        risk_tier = self.task_risk_tier(task)

        admission = self._check_admission_rules(task, user_role, risk_tier)
        if not admission["allowed"]:
            decision = {
                "ok": False,
                "approved": False,
                "stage": "admission_rules",
                "error": admission["reason"],
                "matched_rule": admission["rule"],
                "risk_tier": risk_tier,
            }
            self.audit("task_denied", {"user_role": user_role, "decision": decision, "payload_digest": self._payload_digest(task)})
            return decision

        role_ok, role_reason = self._check_role_policy(user_role, task)
        if not role_ok:
            decision = {
                "ok": False,
                "approved": False,
                "stage": "policy",
                "error": role_reason,
                "risk_tier": risk_tier,
            }
            self.audit("task_denied", {"user_role": user_role, "decision": decision, "payload_digest": self._payload_digest(task)})
            return decision

        agent_ok, agent_reason = self._check_agent_policy(task)
        if not agent_ok:
            decision = {
                "ok": False,
                "approved": False,
                "stage": "agent_controls",
                "error": agent_reason,
                "risk_tier": risk_tier,
            }
            self.audit("task_denied", {"user_role": user_role, "decision": decision, "payload_digest": self._payload_digest(task)})
            return decision

        provider, estimated, attempts = self._select_provider(task, user_role)
        if provider is None:
            decision = {
                "ok": False,
                "approved": False,
                "stage": "routing",
                "error": "No provider satisfied role, mode, health, and budget policy",
                "risk_tier": risk_tier,
                "provider_attempts": attempts,
            }
            self.audit("task_denied", {"user_role": user_role, "decision": decision, "payload_digest": self._payload_digest(task)})
            return decision

        decision = {
            "ok": True,
            "approved": True,
            "stage": "approved",
            "provider": provider,
            "estimated_cost": estimated,
            "risk_tier": risk_tier,
            "user_role": user_role,
            "provider_attempts": attempts,
            "matched_rule": admission["rule"],
        }
        self.audit("task_approved", {"user_role": user_role, "decision": decision, "payload_digest": self._payload_digest(task)})
        return decision

    def execute_task(self, task: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a task that has already passed review_task()."""
        if not decision.get("approved"):
            return {"ok": False, "stage": "approval", "error": "Task has not been approved"}

        provider = str(decision["provider"])
        estimated = float(decision.get("estimated_cost", 0.0))
        user_role = str(decision.get("user_role", "developer"))
        task_for_provider = {**task, "provider": provider}

        cached = self.cache_lookup(task_for_provider)
        if cached:
            cached_response = {
                "ok": True,
                "result": cached["response"].get("result"),
                "metadata": cached["response"].get("metadata", {}),
            }
            assurance = cached["response"].get("assurance") or assess_output(
                task_for_provider,
                cached_response,
                str(cached["response"].get("provider") or provider),
            )
            result = {
                "ok": True,
                "task": task,
                "cache_hit": True,
                "provider": cached["response"].get("provider"),
                "estimated_cost": 0.0,
                "spent_today": self.spent_today_total(),
                "risk_tier": decision.get("risk_tier"),
                "result": cached["response"].get("result"),
                "metadata": cached["response"].get("metadata", {}),
                "assurance": assurance,
                "completion_status": assurance["completion_disposition"],
            }
            self.audit("task_cache_hit", {"provider": provider, "risk_tier": decision.get("risk_tier"), "payload_digest": self._payload_digest(task)})
            return result

        failed_providers: List[str] = []
        execution_attempts: List[Dict[str, Any]] = []
        candidates = [{"provider": provider, "estimated_cost": estimated}]
        seen_providers = {provider}
        for candidate in self._approved_provider_candidates(task, user_role=user_role, exclude=[]):
            if candidate["provider"] in seen_providers:
                continue
            candidates.append(candidate)
            seen_providers.add(candidate["provider"])

        for candidate in candidates:
            active_provider = str(candidate["provider"])
            active_estimated = float(candidate["estimated_cost"])
            max_attempts = self._retry_attempts_for_provider(active_provider)
            task_for_provider = {**task, "provider": active_provider}

            for attempt_number in range(1, max_attempts + 1):
                response = call_provider(active_provider, task_for_provider, policy=self.policy)
                if response.get("ok", False):
                    self.add_spend(active_provider, active_estimated)

                    assurance = assess_output(
                        task_for_provider,
                        response,
                        active_provider,
                    )

                    final_response = {
                        "provider": active_provider,
                        "result": response.get("result"),
                        "metadata": response.get("metadata", {}),
                        "assurance": assurance,
                    }
                    if not assurance["requires_human_approval"]:
                        self.cache_store(task_for_provider, final_response)

                    result = {
                        "ok": True,
                        "task": task,
                        "cache_hit": False,
                        "provider": active_provider,
                        "estimated_cost": active_estimated,
                        "spent_today": self.spent_today_total(),
                        "risk_tier": decision.get("risk_tier"),
                        "result": response.get("result"),
                        "metadata": response.get("metadata", {}),
                        "assurance": assurance,
                        "completion_status": assurance["completion_disposition"],
                        "execution_attempts": execution_attempts + [{
                            "provider": active_provider,
                            "attempt": attempt_number,
                            "ok": True,
                        }],
                    }
                    if assurance["requires_human_approval"]:
                        self.audit("task_flagged_for_review", {"provider": active_provider, "estimated_cost": active_estimated, "spent_today": result["spent_today"], "risk_tier": decision.get("risk_tier"), "payload_digest": self._payload_digest(task), "execution_attempts": result["execution_attempts"], "assurance": assurance})
                    else:
                        self.audit("task_completed", {"provider": active_provider, "estimated_cost": active_estimated, "spent_today": result["spent_today"], "risk_tier": decision.get("risk_tier"), "payload_digest": self._payload_digest(task), "execution_attempts": result["execution_attempts"], "assurance": assurance})
                    return result

                error = response.get("error", "Unknown provider error")
                transient = self._is_transient_provider_error(active_provider, error)
                attempt_record = {
                    "provider": active_provider,
                    "attempt": attempt_number,
                    "ok": False,
                    "error": error,
                    "transient": transient,
                }
                execution_attempts.append(attempt_record)
                self.audit("provider_attempt_failed", {"provider": active_provider, "attempt": attempt_number, "transient": transient, "error": error, "payload_digest": self._payload_digest(task)})

                if transient and attempt_number < max_attempts:
                    continue

                cooldown = int(self.policy.get("provider_failure_cooldown_seconds", 600))
                self.set_provider_status(active_provider, False, error, cooldown_seconds=cooldown)
                failed_providers.append(active_provider)
                break

        last_error = execution_attempts[-1]["error"] if execution_attempts else "Unknown provider error"
        result = {
            "ok": False,
            "stage": "provider_call",
            "provider": failed_providers[-1] if failed_providers else provider,
            "error": last_error,
            "risk_tier": decision.get("risk_tier"),
            "execution_attempts": execution_attempts,
        }
        self.audit("task_failed", {"provider": result["provider"], "result": result, "payload_digest": self._payload_digest(task)})
        return result

    def _fallback_to_mcp_proxy(
        self,
        task: Dict[str, Any],
        user_role: str,
        trigger: str,
        broker_result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Permissive-mode escape hatch to the standalone mcp_proxy.

        Returns None when the fallback does not apply, so the caller can keep
        the broker's original (rejecting) result.  Every attempt is audited:
        that trail is the list of cases policy still needs to cover before
        strict mode can be the default.
        """
        if self.enforcement() != ENFORCEMENT_PERMISSIVE:
            return None

        config = mcp_proxy_bridge.fallback_config(self.policy)

        if trigger == "policy_denial" and not config["on_policy_denial"]:
            return None
        if trigger == "execution_failure" and not config["on_execution_failure"]:
            return None

        eligible, ineligible_reason = mcp_proxy_bridge.is_eligible(task, config)
        broker_reason = broker_result.get("error", "unknown")

        if not eligible:
            self.audit(
                "fallback_refused",
                {
                    "trigger": trigger,
                    "user_role": user_role,
                    "broker_stage": broker_result.get("stage"),
                    "broker_reason": broker_reason,
                    "refusal_reason": ineligible_reason,
                    "task_type": task.get("type"),
                    "intent": task.get("intent"),
                    "payload_digest": self._payload_digest(task),
                },
            )
            return None

        response = mcp_proxy_bridge.forward(task, config)

        self.audit(
            "fallback_to_mcp_proxy",
            {
                "trigger": trigger,
                "user_role": user_role,
                "broker_stage": broker_result.get("stage"),
                "broker_reason": broker_reason,
                "matched_rule": broker_result.get("matched_rule"),
                "task_type": task.get("type"),
                "intent": task.get("intent"),
                "proxy_url": config["url"],
                "ok": response.get("ok", False),
                "error": response.get("error"),
                "payload_digest": self._payload_digest(task),
            },
        )

        if not response.get("ok", False):
            return {
                "ok": False,
                "stage": "fallback",
                "error": response.get("error", "mcp_proxy fallback failed"),
                "mediated": False,
                "fallback": {
                    "used": True,
                    "target": "mcp_proxy",
                    "trigger": trigger,
                    "broker_reason": broker_reason,
                },
                "broker_decision": broker_result,
            }

        fallback_response = {
            "ok": True,
            "result": response.get("result"),
            "metadata": {**response.get("metadata", {}), "mediated": False},
        }
        assurance = assess_output(task, fallback_response, "mcp_proxy")
        self.audit(
            "task_flagged_for_review",
            {
                "provider": "mcp_proxy",
                "risk_tier": broker_result.get("risk_tier"),
                "payload_digest": self._payload_digest(task),
                "assurance": assurance,
            },
        )
        return {
            "ok": True,
            "task": task,
            "cache_hit": False,
            "provider": "mcp_proxy",
            "estimated_cost": 0.0,
            "spent_today": self.spent_today_total(),
            "risk_tier": broker_result.get("risk_tier"),
            "result": response.get("result"),
            "metadata": fallback_response["metadata"],
            "assurance": assurance,
            "completion_status": assurance["completion_disposition"],
            "mediated": False,
            "fallback": {
                "used": True,
                "target": "mcp_proxy",
                "trigger": trigger,
                "broker_reason": broker_reason,
            },
            "broker_decision": broker_result,
        }

    def process_task(self, task: Dict[str, Any], user_role: str = "developer") -> Dict[str, Any]:
        decision = self.review_task(task, user_role=user_role)

        if not decision.get("approved"):
            fallback = self._fallback_to_mcp_proxy(
                task, user_role, "policy_denial", decision
            )
            return fallback if fallback is not None else decision

        result = self.execute_task(task, decision)

        if not result.get("ok", False):
            fallback = self._fallback_to_mcp_proxy(
                task, user_role, "execution_failure", result
            )
            if fallback is not None:
                return fallback

        return result

    def _status_sample_tasks(self) -> Dict[str, Dict[str, Any]]:
        configured = self.policy.get("status_report", {}).get("sample_tasks", {})
        if isinstance(configured, dict) and configured:
            return configured
        return {
            "summarize": {
                "type": "llm",
                "intent": "summarize",
                "payload": {"text": "Summarize this short calibration message."},
            },
            "analyze": {
                "type": "llm",
                "intent": "analyze",
                "payload": {"text": "Analyze a small software cost-control design."},
            },
            "classify": {
                "type": "llm",
                "intent": "classify",
                "payload": {"text": "Classify this request as low, medium, or high cost risk."},
            },
        }

    def _configured_quality_score(self, provider: str, task_class: str) -> Optional[float]:
        entry = self.provider_registry_entry(provider)
        score = entry.get("quality_scores", {}).get(task_class)
        if score is None:
            score = self.policy.get("status_report", {}).get("quality_scores", {}).get(provider, {}).get(task_class)
        if score is None:
            return None
        try:
            return float(score)
        except (TypeError, ValueError):
            return None

    def _provider_status_summary(self, provider: str) -> str:
        blocked = self.provider_block_reason(provider)
        if blocked:
            return "blocked"
        runtime = self.provider_status.get(provider, {})
        if runtime and not runtime.get("ok", True):
            return "unavailable"
        return str(self.provider_registry_entry(provider).get("status") or "healthy")

    def build_status_report(self, user_role: str = "developer", public: bool = True) -> Dict[str, Any]:
        """Build a dynamic JSON status report without making provider calls.

        This MVP is intentionally non-spending: it combines policy, current
        health/cooldown state, daily spend, configured quality scores, and
        estimated cost for small sample tasks.  A later benchmark runner can
        update the quality fields with measured calibration results.
        """
        self.reload_policy()
        self._normalize_budget_ledger()
        sample_tasks = self._status_sample_tasks()

        provider_names = set(self.provider_registry().keys())
        provider_names.update(self.policy.get("provider_settings", {}).keys())
        provider_names.update(self.policy.get("budget", {}).get("provider_daily_limits", {}).keys())
        provider_names.update(self.provider_status.keys())
        provider_names.update(self.policy.get("routing", {}).get("fallback_order", []))
        for routes in self.policy.get("routing", {}).get("task_routes", {}).values():
            provider_names.update(routes)
        for routes in self.policy.get("routing", {}).get("intent_routes", {}).values():
            provider_names.update(routes)

        providers: List[Dict[str, Any]] = []
        recommendations: Dict[str, Any] = {}

        for provider in sorted(provider_names):
            entry = self.provider_registry_entry(provider)
            blocked_reason = self.provider_block_reason(provider)
            runtime_status = self.provider_status.get(provider, {})
            spend_today = self.spent_today_for_provider(provider)
            budget_cfg = self.policy.get("budget", {})
            daily_limit = budget_cfg.get("provider_daily_limits", {}).get(provider)

            estimates: Dict[str, float] = {}
            quality_scores: Dict[str, Optional[float]] = {}
            value_scores: Dict[str, Optional[float]] = {}
            eligible_for: Dict[str, bool] = {}

            for task_class, task in sample_tasks.items():
                task_copy = dict(task)
                task_copy["provider"] = provider
                estimate = estimate_cost(provider, task_copy)
                quality = self._configured_quality_score(provider, task_class)
                provider_ok, _ = self._check_provider_policy(user_role, provider)
                mode_ok, _ = self._check_mode_policy(provider)
                health_ok = self.provider_ok(provider)
                budget_ok, _ = self._check_budget_policy(provider, estimate, task_copy)
                eligible = bool(provider_ok and mode_ok and health_ok and budget_ok)

                estimates[task_class] = estimate
                quality_scores[task_class] = quality
                eligible_for[task_class] = eligible
                if eligible and quality is not None:
                    effective_cost = max(estimate, 0.000001)
                    value_scores[task_class] = round(quality / effective_cost, 6)
                else:
                    value_scores[task_class] = None

            provider_record: Dict[str, Any] = {
                "name": provider,
                "status": self._provider_status_summary(provider),
                "scope": entry.get("scope") or ("local" if provider in {"local", "shell"} else "cloud"),
                "eligible": blocked_reason is None and self.provider_ok(provider),
                "summary": entry.get("summary") or runtime_status.get("note") or "Provider available under current policy.",
                "spend_today_usd": round(spend_today, 6),
                "daily_limit_usd": daily_limit,
                "estimated_cost_usd": estimates,
                "quality_scores": quality_scores,
                "value_scores": value_scores,
                "eligible_for": eligible_for,
            }
            if not public:
                provider_record["runtime_status"] = runtime_status
                provider_record["policy"] = entry.get("policy", {})
                provider_record["settings_present"] = provider in self.policy.get("provider_settings", {})
            providers.append(provider_record)

        for task_class in sample_tasks:
            ranked = [
                p for p in providers
                if p.get("eligible_for", {}).get(task_class) and p.get("value_scores", {}).get(task_class) is not None
            ]
            ranked.sort(key=lambda item: float(item["value_scores"][task_class]), reverse=True)
            if ranked:
                winner = ranked[0]
                recommendations[task_class] = {
                    "provider": winner["name"],
                    "reason": "highest policy-eligible MVP value score for this task class",
                    "value_score": winner["value_scores"][task_class],
                    "quality_score": winner["quality_scores"][task_class],
                    "estimated_cost_usd": winner["estimated_cost_usd"][task_class],
                }
            else:
                recommendations[task_class] = {
                    "provider": None,
                    "reason": "no provider has both eligibility and a configured quality score for this task class",
                }

        report = {
            "schema_version": "ai-broker-status/v1",
            "generated_at": self.now(),
            "broker": {
                "mode": self.mode(),
                "policy_path": self.policy_path if not public else os.path.basename(self.policy_path),
                "status_report_mode": "public" if public else "admin",
            },
            "budget": {
                "date": self.budget_ledger.get("date"),
                "spent_today_usd": self.spent_today_total(),
                "daily_global_limit_usd": self.policy.get("budget", {}).get("daily_global_limit"),
            },
            "recommendations": recommendations,
            "providers": providers,
        }
        self.audit("status_report_built", {"public": public, "user_role": user_role, "provider_count": len(providers)})
        return report

    def write_status_report(
        self,
        output_path: str = DEFAULT_STATUS_REPORT,
        user_role: str = "developer",
        public: bool = True,
    ) -> Dict[str, Any]:
        report = self.build_status_report(user_role=user_role, public=public)
        self.save_json(output_path, report)
        self.audit("status_report_written", {"output_path": output_path, "public": public, "user_role": user_role})
        return report

    def warm_cache(self) -> Dict[str, Any]:
        tasks = self.policy.get("prefetch", [])
        results = []
        for task in tasks:
            if task.get("enabled", True):
                role = task.get("user_role", "developer")
                task_copy = dict(task)
                task_copy.pop("enabled", None)
                task_copy.pop("user_role", None)
                results.append(self.process_task(task_copy, user_role=role))
        self.audit("prefetch_completed", {"prefetch_count": len(results)})
        return {"ok": True, "prefetch_count": len(results), "results": results}
