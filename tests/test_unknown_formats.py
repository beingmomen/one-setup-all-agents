"""Cross-adapter refusal tests proving unreadable config formats are never overwritten."""

import unittest

from tests.engine_support import EngineCase, FakeRunner

import _bootstrap  # noqa: F401
from one_setup_all_agents.adapters import ADAPTERS
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.model import (
    Conflict,
    Detection,
    EditFile,
    Kind,
    McpServer,
    Plan,
    RunCli,
    Scope,
    SkillRef,
)
from one_setup_all_agents.reader import read_all, read_tool

PLANTED_SECRET = "sk-" + "tokensecret" * 3
GARBAGE_PAYLOAD = f'{{"broken": "{PLANTED_SECRET}", invalid_json: [}}\n'.encode("utf-8")


class UnknownFormatsRefusalTests(EngineCase):
    def test_all_adapters_refuse_unreadable_mcp_config_without_overwriting(self):
        demo_server = McpServer(name="demo", command="demo-bin")

        for cls in ADAPTERS:
            adapter = cls()
            paths = adapter.paths(self.env, Scope.GLOBAL)
            if not paths.mcp:
                continue

            # 1. Write garbage to MCP config file
            self.write(paths.mcp, GARBAGE_PAYLOAD)
            before_listing = self.listing(self.home)

            reading = read_tool(adapter, self.env)

            # Assert reading reports Problem and masks secrets
            self.assertEqual(len(reading.problems), 1, f"Expected 1 problem for {adapter.id}")
            prob = reading.problems[0]
            self.assertEqual(prob.kind, Kind.MCP, adapter.id)
            self.assertEqual(reading.mcp, {}, adapter.id)
            self.assertNotIn(PLANTED_SECRET, prob.message, adapter.id)
            self.assertNotIn(PLANTED_SECRET, repr(reading), adapter.id)
            self.assertEqual(self.listing(self.home), before_listing, adapter.id)

            # 2. Planning MCP produces Conflict and no write/run op targeting the file
            plan_ops = adapter.plan_mcp(self.env, Scope.GLOBAL, {"demo": demo_server}, frozenset())
            mcp_str = str(paths.mcp)

            for op in plan_ops:
                if isinstance(op, EditFile):
                    self.assertNotEqual(
                        op.path,
                        paths.mcp,
                        f"{adapter.id} generated EditFile targeting unreadable config",
                    )
                elif isinstance(op, RunCli):
                    self.assertNotIn(
                        mcp_str,
                        " ".join(op.argv),
                        f"{adapter.id} generated RunCli targeting unreadable config",
                    )

            conflicts = [op for op in plan_ops if isinstance(op, Conflict)]
            self.assertTrue(conflicts, f"Expected Conflict op for {adapter.id}")

            # 3. Applying the plan with real engine leaves the garbage bytes untouched
            runner = FakeRunner()
            apply_plan(self.env, Plan(plan_ops), label="test-unknown-mcp", run_cli=runner)
            self.assertEqual(
                paths.mcp.read_bytes(),
                GARBAGE_PAYLOAD,
                f"Garbage MCP file was modified for {adapter.id}",
            )

    def test_adapter_with_skills_manifest_refuses_garbage_without_overwriting(self):
        # Generically find the adapter whose plan_skills edits a file (skills manifest)
        manifest_adapter = None
        manifest_path = None

        skill_dir = self.home / "demo-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        test_skill = SkillRef("demo-skill", skill_dir)
        for cls in ADAPTERS:
            adapter = cls()
            ops = adapter.plan_skills(self.env, Scope.GLOBAL, [test_skill], frozenset())
            for op in ops:
                if isinstance(op, EditFile):
                    manifest_adapter = adapter
                    manifest_path = op.path
                    break
            if manifest_adapter:
                break

        self.assertIsNotNone(manifest_adapter, "Expected an adapter managing skills via a manifest")
        assert manifest_path is not None

        # Write garbage to the manifest path
        self.write(manifest_path, GARBAGE_PAYLOAD)
        before_listing = self.listing(self.home)

        reading = read_tool(manifest_adapter, self.env)
        self.assertEqual(len(reading.problems), 1)
        prob = reading.problems[0]
        self.assertEqual(prob.kind, Kind.SKILLS)
        self.assertNotIn(PLANTED_SECRET, prob.message)
        self.assertNotIn(PLANTED_SECRET, repr(reading))
        self.assertEqual(self.listing(self.home), before_listing)

        # Plan skills with garbage manifest
        plan_ops = manifest_adapter.plan_skills(self.env, Scope.GLOBAL, [test_skill], frozenset())
        for op in plan_ops:
            if isinstance(op, EditFile):
                self.assertNotEqual(op.path, manifest_path)

        runner = FakeRunner()
        apply_plan(self.env, Plan(plan_ops), label="test-unknown-skills", run_cli=runner)
        self.assertEqual(manifest_path.read_bytes(), GARBAGE_PAYLOAD)

    def test_read_all_filters_only_installed_tools(self):
        adapters = [cls() for cls in ADAPTERS]
        first = adapters[0]
        second = adapters[1]

        detections = [
            Detection(tool=first.id, installed=True),
            Detection(tool=second.id, installed=False),
        ]
        readings = read_all(self.env, adapters, detections)
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0].tool, first.id)


if __name__ == "__main__":
    unittest.main()
