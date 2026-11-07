"""The adapter registry and base interface (Phase 0 skeleton)."""

import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import ADAPTERS, all_adapters, get_adapter
from one_setup_all_agents.adapters._stub import StubAdapter
from one_setup_all_agents.adapters.base import ToolAdapter
from one_setup_all_agents.model import Env, Kind, Scope, Unsupported


def _env():
    root = Path("unused")
    return Env(
        home=root,
        platform="linux",
        environ={},
        config_dir=root / "config",
        state_dir=root / "state",
    )


class RegistryTests(unittest.TestCase):
    def test_four_tools_in_porting_order(self):
        self.assertEqual([a.id for a in all_adapters()], ["opencode", "claude", "codex", "agy"])

    def test_ids_are_unique_and_labels_present(self):
        ids = [cls.id for cls in ADAPTERS]
        self.assertEqual(len(ids), len(set(ids)))
        for cls in ADAPTERS:
            self.assertTrue(cls.label)
            self.assertTrue(issubclass(cls, ToolAdapter))

    def test_get_adapter(self):
        self.assertEqual(get_adapter("codex").label, "Codex")
        with self.assertRaises(KeyError):
            get_adapter("nope")

    def test_v01_capabilities(self):
        v01 = {Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS}
        for adapter in all_adapters():
            self.assertEqual(set(adapter.capabilities), v01, adapter.id)


class StubBehaviorTests(unittest.TestCase):
    def test_required_methods_raise_clear_not_implemented(self):
        env = _env()
        for adapter in all_adapters():
            if not isinstance(adapter, StubAdapter):
                continue  # ported adapters are covered by their own test module and the contract suite
            calls = (
                lambda a=adapter: a.detect(env),
                lambda a=adapter: a.paths(env, Scope.GLOBAL),
                lambda a=adapter: a.read_instructions(env, Scope.GLOBAL),
                lambda a=adapter: a.read_mcp(env, Scope.GLOBAL),
                lambda a=adapter: a.read_skills(env, Scope.GLOBAL),
                lambda a=adapter: a.plan_instructions(env, Scope.GLOBAL, "", frozenset()),
                lambda a=adapter: a.plan_mcp(env, Scope.GLOBAL, {}, frozenset()),
                lambda a=adapter: a.plan_skills(env, Scope.GLOBAL, [], frozenset()),
            )
            for call in calls:
                with self.assertRaises(NotImplementedError) as ctx:
                    call()
                message = str(ctx.exception)
                self.assertIn(adapter.label, message)
                self.assertIn("not implemented yet", message)

    def test_optional_kinds_default_to_unsupported_not_silence(self):
        env = _env()
        for tool in ("opencode", "claude", "codex", "agy"):
            adapter = get_adapter(tool)
            for plan in (adapter.plan_hooks, adapter.plan_permissions, adapter.plan_memory):
                ops = plan(env, Scope.GLOBAL, None, frozenset())
                self.assertEqual(len(ops), 1)
                self.assertIsInstance(ops[0], Unsupported)
                self.assertEqual(ops[0].tool, tool)

    def test_only_antigravity_is_still_a_stub(self):
        stubs = [a.id for a in all_adapters() if isinstance(a, StubAdapter)]
        self.assertEqual(stubs, ["agy"])


if __name__ == "__main__":
    unittest.main()
