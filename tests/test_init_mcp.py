"""Tests for MCP servers import in `all-agents init` (Phase 4 M04-T2.2)."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.engine_support import FakeRunner, make_env

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.adapters.base import ToolAdapter
from one_setup_all_agents.cli import build_parser
from one_setup_all_agents.commands import init
from one_setup_all_agents.config_source import desired_all, load_source
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine.plan import build_plan
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.importer import (
    McpProposal,
    propose_mcp,
)
from one_setup_all_agents.model import (
    Detection,
    Kind,
    McpServer,
    Paths,
    Scope,
)
from one_setup_all_agents.reader import ToolReading

PLANTED_TOKEN = "sk-placeholder-test-secret-token-xyz"


class InitMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = make_env(self.root)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, is_interactive: bool = False, read_line=lambda prompt: "") -> CommandContext:
        return CommandContext(
            env=self.env,
            read_line=read_line,
            is_interactive=is_interactive,
            out=self.out,
            err=self.err,
        )

    def _dummy_adapter(self, tool_id: str, tool_label: str) -> ToolAdapter:
        return type(
            "DummyAdapter",
            (ToolAdapter,),
            {
                "id": tool_id,
                "label": tool_label,
                "capabilities": frozenset({Kind.MCP}),
                "detect": lambda self, env: Detection(
                    installed=True, tool=tool_id, label=tool_label
                ),
                "paths": lambda self, env, scope: Paths(),
                "read_instructions": lambda self, env, scope: None,
                "read_mcp": lambda self, env, scope: {},
                "read_skills": lambda self, env, scope: [],
                "plan_instructions": lambda self, env, scope, desired, owned: [],
                "plan_mcp": lambda self, env, scope, desired, owned: [],
                "plan_skills": lambda self, env, scope, desired, owned: [],
                "import_notes": lambda self, env, scope=Scope.GLOBAL: [],
            },
        )()

    def test_single_form_imported_and_adopted(self):
        a1 = self._dummy_adapter("tool-a", "Tool A")
        a2 = self._dummy_adapter("tool-b", "Tool B")
        s = McpServer("demo", command="cmd", args=("--test",))
        r1 = ToolReading("tool-a", "Tool A", None, {"demo": s}, (), ())
        r2 = ToolReading("tool-b", "Tool B", None, {"demo": s}, (), ())

        prop = propose_mcp([r1, r2], [a1, a2], self.env)
        self.assertIsInstance(prop, McpProposal)
        self.assertIn("demo", prop.servers)
        self.assertEqual(prop.servers["demo"].server, s)
        self.assertIsNone(prop.servers["demo"].tools)
        adopted_tools = [
            ref.tool for ref in prop.adopt if ref.kind is Kind.MCP and ref.key == "demo"
        ]
        self.assertEqual(sorted(adopted_tools), ["tool-a", "tool-b"])
        self.assertEqual(prop.messages, [])

    def test_majority_resolution(self):
        a1 = self._dummy_adapter("tool-a", "Tool A")
        a2 = self._dummy_adapter("tool-b", "Tool B")
        a3 = self._dummy_adapter("tool-c", "Tool C")
        s_major = McpServer("demo", command="cmd1")
        s_minor = McpServer("demo", command="cmd2")
        r1 = ToolReading("tool-a", "Tool A", None, {"demo": s_major}, (), ())
        r2 = ToolReading("tool-b", "Tool B", None, {"demo": s_major}, (), ())
        r3 = ToolReading("tool-c", "Tool C", None, {"demo": s_minor}, (), ())

        prop = propose_mcp([r1, r2, r3], [a1, a2, a3], self.env)
        self.assertEqual(prop.servers["demo"].server.command, "cmd1")
        self.assertTrue(any("differs between tools: demo" in m for m in prop.messages))
        adopted_tools = [
            ref.tool for ref in prop.adopt if ref.kind is Kind.MCP and ref.key == "demo"
        ]
        self.assertEqual(sorted(adopted_tools), ["tool-a", "tool-b"])

    def test_tie_with_and_without_from_tool(self):
        adapters = all_adapters()
        adapter_first = adapters[0]
        adapter_second = adapters[1]
        s1 = McpServer("demo", command="first_cmd")
        s2 = McpServer("demo", command="second_cmd")
        r1 = ToolReading(adapter_first.id, adapter_first.label, None, {"demo": s1}, (), ())
        r2 = ToolReading(adapter_second.id, adapter_second.label, None, {"demo": s2}, (), ())

        # Tie without from_tool -> registry order selects first
        prop_no_from = propose_mcp([r1, r2], [adapter_first, adapter_second], self.env)
        self.assertEqual(prop_no_from.servers["demo"].server.command, "first_cmd")

        # Tie with from_tool -> selects second
        prop_with_from = propose_mcp(
            [r1, r2],
            [adapter_first, adapter_second],
            self.env,
            from_tool=adapter_second.id,
        )
        self.assertEqual(prop_with_from.servers["demo"].server.command, "second_cmd")

    def test_tools_selector_when_present_in_subset_of_tools(self):
        a1 = self._dummy_adapter("tool-a", "Tool A")
        a2 = self._dummy_adapter("tool-b", "Tool B")
        a3 = self._dummy_adapter("tool-c", "Tool C")
        s = McpServer("demo", command="cmd")
        r1 = ToolReading("tool-a", "Tool A", None, {"demo": s}, (), ())
        r2 = ToolReading("tool-b", "Tool B", None, {"demo": s}, (), ())
        r3 = ToolReading("tool-c", "Tool C", None, {}, (), ())

        prop = propose_mcp([r1, r2, r3], [a1, a2, a3], self.env)
        self.assertEqual(prop.servers["demo"].tools, ("tool-a", "tool-b"))

    def test_timeout_filled_from_another_tool(self):
        a1 = self._dummy_adapter("tool-a", "Tool A")
        a2 = self._dummy_adapter("tool-b", "Tool B")
        s1 = McpServer("demo", command="cmd", startup_timeout_sec=None)
        s2 = McpServer("demo", command="cmd", startup_timeout_sec=45)
        r1 = ToolReading("tool-a", "Tool A", None, {"demo": s1}, (), ())
        r2 = ToolReading("tool-b", "Tool B", None, {"demo": s2}, (), ())

        prop = propose_mcp([r1, r2], [a1, a2], self.env)
        self.assertEqual(prop.servers["demo"].server.startup_timeout_sec, 45)

    def test_disabled_import(self):
        a1 = self._dummy_adapter("tool-a", "Tool A")
        s = McpServer("demo", command="cmd", enabled=False)
        r1 = ToolReading("tool-a", "Tool A", None, {"demo": s}, (), ())

        prop = propose_mcp([r1], [a1], self.env)
        self.assertFalse(prop.servers["demo"].server.enabled)

    def test_secret_literal_env_value_note_present_and_value_absent(self):
        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        self.assertIsNotNone(paths.mcp)
        paths.mcp.parent.mkdir(parents=True, exist_ok=True)
        # Format for open_code adapter (first in registry)
        paths.mcp.write_text(
            f'{{"mcp": {{"test-srv": {{"type": "local", "command": ["echo"], "environment": {{"MY_SECRET": "{PLANTED_TOKEN}"}}}}}}}}',
            encoding="utf-8",
        )

        detections = [
            Detection(
                installed=True, tool=adapter.id, label=adapter.label, binary=Path("/bin/adapter")
            )
        ]
        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)

        # 1. Output checks
        out = ctx.out.getvalue()
        err = ctx.err.getvalue()
        self.assertNotIn(PLANTED_TOKEN, out)
        self.assertNotIn(PLANTED_TOKEN, err)
        self.assertIn("MY_SECRET", err)

        # 2. Source file check
        mcp_source = self.env.config_dir / "mcp.jsonc"
        if mcp_source.exists():
            content = mcp_source.read_text(encoding="utf-8")
            self.assertNotIn(PLANTED_TOKEN, content)
            self.assertIn("MY_SECRET", content)

        # 3. Journal checks
        ops = journal.list_operations(self.env.state_dir)
        self.assertEqual(len(ops), 1)
        journal_text = (journal.journal_path(self.env.state_dir, ops[0]["id"])).read_text(
            encoding="utf-8"
        )
        self.assertNotIn(PLANTED_TOKEN, journal_text)

    def test_unreadable_mcp_file_in_one_tool_does_not_block_others(self):
        a_good = all_adapters()[0]
        a_bad = all_adapters()[1]
        p_good = a_good.paths(self.env, Scope.GLOBAL)
        p_bad = a_bad.paths(self.env, Scope.GLOBAL)
        p_good.mcp.parent.mkdir(parents=True, exist_ok=True)
        p_bad.mcp.parent.mkdir(parents=True, exist_ok=True)

        p_good.mcp.write_text(
            '{"mcp": {"good-srv": {"type": "local", "command": ["echo"]}}}', encoding="utf-8"
        )
        p_bad.mcp.write_text("NOT VALID JSON {{{", encoding="utf-8")

        detections = [
            Detection(installed=True, tool=a_good.id, label=a_good.label, binary=Path("/bin/good")),
            Detection(installed=True, tool=a_bad.id, label=a_bad.label, binary=Path("/bin/bad")),
        ]

        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)
        self.assertIn("problem reading", ctx.err.getvalue())
        mcp_file = self.env.config_dir / "mcp.jsonc"
        self.assertTrue(mcp_file.exists())
        self.assertIn("good-srv", mcp_file.read_text(encoding="utf-8"))

    def test_adoption_only_for_equal_entries(self):
        a1 = self._dummy_adapter("tool-a", "Tool A")
        a2 = self._dummy_adapter("tool-b", "Tool B")
        s1 = McpServer("srv", command="cmd1")
        s2 = McpServer("srv", command="cmd2")
        r1 = ToolReading("tool-a", "Tool A", None, {"srv": s1}, (), ())
        r2 = ToolReading("tool-b", "Tool B", None, {"srv": s2}, (), ())

        prop = propose_mcp([r1, r2], [a1, a2], self.env, from_tool="tool-a")
        self.assertEqual(prop.servers["srv"].server.command, "cmd1")
        adopted = [ref.tool for ref in prop.adopt if ref.kind is Kind.MCP and ref.key == "srv"]
        self.assertEqual(adopted, ["tool-a"])

    def test_idempotence_build_plan_empty_for_adopted_pairs(self):
        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        paths.mcp.parent.mkdir(parents=True, exist_ok=True)
        paths.mcp.write_text(
            '{"mcp": {"demo": {"type": "local", "command": ["run"]}}}', encoding="utf-8"
        )

        detections = [
            Detection(
                installed=True, tool=adapter.id, label=adapter.label, binary=Path("/bin/adapter")
            )
        ]
        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)

        source = load_source(self.env.config_dir, [adapter.id])
        desired = desired_all(source, [adapter.id])
        plan = build_plan(self.env, [adapter], desired)
        mcp_ops = [
            op
            for op in plan.executable
            if getattr(op, "claims", None) and any(c.kind is Kind.MCP for c in op.claims)
        ]
        self.assertEqual(
            mcp_ops, [], "no MCP op should be planned for already equal adopted server"
        )

    def test_undo_init_operation_removes_source_and_ownership(self):
        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        paths.mcp.parent.mkdir(parents=True, exist_ok=True)
        paths.mcp.write_text(
            '{"mcp": {"demo": {"type": "local", "command": ["run"]}}}', encoding="utf-8"
        )

        detections = [
            Detection(
                installed=True, tool=adapter.id, label=adapter.label, binary=Path("/bin/adapter")
            )
        ]
        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)

        ops = journal.list_operations(self.env.state_dir)
        self.assertEqual(len(ops), 1)
        op_id = ops[0]["id"]

        # Ledger has demo owned
        owned = journal.owned_items(self.env.state_dir)
        self.assertTrue(any(item[1] == "mcp" and item[2] == "demo" for item in owned))

        # Revert init
        report = revert_operation(self.env, op_id, run_cli=FakeRunner())
        self.assertEqual(report.op_id, op_id)

        # Source files created by init are removed/restored
        self.assertFalse((self.env.config_dir / "mcp.jsonc").exists())
        self.assertFalse((self.env.config_dir / "config.toml").exists())

        # Ownership is cleared
        owned_after = journal.owned_items(self.env.state_dir)
        self.assertFalse(any(item[1] == "mcp" and item[2] == "demo" for item in owned_after))


if __name__ == "__main__":
    unittest.main()
