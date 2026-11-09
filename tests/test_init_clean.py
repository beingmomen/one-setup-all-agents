"""Tests for `all-agents init` on a clean machine (Phase 4 M04-T4)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import EngineCase, FakeRunner
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import main
from one_setup_all_agents.config_source import SOURCE_CONFIG_NAME, desired_all, load_source
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.plan import build_plan
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import Scope


class InitCleanMachineTests(EngineCase):
    def _ctx(
        self,
        runner: FakeRunner | None = None,
        is_interactive: bool = False,
        read_line=lambda prompt: "",
    ) -> tuple[CommandContext, io.StringIO, io.StringIO]:
        out = io.StringIO()
        err = io.StringIO()
        ctx = CommandContext(
            env=self.env,
            read_line=read_line,
            run_cli=runner or FakeRunner(),
            is_interactive=is_interactive,
            out=out,
            err=err,
        )
        return ctx, out, err

    def test_empty_temp_home_writes_nothing_and_exits_zero(self):
        # 1. Empty temp home: init --yes -> exit 0, "No supported tool was found", byte-identical
        before_listing = self.listing(self.root)
        ctx, out, err = self._ctx()

        code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("No supported tool was found. Nothing to import.", out.getvalue())

        # State dir is not created
        self.assertFalse(self.state.exists())
        self.assertFalse((self.env.config_dir / SOURCE_CONFIG_NAME).exists())
        self.assertEqual(self.listing(self.root), before_listing)

    def test_home_with_config_only_detection_imports_and_reports_config_only(self):
        # 2. Home with only a config folder of one tool (config-only detection)
        # Setup config files for opencode only (binary absent)
        adapter = [a for a in all_adapters() if a.id == "opencode"][0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        self.write(paths.instructions, b"# OpenCode instructions\n")
        self.write(paths.mcp, b'{"mcp": {}}')

        ctx, out, err = self._ctx()
        code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)

        stdout = out.getvalue()
        # Status printed as config only
        self.assertIn("config only", stdout)
        # Binary line is absent
        self.assertNotIn("binary:", stdout)

        # Source files created
        self.assertTrue((self.env.config_dir / SOURCE_CONFIG_NAME).exists())
        self.assertTrue((self.env.config_dir / "instructions.md").exists())
        self.assertEqual(
            (self.env.config_dir / "instructions.md").read_bytes(),
            b"# OpenCode instructions\n",
        )

    def test_fixture_homes_all_four_tools_full_flow_with_zero_runner_calls(self):
        # 3. Fixture homes for all four tools -> full flow with a FakeRunner (assert zero calls)
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                self.write(paths.instructions, b"# Shared instructions\n")
            if paths.mcp:
                if a.id == "claude":
                    self.write(paths.mcp, b'{"mcpServers": {"demo": {"command": "demo"}}}')
                elif "json" in paths.mcp.name:
                    self.write(paths.mcp, b"{}")
                else:
                    self.write(paths.mcp, b"")
            for snap in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap.exists():
                    self.write(snap, b"{}" if "json" in snap.name else b"")

        runner = FakeRunner()
        ctx, out, err = self._ctx(runner=runner)

        code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls), 0, "init must never invoke tool CLIs")

        operations = journal.list_operations(self.state)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["label"], "init")
        self.assertEqual(operations[0]["status"], "done")

        op_obj = journal.load(self.state, operations[0]["id"])
        snapshot_entries = [e for e in op_obj.entries if "snapshot" in e.get("description", "")]
        self.assertGreater(len(snapshot_entries), 0)

        self.assertTrue((self.env.config_dir / SOURCE_CONFIG_NAME).is_file())
        self.assertTrue((self.env.config_dir / "mcp.jsonc").is_file())
        self.assertTrue((self.env.config_dir / "instructions.md").is_file())

    def test_second_init_is_refused(self):
        # 4. A second init is refused (exit 1)
        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        self.write(paths.instructions, b"# instructions\n")
        self.write(paths.mcp, b"{}" if "json" in paths.mcp.name else b"")

        ctx1, _, _ = self._ctx()
        code1 = main(["init", "--yes"], context=ctx1)
        self.assertEqual(code1, 0)

        ctx2, out2, err2 = self._ctx()
        code2 = main(["init", "--yes"], context=ctx2)
        self.assertEqual(code2, 1)
        self.assertIn("already initialised; use `all-agents sync`", err2.getvalue())

    def test_sync_follow_up_and_revert_both_operations_restores_home_byte_for_byte(self):
        # 5. After init, build_plan + apply_plan of follow-up sync-like plan works
        # and revert_operation of BOTH operations returns home to exact original bytes.
        adapters = all_adapters()
        for a in adapters:
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                self.write(paths.instructions, b"# Shared instructions\n")
            if paths.mcp:
                self.write(paths.mcp, b"{}" if "json" in paths.mcp.name else b"")
            for snap in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap.exists():
                    self.write(snap, b"{}" if "json" in snap.name else b"")

        before_listing = self.listing(self.home)

        # 1. Run init
        runner = FakeRunner()
        ctx, out, err = self._ctx(runner=runner)
        code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)

        operations = journal.list_operations(self.state)
        self.assertEqual(len(operations), 1)
        init_op_id = operations[0]["id"]

        # 2. Build and apply follow-up sync plan
        adapter_ids = [a.id for a in adapters]
        source = load_source(self.env.config_dir, adapter_ids)
        desired = desired_all(source, adapter_ids)
        sync_plan = build_plan(self.env, adapters, desired)
        sync_op_id = apply_plan(self.env, sync_plan, label="sync", run_cli=runner)

        # 3. Revert sync operation (if non-empty)
        if sync_op_id is not None:
            revert_operation(self.env, sync_op_id, run_cli=runner)

        # 4. Revert init operation
        revert_operation(self.env, init_op_id, run_cli=runner)

        # 5. Assert home restored byte-for-byte, source config removed, owned empty
        self.assertEqual(self.listing(self.home), before_listing)
        self.assertFalse((self.env.config_dir / SOURCE_CONFIG_NAME).exists())
        self.assertEqual(journal.owned_items(self.state), set())

    def test_subprocess_smoke_clean_environment(self):
        # Subprocess smoke test in clean temp-folder environment
        sub_home = self.root / "sub_home"
        sub_config = self.root / "sub_config"
        sub_state = self.root / "sub_state"
        sub_env = {
            "HOME": str(sub_home),
            "USERPROFILE": str(sub_home),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
            "XDG_CONFIG_HOME": str(self.root / "xdg_config"),
            "XDG_STATE_HOME": str(self.root / "xdg_state"),
            "ALL_AGENTS_CONFIG_DIR": str(sub_config),
            "ALL_AGENTS_STATE_DIR": str(sub_state),
            "PATH": str(Path(sys.executable).parent),
            "PYTHONPATH": str(_bootstrap.SRC),
        }
        for var in ("SYSTEMROOT", "SystemDrive", "TMP", "TEMP"):
            if var in os.environ:
                sub_env[var] = os.environ[var]

        # Dry-run
        res_dry = subprocess.run(
            [sys.executable, "-m", "one_setup_all_agents", "init", "--dry-run", "--yes"],
            env=sub_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res_dry.returncode, 0, res_dry.stderr)
        self.assertIn("No supported tool was found. Nothing to import.", res_dry.stdout)
        self.assertFalse(sub_config.exists())
        self.assertFalse(sub_state.exists())

        # Apply
        res_apply = subprocess.run(
            [sys.executable, "-m", "one_setup_all_agents", "init", "--yes"],
            env=sub_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res_apply.returncode, 0, res_apply.stderr)
        self.assertIn("No supported tool was found. Nothing to import.", res_apply.stdout)
        self.assertFalse(sub_config.exists())
        self.assertFalse(sub_state.exists())


if __name__ == "__main__":
    unittest.main()
