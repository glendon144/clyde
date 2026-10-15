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

from ai_broker import AIBroker
from epistemic_validator import assess_output


class EpistemicValidatorTests(unittest.TestCase):
    def test_ordinary_grounded_text_is_provisionally_clear(self):
        assurance = assess_output(
            {"type": "llm", "intent": "summarize", "payload": {"text": "source"}},
            {
                "ok": True,
                "result": {"intent": "summarize", "text": "A concise summary."},
                "metadata": {"model": "local-test"},
            },
            "local",
        )

        self.assertEqual(assurance["status"], "clear")
        self.assertFalse(assurance["requires_human_approval"])
        self.assertEqual(assurance["completion_disposition"], "provisionally_clear")

    def test_unsupported_action_claim_requires_human_approval(self):
        assurance = assess_output(
            {"type": "llm", "intent": "analyze", "payload": {"text": "Do the work"}},
            {
                "ok": True,
                "result": {"text": "I have attached the requested spreadsheet."},
                "metadata": {"model": "local-test"},
            },
            "local",
        )

        self.assertEqual(assurance["status"], "suspicious")
        self.assertTrue(assurance["requires_human_approval"])
        self.assertIn(
            "unsupported_action_claim",
            [signal["code"] for signal in assurance["signals"]],
        )

    def test_missing_result_is_indeterminate(self):
        assurance = assess_output(
            {"type": "llm", "intent": "summarize", "payload": {}},
            {"ok": True, "result": None, "metadata": {}},
            "openai",
        )

        self.assertEqual(assurance["status"], "indeterminate")
        self.assertTrue(assurance["requires_human_approval"])

    def test_nonzero_shell_returncode_is_flagged(self):
        assurance = assess_output(
            {"type": "shell", "intent": "execute", "payload": {"command": "false"}},
            {
                "ok": True,
                "result": {"returncode": 1, "stdout": "", "stderr": ""},
                "metadata": {"argv": ["false"]},
            },
            "shell",
        )

        self.assertEqual(assurance["status"], "suspicious")
        self.assertIn(
            "shell_command_failed",
            [signal["code"] for signal in assurance["signals"]],
        )

    def test_declared_required_evidence_is_enforced(self):
        assurance = assess_output(
            {
                "type": "llm",
                "intent": "analyze",
                "payload": {"text": "inspect"},
                "expect": {
                    "evidence_required": True,
                    "required_result_fields": ["text"],
                    "required_metadata_fields": ["source_id"],
                },
            },
            {
                "ok": True,
                "result": {"text": "Analysis"},
                "metadata": {},
            },
            "local",
        )

        codes = [signal["code"] for signal in assurance["signals"]]
        self.assertIn("required_evidence_missing", codes)
        self.assertIn("missing_required_metadata_field", codes)
        self.assertTrue(assurance["requires_human_approval"])


class BrokerAssuranceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy_path = os.path.join(self.tmp.name, "policy.json")
        self.cache_path = os.path.join(self.tmp.name, "cache", "responses.json")
        self.budget_path = os.path.join(self.tmp.name, "state", "budget.json")
        self.status_path = os.path.join(self.tmp.name, "state", "providers.json")
        self.audit_path = os.path.join(self.tmp.name, "state", "audit.jsonl")
        policy = {
            "roles": {
                "developer": {
                    "allow_paths": [self.tmp.name],
                    "allow_providers": ["local"],
                    "deny_providers": [],
                    "allow_tasks": {"llm": ["summarize", "analyze"]},
                    "read_only": True,
                }
            },
            "routing": {
                "task_routes": {"llm": ["local"]},
                "intent_routes": {"summarize": ["local"], "analyze": ["local"]},
                "fallback_order": ["local"],
            },
            "budget": {"daily_global_limit": 1.0},
            "task_defaults": {"llm": {"cache_ttl_seconds": 3600}},
        }
        with open(self.policy_path, "w", encoding="utf-8") as fh:
            json.dump(policy, fh)

    def broker(self):
        return AIBroker(
            policy_path=self.policy_path,
            cache_path=self.cache_path,
            budget_ledger_path=self.budget_path,
            provider_status_path=self.status_path,
            audit_log_path=self.audit_path,
        )

    def task(self, text="source"):
        return {
            "type": "llm",
            "intent": "summarize",
            "cwd": self.tmp.name,
            "payload": {"text": text},
        }

    @patch("ai_broker.call_provider")
    def test_suspicious_result_is_returned_but_not_cached(self, call_provider):
        call_provider.return_value = {
            "ok": True,
            "result": {"text": "I have sent the final email."},
            "metadata": {"model": "test"},
        }

        result = self.broker().process_task(self.task(), user_role="developer")

        self.assertTrue(result["ok"])
        self.assertTrue(result["assurance"]["requires_human_approval"])
        self.assertEqual(result["completion_status"], "pending_human_approval")
        self.assertFalse(os.path.exists(self.cache_path))
        with open(self.audit_path, encoding="utf-8") as fh:
            events = [json.loads(line)["event"] for line in fh]
        self.assertIn("task_flagged_for_review", events)
        self.assertNotIn("task_completed", events)

    @patch("ai_broker.call_provider")
    def test_clear_result_is_assessed_and_cached(self, call_provider):
        call_provider.return_value = {
            "ok": True,
            "result": {"text": "A concise summary."},
            "metadata": {"model": "test"},
        }

        first = self.broker().process_task(self.task(), user_role="developer")
        second = self.broker().process_task(self.task(), user_role="developer")

        self.assertFalse(first["assurance"]["requires_human_approval"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["assurance"]["status"], "clear")
        call_provider.assert_called_once()


if __name__ == "__main__":
    unittest.main()
