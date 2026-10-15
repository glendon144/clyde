import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai_broker import AIBroker
from command_processor import process_request
from provider_adapters import (
    _extract_chat_message_text,
    _openai_response_debug,
    _provider_runtime_config,
    call_provider,
    estimate_cost,
)


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def base_policy(workdir):
    return {
        "provider_retry_max_attempts": 2,
        "provider_retry_attempts": {"shell": 1, "local": 1},
        "provider_failure_cooldown_seconds": 30,
        "provider_settings": {
            "local": {"api_key": "local-test", "base_url": "http://127.0.0.1:1/v1"},
            "openai": {"base_url": "https://example.invalid/v1", "model": "test-model"},
            "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"},
        },
        "budget": {
            "daily_global_limit": 1.0,
            "provider_daily_limits": {
                "shell": 0.0,
                "local": 0.0,
                "openai": 1.0,
                "deepseek": 1.0,
            },
        },
        "task_defaults": {
            "shell": {"cache_ttl_seconds": 0},
            "llm": {"cache_ttl_seconds": 3600},
            "ocr": {"cache_ttl_seconds": 3600},
        },
        "routing": {
            "task_routes": {
                "shell": ["shell"],
                "llm": ["openai", "local"],
                "ocr": ["local"],
            },
            "intent_routes": {
                "summarize": ["openai", "local"],
                "analyze": ["openai", "local"],
                "ocr_extract": ["local"],
            },
            "fallback_order": ["openai", "local"],
        },
        "roles": {
            "developer": {
                "read_only": True,
                "allow_paths": [workdir],
                "allow_providers": ["shell", "openai", "local"],
                "deny_providers": [],
                "allow_tasks": {
                    "shell": {
                        "pwd": {"allow_positionals": False},
                        "ls": {
                            "allowed_flags": ["-a", "-l", "-la", "-al"],
                            "max_positionals": 1,
                        },
                        "find": {
                            "options_with_values": {
                                "-maxdepth": {"type": "int"},
                                "-type": {"values": ["f", "d"]},
                            },
                            "min_positionals": 1,
                            "max_positionals": 2,
                        },
                    },
                    "llm": ["summarize", "analyze"],
                    "ocr": ["ocr_extract"],
                },
            }
        },
    }


class BrokerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = os.path.join(self.tmp.name, "workspace")
        os.makedirs(self.workdir, exist_ok=True)

        self.policy_path = os.path.join(self.tmp.name, "policy.json")
        self.cache_path = os.path.join(self.tmp.name, "cache", "responses.json")
        self.budget_path = os.path.join(self.tmp.name, "state", "budget_ledger.json")
        self.status_path = os.path.join(self.tmp.name, "state", "provider_status.json")
        self.audit_path = os.path.join(self.tmp.name, "state", "audit.jsonl")
        self.policy = base_policy(self.workdir)
        write_json(self.policy_path, self.policy)

    def broker(self):
        return AIBroker(
            policy_path=self.policy_path,
            cache_path=self.cache_path,
            budget_ledger_path=self.budget_path,
            provider_status_path=self.status_path,
            audit_log_path=self.audit_path,
        )

    def shell_task(self, command):
        return {
            "type": "shell",
            "intent": "execute",
            "cwd": self.workdir,
            "payload": {"command": command},
        }

    def llm_task(self, text="Summarize this text."):
        return {
            "type": "llm",
            "intent": "summarize",
            "cwd": self.workdir,
            "payload": {"text": text},
        }


class PolicyTests(BrokerTestCase):
    def test_allows_policy_approved_read_only_shell_command(self):
        result = self.broker().process_task(self.shell_task("pwd"), user_role="developer")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["provider"], "shell")
        self.assertEqual(result["result"]["returncode"], 0)
        self.assertEqual(
            os.path.realpath(result["result"]["stdout"].strip()),
            os.path.realpath(self.workdir),
        )

    def test_denies_shell_metacharacters_before_execution(self):
        decision = self.broker().review_task(
            self.shell_task("pwd; echo bypass"),
            user_role="developer",
        )

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "policy")
        self.assertIn("metacharacters", decision["error"])

    def test_denies_read_only_write_commands(self):
        decision = self.broker().review_task(
            self.shell_task("rm file.txt"),
            user_role="developer",
        )

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "policy")
        self.assertIn("not allowed", decision["error"])

    def test_denies_read_only_find_delete_argument(self):
        decision = self.broker().review_task(
            self.shell_task("find . -delete"),
            user_role="developer",
        )

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "policy")
        self.assertIn("-delete", decision["error"])

    def test_denies_cwd_outside_role_allowed_paths(self):
        task = self.shell_task("pwd")
        task["cwd"] = tempfile.gettempdir()

        decision = self.broker().review_task(task, user_role="developer")

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "policy")
        self.assertIn("not allowed", decision["error"])


class RoutingAndExecutionTests(BrokerTestCase):
    def test_deepseek_is_not_selected_until_role_policy_allows_it(self):
        task = self.llm_task("Use DeepSeek only if policy allows it.")
        task["provider"] = "deepseek"

        decision = self.broker().review_task(task, user_role="developer")

        self.assertTrue(decision["approved"], decision)
        self.assertNotEqual(decision["provider"], "deepseek")
        rejected = [
            item for item in decision["provider_attempts"]
            if item["provider"] == "deepseek" and item.get("stage") == "provider_policy"
        ]
        self.assertTrue(rejected, decision["provider_attempts"])

    def test_deepseek_is_available_when_role_policy_allows_it(self):
        self.policy["roles"]["developer"]["allow_providers"].append("deepseek")
        write_json(self.policy_path, self.policy)
        task = self.llm_task("Use DeepSeek when configured.")
        task["provider"] = "deepseek"

        decision = self.broker().review_task(task, user_role="developer")

        self.assertTrue(decision["approved"], decision)
        self.assertEqual(decision["provider"], "deepseek")

    def test_clear_provider_status_only_clears_named_provider(self):
        broker = self.broker()
        broker.set_provider_status("deepseek", False, "stale failure", cooldown_seconds=60)
        broker.set_provider_status("openai", False, "other failure", cooldown_seconds=60)

        cleared = broker.clear_provider_status("deepseek")

        self.assertTrue(cleared)
        self.assertTrue(broker.provider_ok("deepseek"))
        self.assertFalse(broker.provider_ok("openai"))
        self.assertNotIn("deepseek", broker.provider_status)
        self.assertIn("openai", broker.provider_status)

    def test_provider_cooldown_still_blocks_when_not_cleared(self):
        broker = self.broker()
        broker.set_provider_status("deepseek", False, "temporary failure", cooldown_seconds=60)

        self.assertFalse(broker.provider_ok("deepseek"))

    def test_clear_provider_status_returns_false_for_missing_provider(self):
        broker = self.broker()

        self.assertFalse(broker.clear_provider_status("deepseek"))

    def test_provider_health_causes_review_to_skip_unavailable_provider(self):
        broker = self.broker()
        broker.set_provider_status("openai", False, "test outage", cooldown_seconds=60)

        decision = broker.review_task(self.llm_task(), user_role="developer")

        self.assertTrue(decision["approved"], decision)
        self.assertEqual(decision["provider"], "local")
        self.assertFalse(broker.provider_ok("openai"))

    def test_explicit_provider_remains_first_choice_over_router_hint(self):
        task = self.llm_task("Use the local provider explicitly.")
        task["provider"] = "local"

        decision = self.broker().review_task(task, user_role="developer")

        self.assertTrue(decision["approved"], decision)
        self.assertEqual(decision["provider"], "local")
        self.assertEqual(decision["provider_attempts"][0]["provider"], "local")

    def test_budget_limit_denies_external_provider_when_no_fallback_fits(self):
        self.policy["routing"]["task_routes"]["llm"] = ["openai"]
        self.policy["routing"]["intent_routes"]["summarize"] = ["openai"]
        self.policy["routing"]["fallback_order"] = ["openai"]
        self.policy["budget"]["daily_global_limit"] = 0.000001
        write_json(self.policy_path, self.policy)

        decision = self.broker().review_task(
            self.llm_task("This text is long enough to exceed the tiny test budget."),
            user_role="developer",
        )

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "routing")
        self.assertEqual(decision["provider_attempts"][0]["stage"], "budget")


    def test_per_task_budget_limit_denies_large_request_before_daily_limit(self):
        self.policy["routing"]["task_routes"]["llm"] = ["openai"]
        self.policy["routing"]["intent_routes"]["summarize"] = ["openai"]
        self.policy["routing"]["fallback_order"] = ["openai"]
        self.policy["budget"]["daily_global_limit"] = 10.0
        self.policy["budget"]["per_task_limit"] = 0.00001
        write_json(self.policy_path, self.policy)

        decision = self.broker().review_task(
            self.llm_task("This text is long enough to exceed the per-task test budget."),
            user_role="developer",
        )

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "routing")
        self.assertEqual(decision["provider_attempts"][0]["stage"], "budget")
        self.assertIn("per-task", decision["provider_attempts"][0]["reason"])

    def test_agent_fanout_requires_explicit_swarm_approval(self):
        self.policy["agent_controls"] = {
            "default_max_child_tasks": 0,
            "hard_max_child_tasks": 4,
            "require_explicit_swarm_approval": True,
        }
        write_json(self.policy_path, self.policy)
        task = self.llm_task("Try to spawn helpers.")
        task["agent"] = {"child_task_count": 2}

        decision = self.broker().review_task(task, user_role="developer")

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "agent_controls")
        self.assertIn("explicit swarm approval", decision["error"])

    def test_agent_fanout_hard_limit_applies_even_with_approval(self):
        self.policy["agent_controls"] = {
            "default_max_child_tasks": 0,
            "hard_max_child_tasks": 4,
            "require_explicit_swarm_approval": True,
        }
        write_json(self.policy_path, self.policy)
        task = self.llm_task("Try to spawn too many helpers.")
        task["agent"] = {"child_task_count": 5, "swarm_approved": True}

        decision = self.broker().review_task(task, user_role="developer")

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "agent_controls")
        self.assertIn("hard limit", decision["error"])

    @patch("ai_broker.call_provider")
    def test_successful_llm_response_is_cached_and_not_called_twice(self, call_provider):
        call_provider.return_value = {
            "ok": True,
            "result": {"intent": "summarize", "text": "cached summary"},
            "metadata": {"model": "test"},
        }
        broker = self.broker()
        task = self.llm_task("Cache me.")

        first = broker.process_task(task, user_role="developer")
        second = broker.process_task(task, user_role="developer")

        self.assertTrue(first["ok"], first)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["result"]["text"], "cached summary")
        self.assertEqual(call_provider.call_count, 1)

    @patch("ai_broker.call_provider")
    def test_transient_failure_retries_then_fails_over_to_next_provider(self, call_provider):
        call_provider.side_effect = [
            {"ok": False, "error": "openai timeout; retry later"},
            {"ok": False, "error": "openai timeout; retry later"},
            {
                "ok": True,
                "result": {"intent": "summarize", "text": "local fallback"},
                "metadata": {"model": "local-test"},
            },
        ]
        broker = self.broker()

        result = broker.process_task(self.llm_task("Use fallback."), user_role="developer")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["provider"], "local")
        self.assertEqual(call_provider.call_count, 3)
        self.assertEqual(
            [(item["provider"], item["attempt"], item["ok"]) for item in result["execution_attempts"]],
            [("openai", 1, False), ("openai", 2, False), ("local", 1, True)],
        )
        self.assertFalse(broker.provider_ok("openai"))

    @patch("provider_adapters._call_openai_compatible")
    def test_deepseek_uses_openai_compatible_adapter_path(self, adapter):
        adapter.return_value = {"ok": True, "result": {"text": "ok"}, "metadata": {}}
        task = self.llm_task("Dispatch through the shared adapter.")

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
            result = call_provider("deepseek", task, policy={})

        self.assertTrue(result["ok"], result)
        adapter.assert_called_once()
        self.assertEqual(adapter.call_args.args[0], "deepseek")

    def test_openai_compatible_text_extraction_handles_content_blocks(self):
        message = SimpleNamespace(content=[
            {"type": "text", "text": "DeepSeek provider"},
            SimpleNamespace(type="text", text="test OK"),
        ])

        self.assertEqual(
            _extract_chat_message_text(message),
            "DeepSeek provider\ntest OK",
        )

    def test_openai_compatible_text_extraction_does_not_use_reasoning_content(self):
        message = SimpleNamespace(content="", reasoning_content="hidden reasoning")

        self.assertEqual(_extract_chat_message_text(message), "")

    def test_openai_compatible_empty_text_debug_includes_safe_structure(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="", reasoning_content="hidden reasoning", refusal=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=0, total_tokens=4),
        )

        debug = _openai_response_debug(response)

        self.assertEqual(debug["response_object_type"], "SimpleNamespace")
        self.assertEqual(debug["choices"][0]["message"]["likely_text_bearing_fields"]["content"]["type"], "str")
        self.assertNotIn("preview", debug["choices"][0]["message"]["likely_text_bearing_fields"]["reasoning_content"])
        self.assertEqual(debug["usage"]["total_tokens"], 4)

    def test_deepseek_runtime_uses_env_base_url_override(self):
        with patch.dict(os.environ, {"DEEPSEEK_BASE_URL": "https://deepseek.example"}):
            runtime = _provider_runtime_config("deepseek", policy={})

        self.assertEqual(runtime["base_url"], "https://deepseek.example")
        self.assertEqual(runtime["model"], "deepseek-v4-flash")

    def test_deepseek_v4_cost_estimates_are_model_specific(self):
        flash = estimate_cost("deepseek", {
            "type": "llm",
            "intent": "summarize",
            "payload": {"text": "abcdef", "model": "deepseek-v4-flash"},
        })
        pro = estimate_cost("deepseek", {
            "type": "llm",
            "intent": "summarize",
            "payload": {"text": "abcdef", "model": "deepseek-v4-pro"},
        })

        self.assertGreater(flash, 0)
        self.assertGreater(pro, flash)

    def test_deepseek_workload_scaling_uses_supported_unit_fields(self):
        base_task = {
            "type": "llm",
            "intent": "summarize",
            "payload": {"text": "abcdef", "model": "deepseek-v4-flash"},
        }
        base_cost = estimate_cost("deepseek", base_task)

        for field in ("unit_count", "frame_count", "batch_size", "sample_count"):
            with self.subTest(field=field):
                scaled = estimate_cost("deepseek", {
                    "type": "llm",
                    "intent": "summarize",
                    "payload": {
                        "text": "abcdef",
                        "model": "deepseek-v4-flash",
                        field: 7,
                    },
                })
                self.assertEqual(scaled, round(base_cost * 7, 6))

    def test_cost_estimate_scales_with_declared_work_units(self):
        single = {
            "type": "ocr",
            "intent": "ocr_extract",
            "payload": {"image_path": "frame.jpg"},
        }
        batch = {
            "type": "ocr",
            "intent": "ocr_extract",
            "payload": {"image_path": "frame.jpg", "frame_count": 12},
        }

        self.assertEqual(
            estimate_cost("google_vision", batch),
            round(estimate_cost("google_vision", single) * 12, 6),
        )

    def test_cache_key_for_ocr_uses_image_content_not_image_path(self):
        image_a = os.path.join(self.tmp.name, "a.jpg")
        image_b = os.path.join(self.tmp.name, "b.jpg")
        with open(image_a, "wb") as fh:
            fh.write(b"same-image-content")
        with open(image_b, "wb") as fh:
            fh.write(b"same-image-content")

        broker = self.broker()
        task_a = {
            "type": "ocr",
            "intent": "ocr_extract",
            "cwd": self.workdir,
            "provider": "local",
            "payload": {"image_path": image_a},
        }
        task_b = {
            "type": "ocr",
            "intent": "ocr_extract",
            "cwd": self.workdir,
            "provider": "local",
            "payload": {"image_path": image_b},
        }

        self.assertEqual(broker.task_cache_key(task_a), broker.task_cache_key(task_b))

    def test_provider_registry_blocks_provider_even_for_maintainer_wildcard(self):
        self.policy["provider_registry"] = {
            "grok": {
                "status": "blocked",
                "enabled": False,
                "summary": "-1 [DO NOT USE UNDER ANY CIRCUMSTANCES]",
                "policy": {"allow_routing": False},
            }
        }
        self.policy["roles"]["maintainer"] = {
            "read_only": False,
            "allow_paths": [self.workdir],
            "allow_providers": ["*"],
            "deny_providers": [],
            "allow_tasks": {"llm": ["summarize"]},
        }
        write_json(self.policy_path, self.policy)
        task = self.llm_task("Try the explicitly blocked provider.")
        task["provider"] = "grok"

        decision = self.broker().review_task(task, user_role="maintainer")

        self.assertFalse(decision["approved"])
        self.assertEqual(decision["stage"], "routing")
        self.assertIn("blocked by policy", str(decision["provider_attempts"][0]["reason"]))

    def test_status_report_includes_blocked_provider_and_recommendations(self):
        self.policy["provider_registry"] = {
            "local": {
                "status": "healthy",
                "scope": "local",
                "enabled": True,
                "quality_scores": {"summarize": 0.70, "analyze": 0.60, "classify": 0.70},
            },
            "openai": {
                "status": "healthy",
                "scope": "cloud",
                "enabled": True,
                "quality_scores": {"summarize": 0.90, "analyze": 0.90, "classify": 0.90},
            },
            "grok": {
                "status": "blocked",
                "scope": "cloud",
                "enabled": False,
                "summary": "-1 [DO NOT USE UNDER ANY CIRCUMSTANCES]",
                "quality_scores": {"summarize": -1.0},
                "policy": {"allow_routing": False, "allow_fallback": False, "allow_benchmark": False},
            },
        }
        write_json(self.policy_path, self.policy)

        report = self.broker().build_status_report(user_role="developer")

        self.assertEqual(report["schema_version"], "ai-broker-status/v1")
        providers = {item["name"]: item for item in report["providers"]}
        self.assertIn("grok", providers)
        self.assertEqual(providers["grok"]["status"], "blocked")
        self.assertFalse(providers["grok"]["eligible"])
        self.assertIn("summarize", report["recommendations"])
        self.assertIsNotNone(report["recommendations"]["summarize"]["provider"])

    def test_write_status_report_persists_json(self):
        self.policy["provider_registry"] = {
            "local": {
                "status": "healthy",
                "scope": "local",
                "enabled": True,
                "quality_scores": {"summarize": 0.70},
            }
        }
        write_json(self.policy_path, self.policy)
        output = os.path.join(self.tmp.name, "state", "status_report.json")

        report = self.broker().write_status_report(output_path=output, user_role="developer")

        self.assertTrue(os.path.exists(output))
        with open(output, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["schema_version"], report["schema_version"])


class CompatibilityWrapperTests(BrokerTestCase):
    def test_command_processor_wraps_legacy_shell_request(self):
        legacy = {"name": "bash", "arguments": {"command": "pwd"}}

        with patch("command_processor.AIBroker") as broker_cls:
            broker_cls.return_value.process_task.return_value = {"ok": True}
            result = process_request(legacy, user_role="developer", cwd=self.workdir)

        self.assertEqual(result, {"ok": True})
        task = broker_cls.return_value.process_task.call_args.args[0]
        self.assertEqual(task["type"], "shell")
        self.assertEqual(task["intent"], "execute")
        self.assertEqual(task["cwd"], self.workdir)
        self.assertEqual(task["payload"]["command"], "pwd")


if __name__ == "__main__":
    unittest.main()
