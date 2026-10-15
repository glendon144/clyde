"""Tests for the admission rule subset evaluator and its deny-by-default engine."""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from policy_rules import (
    MAX_EXPRESSION_CHARS,
    RuleEngine,
    RuleError,
    SafeExpression,
    build_context,
)


class SafeExpressionSyntaxTests(unittest.TestCase):
    """The whitelist must reject anything that could reach the interpreter."""

    def assert_rejected(self, source):
        with self.assertRaises(RuleError, msg=f"should have rejected: {source}"):
            SafeExpression(source)

    def test_rejects_dunder_import(self):
        self.assert_rejected('__import__("os")')

    def test_rejects_attribute_access(self):
        self.assert_rejected('user.__class__')

    def test_rejects_method_call(self):
        self.assert_rejected('user.startswith("g")')

    def test_rejects_lambda(self):
        self.assert_rejected('(lambda: 1)()')

    def test_rejects_comprehension(self):
        self.assert_rejected('[x for x in argv]')

    def test_rejects_walrus_assignment(self):
        self.assert_rejected('(x := 1) == 1')

    def test_rejects_fstring(self):
        self.assert_rejected('f"{user}" == "gross"')

    def test_rejects_arithmetic_on_strings(self):
        # BinOp is not in the whitelist at all.
        self.assert_rejected('1 + 1 == 2')

    def test_rejects_unknown_function(self):
        self.assert_rejected('system("rm -rf /")')

    def test_rejects_keyword_arguments(self):
        self.assert_rejected('get(payload, "k", default=1)')

    def test_rejects_empty_expression(self):
        self.assert_rejected('   ')

    def test_rejects_syntax_error(self):
        self.assert_rejected('user ==')

    def test_rejects_oversized_expression(self):
        self.assert_rejected('user == "g" and ' * MAX_EXPRESSION_CHARS)

    def test_rejects_indirect_call(self):
        self.assert_rejected('get(payload, "f")()')


class SafeExpressionEvaluationTests(unittest.TestCase):
    def evaluate(self, source, **context):
        return SafeExpression(source).evaluate(context)

    def test_equality(self):
        self.assertTrue(self.evaluate('user == "gross"', user="gross"))
        self.assertFalse(self.evaluate('user == "gross"', user="mallory"))

    def test_boolean_operators_short_circuit(self):
        # argv[0] would raise on an empty list; the guard must prevent that.
        self.assertFalse(
            self.evaluate('len(argv) > 0 and argv[0] == "ls"', argv=[])
        )

    def test_membership(self):
        self.assertTrue(self.evaluate('role in ["developer", "maintainer"]', role="developer"))
        self.assertFalse(self.evaluate('role in ["developer"]', role="guest"))

    def test_not_operator(self):
        self.assertTrue(self.evaluate('not (role == "guest")', role="developer"))

    def test_subscript(self):
        self.assertTrue(self.evaluate('argv[0] == "ls"', argv=["ls", "-la"]))

    def test_helper_startswith(self):
        self.assertTrue(self.evaluate('startswith(command, "git ")', command="git status"))

    def test_helper_any_of(self):
        self.assertTrue(self.evaluate('any_of(intent, ["a", "b"])', intent="b"))

    def test_helper_matches_is_anchored(self):
        self.assertTrue(self.evaluate('matches(intent, "search_.*")', intent="search_thoughts"))
        # fullmatch, so a prefix match alone is not enough
        self.assertFalse(self.evaluate('matches(intent, "search")', intent="search_thoughts"))

    def test_helper_path_under(self):
        self.assertTrue(self.evaluate('path_under(cwd, "/tmp")', cwd="/tmp/inner"))
        self.assertFalse(self.evaluate('path_under(cwd, "/tmp")', cwd="/etc"))

    def test_helper_path_under_rejects_prefix_lookalike(self):
        self.assertFalse(self.evaluate('path_under(cwd, "/tmp/a")', cwd="/tmp/abc"))

    def test_helper_get(self):
        self.assertTrue(self.evaluate('get(payload, "k") == 1', payload={"k": 1}))

    def test_unknown_name_raises(self):
        with self.assertRaises(RuleError):
            self.evaluate('nonexistent == 1')

    def test_regex_length_cap(self):
        with self.assertRaises(RuleError):
            self.evaluate('matches(intent, pattern)', intent="x", pattern="a" * 5000)


class RuleEngineTests(unittest.TestCase):
    def engine(self, rules, default="deny"):
        return RuleEngine({"default": default, "rules": rules})

    def test_unconfigured_engine_is_inert(self):
        engine = RuleEngine(None)
        self.assertFalse(engine.configured)
        self.assertTrue(engine.evaluate({}).allowed)

    def test_empty_ruleset_denies(self):
        decision = self.engine([]).evaluate({"user": "gross"})
        self.assertFalse(decision.allowed)
        self.assertIn("deny by default", decision.reason)

    def test_allow_rule_matches(self):
        engine = self.engine(
            [{"name": "owner", "when": 'user == "gross"', "decision": "allow"}]
        )
        decision = engine.evaluate({"user": "gross"})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.rule, "owner")

    def test_no_match_denies_even_with_allow_rules_present(self):
        engine = self.engine(
            [{"name": "owner", "when": 'user == "gross"', "decision": "allow"}]
        )
        self.assertFalse(engine.evaluate({"user": "mallory"}).allowed)

    def test_first_match_wins(self):
        engine = self.engine(
            [
                {"name": "deny_first", "when": 'user == "gross"', "decision": "deny"},
                {"name": "allow_later", "when": 'user == "gross"', "decision": "allow"},
            ]
        )
        decision = engine.evaluate({"user": "gross"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule, "deny_first")

    def test_disabled_rule_is_skipped(self):
        engine = self.engine(
            [
                {"name": "off", "when": 'user == "gross"', "decision": "allow", "enabled": False},
            ]
        )
        self.assertFalse(engine.evaluate({"user": "gross"}).allowed)

    def test_malformed_rule_is_dropped_not_fatal(self):
        engine = self.engine(
            [
                {"name": "bad", "when": '__import__("os")', "decision": "allow"},
                {"name": "good", "when": 'user == "gross"', "decision": "allow"},
            ]
        )
        self.assertEqual(len(engine.load_errors), 1)
        decision = engine.evaluate({"user": "gross"})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.rule, "good")
        self.assertTrue(decision.errors)

    def test_rule_that_raises_at_runtime_does_not_allow(self):
        engine = self.engine(
            [{"name": "boom", "when": 'missing_name == 1', "decision": "allow"}]
        )
        decision = engine.evaluate({"user": "gross"})
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.errors)

    def test_invalid_decision_value_drops_rule(self):
        engine = self.engine(
            [{"name": "typo", "when": "user == 'gross'", "decision": "permit"}]
        )
        self.assertEqual(len(engine.load_errors), 1)
        self.assertFalse(engine.evaluate({"user": "gross"}).allowed)

    def test_default_allow_is_honoured_when_configured(self):
        engine = self.engine([], default="allow")
        self.assertTrue(engine.evaluate({"user": "anyone"}).allowed)

    def test_unknown_default_falls_back_to_deny(self):
        engine = RuleEngine({"default": "maybe", "rules": []})
        self.assertEqual(engine.default, "deny")
        self.assertFalse(engine.evaluate({}).allowed)


class BuildContextTests(unittest.TestCase):
    def test_shell_task_context(self):
        context = build_context(
            task={"type": "shell", "cwd": "/tmp", "payload": {"command": "ls -la"}},
            user_role="developer",
            user="gross",
            mode="normal",
            enforcement="strict",
            risk_tier="low",
        )
        self.assertEqual(context["task_type"], "shell")
        self.assertEqual(context["argv"], ["ls", "-la"])
        self.assertEqual(context["command"], "ls -la")
        self.assertEqual(context["role"], "developer")

    def test_mcp_task_context_extracts_server(self):
        context = build_context(
            task={"type": "mcp", "intent": "search_thoughts", "provider": "mcp_openbrain"},
            user_role="maintainer",
            user="gross",
            mode="normal",
            enforcement="permissive",
            risk_tier="low",
        )
        self.assertEqual(context["mcp_server"], "openbrain")
        self.assertEqual(context["intent"], "search_thoughts")

    def test_non_dict_payload_is_tolerated(self):
        context = build_context(
            task={"type": "llm", "payload": "not a dict"},
            user_role="developer",
            user="gross",
            mode="normal",
            enforcement="strict",
            risk_tier="low",
        )
        self.assertEqual(context["payload"], {})
        self.assertEqual(context["payload_keys"], [])


if __name__ == "__main__":
    unittest.main()
