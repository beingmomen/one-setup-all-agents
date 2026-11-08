"""Tests for the `all-agents init` command (Phase 4 M04-T2)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.engine_support import FakeRunner, make_env

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import main
from one_setup_all_agents.config_source import SOURCE_CONFIG_NAME
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import Detection, Kind, Scope
from one_setup_all_agents.reader import Problem, ToolReading

PLANTED_SECRET = "sk-" + "a1b2c3d4" * 6


class CmdInitTests(unittest.TestCase):
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

    def _installed_detection(self) -> Detection:
        return Detection(
            installed=True,
            tool="example-tool",
            label="Example Tool",
            binary=Path("/bin/example-tool"),
        )

    def test_no_tool_installed_exits_zero_and_writes_nothing(self):
        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=[]):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("No supported tool was found. Nothing to import.", self.out.getvalue())
        self.assertFalse(self.env.config_dir.exists())
        self.assertFalse(self.env.state_dir.exists())

    def test_already_initialised_exits_one_and_writes_nothing(self):
        ctx = self._ctx()
        self.env.config_dir.mkdir(parents=True)
        config_file = self.env.config_dir / SOURCE_CONFIG_NAME
        config_file.write_text("dummy-content", encoding="utf-8")

        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[self._installed_detection()],
        ):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("already initialised; use `all-agents sync`", self.err.getvalue())
        self.assertEqual(config_file.read_text(encoding="utf-8"), "dummy-content")

    def test_unknown_from_tool_exits_one(self):
        ctx = self._ctx()
        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[self._installed_detection()],
        ):
            code = main(["init", "--from", "unknown-tool-xyz"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("unknown tool 'unknown-tool-xyz'", self.err.getvalue())

    def test_non_interactive_without_yes_exits_one_and_writes_nothing(self):
        ctx = self._ctx(is_interactive=False)
        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[self._installed_detection()],
        ):
            code = main(["init"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("not asked: not interactive; pass --yes", self.out.getvalue())
        self.assertFalse((self.env.config_dir / SOURCE_CONFIG_NAME).exists())
        self.assertFalse(self.env.state_dir.exists())

    def test_dry_run_exits_zero_and_writes_nothing(self):
        ctx = self._ctx()
        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[self._installed_detection()],
        ):
            code = main(["init", "--dry-run"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("Import proposal:", self.out.getvalue())
        self.assertFalse(self.env.config_dir.exists())
        self.assertFalse(self.env.state_dir.exists())

    def test_secret_planted_in_problem_does_not_leak_into_output(self):
        ctx = self._ctx()
        detection = self._installed_detection()
        problem = Problem(
            tool="example-tool",
            kind=Kind.MCP,
            path=None,
            message=f"failed authentication with token {PLANTED_SECRET}",
        )
        reading = ToolReading(
            tool="example-tool",
            label="Example Tool",
            instructions=None,
            mcp={},
            skills=(),
            problems=(problem,),
        )
        with (
            patch(
                "one_setup_all_agents.commands.init.detect_all",
                return_value=[detection],
            ),
            patch(
                "one_setup_all_agents.commands.init.read_all",
                return_value=[reading],
            ),
        ):
            code = main(["init", "--dry-run"], context=ctx)
        self.assertEqual(code, 0)
        self.assertNotIn(PLANTED_SECRET, self.out.getvalue())
        self.assertNotIn(PLANTED_SECRET, self.err.getvalue())

    def test_successful_init_creates_config_and_journal_operation(self):
        ctx = self._ctx()
        detection = self._installed_detection()
        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[detection],
        ):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)
        config_file = self.env.config_dir / SOURCE_CONFIG_NAME
        self.assertTrue(config_file.exists())
        self.assertIn("version = 1", config_file.read_text(encoding="utf-8"))
        self.assertIn("[tools.example-tool]", config_file.read_text(encoding="utf-8"))
        operations = journal.list_operations(self.env.state_dir)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["label"], "init")
        self.assertEqual(operations[0]["status"], "done")

    def test_init_four_tools_snapshot_entries_and_revert_restores_home_byte_for_byte(self):
        adapters = all_adapters()
        detections = []
        for a in adapters:
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                paths.instructions.parent.mkdir(parents=True, exist_ok=True)
                paths.instructions.write_bytes(b"# Shared instructions\n")
            if paths.mcp:
                paths.mcp.parent.mkdir(parents=True, exist_ok=True)
                paths.mcp.write_bytes(b"{}" if "json" in paths.mcp.name else b"")
            for snap_file in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap_file.exists():
                    snap_file.parent.mkdir(parents=True, exist_ok=True)
                    snap_file.write_bytes(b"{}" if "json" in snap_file.name else b"")
            detections.append(
                Detection(installed=True, tool=a.id, label=a.label, binary=Path(f"/bin/{a.id}"))
            )

        before_listing = {
            p.relative_to(self.home).as_posix(): p.read_bytes()
            for p in self.home.rglob("*")
            if p.is_file()
        }

        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)

        # 1. ops/ has exactly 1 operation done with label init
        operations = journal.list_operations(self.env.state_dir)
        self.assertEqual(len(operations), 1)
        op_id = operations[0]["id"]
        self.assertEqual(operations[0]["label"], "init")
        self.assertEqual(operations[0]["status"], "done")

        # 2. Entries include snapshot no-op entries
        entries = journal.load(self.env.state_dir, op_id).entries
        snap_entries = [e for e in entries if e.get("description") == "snapshot (no change)"]
        self.assertTrue(len(snap_entries) > 0)

        # 3. revert_operation restores temp home byte-for-byte, removes source folder, empties owned
        report = revert_operation(self.env, op_id, run_cli=FakeRunner())
        self.assertEqual(report.op_id, op_id)
        self.assertFalse((self.env.config_dir / SOURCE_CONFIG_NAME).exists())
        self.assertEqual(journal.owned_items(self.env.state_dir), set())

        after_listing = {
            p.relative_to(self.home).as_posix(): p.read_bytes()
            for p in self.home.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before_listing, after_listing)

    def test_subprocess_dry_run_in_isolated_temp_env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_bootstrap.SRC), *filter(None, [env.get("PYTHONPATH")])]
        )
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        result = subprocess.run(
            [sys.executable, "-m", "one_setup_all_agents", "init", "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            "No supported tool was found" in result.stdout or "Import proposal:" in result.stdout
        )


if __name__ == "__main__":
    unittest.main()
