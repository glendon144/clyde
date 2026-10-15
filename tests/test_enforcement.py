"""Tests for strict/permissive enforcement and the mcp_proxy fallback path."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import mcp_proxy_bridge
from ai_broker import ENFORCEMENT_PERMISSIVE, ENFORCEMENT_STRICT, AIBroker
from test_ai_broker import base_policy, write_json

PROXY_OK = {"ok": True, "result": "forwarded", "metadata": {"transport": "mcp_proxy"}}


class EnforcementTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = os.path.join(self.tmp.name, "workspace")
        os.makedirs(self.workdir, exist_ok=True)

        self.policy_path = os.path.join(self.tmp.name, "policy.json")
        self.audit_path = os.path.join(self.tmp.name, "state", "audit.jsonl")
        self.policy = base_policy(self.workdir)

        # Deny everything, so every request exercises the rejection path.
        self.policy["admission_rules"] = {"default": "deny", "rules": []}
        self.policy["fallback"] = {
            "enabled": True,
            "on_policy_denial": True,
            "on_execution_failure": False,
            "allow_task_types": ["mcp"],
            "mcp_proxy": {"url": "http://127.0.0.1:3000", "timeout": 5},
        }
        self.write_policy()

    def write_policy(self):
        write_json(self.policy_path, self.policy)

    def broker(self, enforcement=None):
        return AIBroker(
            policy_path=self.policy_path,
            cache_path=os.path.join(self.tmp.name, "cache", "responses.json"),
            budget_ledger_path=os.path.join(self.tmp.name, "state", "budget.json"),
            provider_status_path=os.path.join(self.tmp.name, "state", "status.json"),
            audit_log_path=self.audit_path,
            enforcement=enforcement,
        )

    def audit_events(self):
        if not os.path.exists(self.audit_path):
            return []
        with open(self.audit_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def event_names(self):
        return [e["event"] for e in self.audit_events()]

    @staticmethod
    def mcp_task():
        return {"type": "mcp", "intent": "search_thoughts", "payload": {"query": "x"}}

    def shell_task(self, command="rm -rf /"):
        return {
            "type": "shell",
            "intent": "execute",
            "cwd": self.workdir,
            "payload": {"command": command},
        }


class EnforcementModeTests(EnforcementTestCase):
    def test_defaults_to_strict(self):
        del self.policy["fallback"]
        self.write_policy()
        self.assertEqual(self.broker().enforcement(), ENFORCEMENT_STRICT)

    def test_reads_mode_from_policy(self):
        self.policy["enforcement"] = {"mode": "permissive"}
        self.write_policy()
        self.assertEqual(self.broker().enforcement(), ENFORCEMENT_PERMISSIVE)

    def test_accepts_bare_string_in_policy(self):
        self.policy["enforcement"] = "permissive"
        self.write_policy()
        self.assertEqual(self.broker().enforcement(), ENFORCEMENT_PERMISSIVE)

    def test_constructor_overrides_policy(self):
        self.policy["enforcement"] = {"mode": "permissive"}
        self.write_policy()
        broker = self.broker(enforcement=ENFORCEMENT_STRICT)
        self.assertEqual(broker.enforcement(), ENFORCEMENT_STRICT)

    def test_unknown_mode_in_policy_falls_back_to_strict(self):
        self.policy["enforcement"] = {"mode": "yolo"}
        self.write_policy()
        self.assertEqual(self.broker().enforcement(), ENFORCEMENT_STRICT)

    def test_unknown_mode_in_constructor_raises(self):
        with self.assertRaises(ValueError):
            self.broker(enforcement="yolo")


class AdmissionGateTests(EnforcementTestCase):
    def test_denied_by_admission_rules(self):
        decision = self.broker(ENFORCEMENT_STRICT).review_task(
            self.mcp_task(), user_role="developer"
        )
        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "admission_rules")
        self.assertIn("deny by default", decision["error"])

    def test_allow_rule_lets_request_reach_later_checks(self):
        self.policy["admission_rules"]["rules"] = [
            {"name": "allow_llm", "when": "task_type == 'llm'", "decision": "allow"}
        ]
        self.write_policy()
        task = {"type": "llm", "intent": "summarize", "payload": {"text": "hi"}}
        decision = self.broker(ENFORCEMENT_STRICT).review_task(task, user_role="developer")
        # It passed admission; whatever happens next is a later stage's call.
        self.assertNotEqual(decision.get("stage"), "admission_rules")

    def test_absent_ruleset_leaves_behaviour_unchanged(self):
        del self.policy["admission_rules"]
        self.write_policy()
        task = {"type": "llm", "intent": "summarize", "payload": {"text": "hi"}}
        decision = self.broker(ENFORCEMENT_STRICT).review_task(task, user_role="developer")
        self.assertTrue(decision["approved"])

    def test_matched_rule_is_recorded_on_approval(self):
        self.policy["admission_rules"]["rules"] = [
            {"name": "allow_llm", "when": "task_type == 'llm'", "decision": "allow"}
        ]
        self.write_policy()
        task = {"type": "llm", "intent": "summarize", "payload": {"text": "hi"}}
        decision = self.broker(ENFORCEMENT_STRICT).review_task(task, user_role="developer")
        self.assertEqual(decision.get("matched_rule"), "allow_llm")


class StrictModeTests(EnforcementTestCase):
    def test_strict_never_falls_back(self):
        with patch.object(mcp_proxy_bridge, "forward", return_value=PROXY_OK) as forward:
            result = self.broker(ENFORCEMENT_STRICT).process_task(
                self.mcp_task(), user_role="maintainer"
            )
        forward.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "admission_rules")

    def test_strict_denial_is_logged(self):
        self.broker(ENFORCEMENT_STRICT).process_task(self.mcp_task(), user_role="maintainer")
        self.assertIn("task_denied", self.event_names())


class PermissiveFallbackTests(EnforcementTestCase):
    def test_permissive_forwards_denied_mcp_task(self):
        with patch.object(mcp_proxy_bridge, "forward", return_value=PROXY_OK) as forward:
            result = self.broker(ENFORCEMENT_PERMISSIVE).process_task(
                self.mcp_task(), user_role="maintainer"
            )
        forward.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback"]["used"])
        self.assertEqual(result["provider"], "mcp_proxy")
        self.assertFalse(result["mediated"])
        self.assertTrue(result["assurance"]["requires_human_approval"])
        self.assertEqual(result["completion_status"], "pending_human_approval")
        self.assertIn(
            "unmediated_execution_path",
            [signal["code"] for signal in result["assurance"]["signals"]],
        )

    def test_fallback_is_audited_with_the_broker_reason(self):
        with patch.object(mcp_proxy_bridge, "forward", return_value=PROXY_OK):
            self.broker(ENFORCEMENT_PERMISSIVE).process_task(
                self.mcp_task(), user_role="maintainer"
            )
        events = [e for e in self.audit_events() if e["event"] == "fallback_to_mcp_proxy"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["trigger"], "policy_denial")
        self.assertIn("deny by default", events[0]["broker_reason"])
        self.assertEqual(events[0]["intent"], "search_thoughts")

    def test_shell_task_never_falls_back_even_in_permissive_mode(self):
        """The guard that matters: a denied shell command must not escape."""
        self.policy["fallback"]["allow_task_types"] = ["mcp", "shell"]
        self.write_policy()
        with patch.object(mcp_proxy_bridge, "forward", return_value=PROXY_OK) as forward:
            result = self.broker(ENFORCEMENT_PERMISSIVE).process_task(
                self.shell_task(), user_role="maintainer"
            )
        forward.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("fallback_refused", self.event_names())

    def test_refusal_records_why_the_fallback_was_not_used(self):
        with patch.object(mcp_proxy_bridge, "forward", return_value=PROXY_OK):
            self.broker(ENFORCEMENT_PERMISSIVE).process_task(
                self.shell_task(), user_role="maintainer"
            )
        refused = [e for e in self.audit_events() if e["event"] == "fallback_refused"]
        self.assertEqual(len(refused), 1)
        self.assertIn("never use the open proxy", refused[0]["refusal_reason"])

    def test_fallback_disabled_in_policy_is_respected(self):
        self.policy["fallback"]["enabled"] = False
        self.write_policy()
        with patch.object(mcp_proxy_bridge, "forward", return_value=PROXY_OK) as forward:
            result = self.broker(ENFORCEMENT_PERMISSIVE).process_task(
                self.mcp_task(), user_role="maintainer"
            )
        forward.assert_not_called()
        self.assertFalse(result["ok"])

    def test_task_type_not_allowed_for_fallback_is_refused(self):
        self.policy["fallback"]["allow_task_types"] = ["mcp"]
        self.write_policy()
        task = {"type": "llm", "intent": "summarize", "payload": {"text": "hi"}}
        with patch.object(mcp_proxy_bridge, "forward", return_value=PROXY_OK) as forward:
            self.broker(ENFORCEMENT_PERMISSIVE).process_task(task, user_role="developer")
        forward.assert_not_called()

    def test_proxy_failure_is_reported_not_swallowed(self):
        failure = {"ok": False, "error": "mcp_proxy unreachable: Connection refused"}
        with patch.object(mcp_proxy_bridge, "forward", return_value=failure):
            result = self.broker(ENFORCEMENT_PERMISSIVE).process_task(
                self.mcp_task(), user_role="maintainer"
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "fallback")
        self.assertIn("unreachable", result["error"])

    def test_on_policy_denial_can_be_switched_off(self):
        self.policy["fallback"]["on_policy_denial"] = False
        self.write_policy()
        with patch.object(mcp_proxy_bridge, "forward", return_value=PROXY_OK) as forward:
            self.broker(ENFORCEMENT_PERMISSIVE).process_task(
                self.mcp_task(), user_role="maintainer"
            )
        forward.assert_not_called()


class DecodeMcpResponseTests(unittest.TestCase):
    """The live OpenBrain endpoint answers tools/call with SSE framing."""

    def test_plain_json(self):
        decoded = mcp_proxy_bridge.decode_mcp_response('{"result": {"content": []}}')
        self.assertEqual(decoded, {"result": {"content": []}})

    def test_sse_framed_json(self):
        raw = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\n\n'
        decoded = mcp_proxy_bridge.decode_mcp_response(raw)
        self.assertEqual(decoded["id"], 1)

    def test_sse_takes_the_last_message(self):
        raw = (
            'event: message\ndata: {"result": {"seq": 1}}\n\n'
            'event: message\ndata: {"result": {"seq": 2}}\n\n'
        )
        decoded = mcp_proxy_bridge.decode_mcp_response(raw)
        self.assertEqual(decoded["result"]["seq"], 2)

    def test_sse_done_sentinel_is_skipped(self):
        raw = 'data: {"result": {"seq": 1}}\n\ndata: [DONE]\n\n'
        decoded = mcp_proxy_bridge.decode_mcp_response(raw)
        self.assertEqual(decoded["result"]["seq"], 1)

    def test_empty_body_raises(self):
        with self.assertRaises(ValueError):
            mcp_proxy_bridge.decode_mcp_response("   ")

    def test_unparseable_body_raises(self):
        with self.assertRaises(ValueError):
            mcp_proxy_bridge.decode_mcp_response("<html>gateway error</html>")

    def test_json_array_is_rejected(self):
        with self.assertRaises(ValueError):
            mcp_proxy_bridge.decode_mcp_response("[1, 2, 3]")


class BridgeEligibilityTests(unittest.TestCase):
    def config(self, **overrides):
        base = {
            "enabled": True,
            "url": "http://127.0.0.1:3000",
            "timeout": 5,
            "allow_task_types": ["mcp"],
            "on_policy_denial": True,
            "on_execution_failure": False,
        }
        base.update(overrides)
        return base

    def test_shell_is_hard_blocked_regardless_of_configuration(self):
        eligible, reason = mcp_proxy_bridge.is_eligible(
            {"type": "shell", "intent": "execute"},
            self.config(allow_task_types=["shell", "mcp"]),
        )
        self.assertFalse(eligible)
        self.assertIn("never", reason)

    def test_mcp_task_with_intent_is_eligible(self):
        eligible, reason = mcp_proxy_bridge.is_eligible(
            {"type": "mcp", "intent": "search_thoughts"}, self.config()
        )
        self.assertTrue(eligible)
        self.assertIsNone(reason)

    def test_missing_intent_is_not_eligible(self):
        eligible, _ = mcp_proxy_bridge.is_eligible({"type": "mcp"}, self.config())
        self.assertFalse(eligible)

    def test_disabled_config_is_not_eligible(self):
        eligible, _ = mcp_proxy_bridge.is_eligible(
            {"type": "mcp", "intent": "x"}, self.config(enabled=False)
        )
        self.assertFalse(eligible)

    def test_fallback_config_defaults_to_disabled(self):
        config = mcp_proxy_bridge.fallback_config({})
        self.assertFalse(config["enabled"])
        self.assertEqual(config["url"], "http://127.0.0.1:3000")


if __name__ == "__main__":
    unittest.main()
