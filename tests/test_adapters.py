"""The adapter registry and base interface (Phase 0 skeleton)."""

import importlib.util
import inspect
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import ADAPTERS, all_adapters, get_adapter
from one_setup_all_agents.adapters.base import ToolAdapter
from one_setup_all_agents.model import Detection, Env, Kind, Paths, Scope, Unsupported


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


class OptionalKindsTests(unittest.TestCase):
    def test_optional_kinds_default_to_unsupported_not_silence(self):
        env = _env()
        for tool in ("opencode", "claude", "codex", "agy"):
            adapter = get_adapter(tool)
            for plan in (adapter.plan_hooks, adapter.plan_permissions, adapter.plan_memory):
                ops = plan(env, Scope.GLOBAL, None, frozenset())
                self.assertEqual(len(ops), 1)
                self.assertIsInstance(ops[0], Unsupported)
                self.assertEqual(ops[0].tool, tool)

    def test_the_base_class_defaults_report_the_optional_kinds_directly(self):
        class Bare(ToolAdapter):
            id = "bare"
            label = "Bare"
            capabilities = frozenset()

            def detect(self, env):
                return Detection(installed=False)

            def paths(self, env, scope):
                return Paths()

            def read_instructions(self, env, scope):
                return None

            def read_mcp(self, env, scope):
                return {}

            def read_skills(self, env, scope):
                return []

            def plan_instructions(self, env, scope, desired, owned):
                return []

            def plan_mcp(self, env, scope, desired, owned):
                return []

            def plan_skills(self, env, scope, desired, owned):
                return []

        adapter = Bare()
        for kind, plan in (
            (Kind.HOOKS, adapter.plan_hooks),
            (Kind.PERMISSIONS, adapter.plan_permissions),
            (Kind.MEMORY, adapter.plan_memory),
        ):
            (op,) = plan(_env(), Scope.GLOBAL, None, frozenset())
            self.assertEqual((type(op), op.tool, op.kind), (Unsupported, "bare", kind))
        self.assertEqual(adapter.normalize_mcp({}), {})
        self.assertEqual(adapter.normalize_instructions("x"), "x")
        self.assertEqual(adapter.notes(), [])
        self.assertEqual(adapter.wanted_links(_env(), Scope.GLOBAL, []), [])


class NoStubsTests(unittest.TestCase):
    def test_the_stub_module_is_gone_and_every_adapter_implements_the_interface(self):
        self.assertIsNone(importlib.util.find_spec("one_setup_all_agents.adapters._stub"))
        for cls in ADAPTERS:
            self.assertFalse(cls.__abstractmethods__, cls.id)
            self.assertNotIn("NotImplementedError", inspect.getsource(cls), cls.id)


if __name__ == "__main__":
    unittest.main()
