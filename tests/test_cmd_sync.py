"""Tests for the `all-agents sync` command (Phase 5 M05-T1)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.engine_support import EngineCase, FakeRunner

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import main
from one_setup_all_agents.config_source import INSTRUCTIONS_NAME, MCP_NAME, SOURCE_CONFIG_NAME
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine.lock import engine_lock
from one_setup_all_agents.engine.plan import build_plan
from one_setup_all_agents.model import EditFile, Plan, Scope

PLANTED_SECRET = "sk-" + "a1b2c3d4" * 6


class CmdSyncTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()
        # Set up fixture homes for all registered adapters
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                self.write(paths.instructions, b"# Shared instructions\n")
            if paths.mcp:
                self.write(paths.mcp, b"{}" if "json" in paths.mcp.name else b"")
            for snap in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap.exists():
                    self.write(snap, b"{}" if "json" in snap.name else b"")

        # Initialise shared source using init --yes
        ctx, _, _ = self._ctx()
        code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)
        self.runner.calls.clear()

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
            run_cli=runner or self.runner,
            is_interactive=is_interactive,
            out=out,
            err=err,
        )
        return ctx, out, err

    def test_not_initialised_exits_one(self):
        config_file = self.env.config_dir / SOURCE_CONFIG_NAME
        config_file.unlink()

        ctx, out, err = self._ctx()
        code = main(["sync"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("not initialised; run `all-agents init`", err.getvalue())

    def test_a_dry_run_prints_plan_and_writes_nothing(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")
        before_home = self.listing(self.home)
        before_state = self.listing(self.state)

        ctx, out, err = self._ctx()
        code = main(["sync", "--dry-run"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("Plan:", out.getvalue())
        self.assertIn("Dry run: nothing was written.", out.getvalue())
        self.assertEqual(self.listing(self.home), before_home)
        self.assertEqual(self.listing(self.state), before_state)

    def test_b_yes_applies_and_second_sync_says_nothing_to_do(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")

        ctx, out, err = self._ctx()
        code = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("applied operation", out.getvalue())

        ctx2, out2, err2 = self._ctx()
        code2 = main(["sync", "--yes"], context=ctx2)
        self.assertEqual(code2, 0)
        self.assertIn("Nothing to do: all tools are in sync.", out2.getvalue())

    def test_c_tool_restricts_and_unknown_id_exits_one(self):
        ctx_err, out_err, err_err = self._ctx()
        code_err = main(["sync", "--tool", "unknown-tool"], context=ctx_err)
        self.assertEqual(code_err, 1)
        self.assertIn("unknown tool 'unknown-tool'", err_err.getvalue())
        self.assertIn("valid tools:", err_err.getvalue())

        # Restrict to the first adapter
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")

        chosen = all_adapters()[0]
        ctx, out, err = self._ctx()
        code = main(["sync", "--dry-run", "--tool", chosen.id], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("To run:", out.getvalue())

        other_adapters = [a for a in all_adapters() if a.id != chosen.id]
        for other in other_adapters:
            other_paths = other.paths(self.env, Scope.GLOBAL)
            if other_paths.instructions:
                self.assertNotIn(str(other_paths.instructions), out.getvalue())

    def test_d_only_mcp_plans_no_instruction_ops(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")

        ctx, out, err = self._ctx()
        code = main(["sync", "--dry-run", "--only", "mcp"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("Nothing to do: all tools are in sync.", out.getvalue())

    def test_e_non_interactive_without_yes_writes_nothing(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")
        before_home = self.listing(self.home)

        ctx, out, err = self._ctx(is_interactive=False)
        code = main(["sync"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("Dry run only: not interactive; pass --yes to apply", out.getvalue())
        self.assertEqual(self.listing(self.home), before_home)

    def test_e_interactive_declined_writes_nothing(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")
        before_home = self.listing(self.home)

        ctx, out, err = self._ctx(is_interactive=True, read_line=lambda prompt: "n")
        code = main(["sync"], context=ctx)
        self.assertEqual(code, 0)
        self.assertEqual(self.listing(self.home), before_home)

    def test_f_conflict_unowned_entry_exits_three_and_entry_untouched(self):
        # Add server "demo-server" in shared mcp.jsonc
        mcp_content = json.dumps(
            {"version": 1, "servers": {"demo-server": {"command": "source-command"}}}
        )
        (self.env.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")

        # Plant an unowned conflicting entry in an adapter's MCP config file
        target_adapter = None
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.mcp and "json" in paths.mcp.name:
                target_adapter = a
                break
        self.assertIsNotNone(target_adapter)
        mcp_path = target_adapter.paths(self.env, Scope.GLOBAL).mcp
        conflicting_text = json.dumps(
            {"mcp": {"demo-server": {"command": "conflicting-unowned-command"}}}
        )
        self.write(mcp_path, conflicting_text.encode("utf-8"))
        original_bytes = mcp_path.read_bytes()

        ctx, out, err = self._ctx()
        code = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code, 3)
        self.assertIn("conflict", out.getvalue().lower())
        self.assertEqual(mcp_path.read_bytes(), original_bytes)

    def test_g_lock_held_exits_one(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")

        ctx, out, err = self._ctx()
        with engine_lock(self.env.state_dir):
            code = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("lock", err.getvalue().lower())

    def test_h_open_operation_planted_exits_one_with_recovery_hint(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")

        journal.begin(self.env.state_dir, "unfinished-sync")

        ctx, out, err = self._ctx()
        code = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn(
            "an earlier run did not finish; run `all-agents undo` to recover",
            err.getvalue(),
        )

    def test_i_failing_runner_for_runcli_exits_one_and_reverts_home(self):
        # Configure fake claude binary so claude adapter plans RunCli for an added MCP server
        fake_binary = self.root / "fake_claude.exe"
        fake_binary.touch()
        self.env.environ["ALL_AGENTS_CLAUDE_BIN"] = str(fake_binary)

        # Add server "runner-demo" to source
        mcp_content = json.dumps(
            {"version": 1, "servers": {"runner-demo": {"command": "demo-cmd"}}}
        )
        (self.env.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")

        before_home = self.listing(self.home)
        failing_runner = FakeRunner(raise_on=["fake_claude.exe", "runner-demo"])
        ctx, out, err = self._ctx(runner=failing_runner)

        code = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("put back", err.getvalue().lower())
        self.assertEqual(self.listing(self.home), before_home)

    def test_j_source_error_bad_version_exits_one_and_writes_nothing(self):
        config_file = self.env.config_dir / SOURCE_CONFIG_NAME
        config_file.write_text("version = 999\n", encoding="utf-8")
        before_home = self.listing(self.home)

        ctx, out, err = self._ctx()
        code = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("unsupported source version 999", err.getvalue())
        self.assertEqual(self.listing(self.home), before_home)

    def test_k_secrets_never_appear_in_output_or_journal(self):
        # Plant secret in source instructions
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text(f"# Secret instructions: {PLANTED_SECRET}\n", encoding="utf-8")

        ctx, out, err = self._ctx()
        code = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code, 0)

        # Output streams must not leak the secret
        self.assertNotIn(PLANTED_SECRET, out.getvalue())
        self.assertNotIn(PLANTED_SECRET, err.getvalue())

        # Journal entries and ops manifests must not contain the secret
        for path in self.state.rglob("*"):
            if path.is_file():
                content = path.read_text("utf-8", errors="ignore")
                self.assertNotIn(PLANTED_SECRET, content)

    def test_l_idempotence_after_apply(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")

        ctx1, out1, _ = self._ctx()
        code1 = main(["sync", "--yes"], context=ctx1)
        self.assertEqual(code1, 0)
        home_after_first = self.listing(self.home)

        ctx2, out2, _ = self._ctx()
        code2 = main(["sync", "--yes"], context=ctx2)
        self.assertEqual(code2, 0)
        self.assertIn("Nothing to do: all tools are in sync.", out2.getvalue())
        self.assertEqual(self.listing(self.home), home_after_first)

    def test_m_verification_replan_warning_appears_when_adapter_misreports(self):
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text("# Updated instructions\n", encoding="utf-8")

        real_build_plan = build_plan
        calls = 0

        def mock_build_plan(*args, **kwargs):
            nonlocal calls
            calls += 1
            plan = real_build_plan(*args, **kwargs)
            if calls == 2:
                # During re-plan verification, simulate an unapplied op remaining
                return Plan(
                    ops=[
                        EditFile(
                            path=self.home / "pending.txt",
                            transform=lambda _: b"pending",
                            expect_sha256=None,
                            description="pending",
                        )
                    ]
                )
            return plan

        with patch("one_setup_all_agents.commands.sync.build_plan", side_effect=mock_build_plan):
            ctx, out, err = self._ctx()
            code = main(["sync", "--yes"], context=ctx)
            self.assertEqual(code, 0)
            self.assertIn("warning: 1 change(s) still pending after apply", err.getvalue())

    def test_n_subprocess_smoke_dry_run_exits_zero(self):
        sub_env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
            "XDG_CONFIG_HOME": str(self.root / "xdg_config"),
            "XDG_STATE_HOME": str(self.root / "xdg_state"),
            "ALL_AGENTS_CONFIG_DIR": str(self.env.config_dir),
            "ALL_AGENTS_STATE_DIR": str(self.state),
            "PATH": str(Path(sys.executable).parent),
            "PYTHONPATH": str(_bootstrap.SRC),
        }
        for var in ("SYSTEMROOT", "SystemDrive", "TMP", "TEMP"):
            if var in os.environ:
                sub_env[var] = os.environ[var]

        res = subprocess.run(
            [sys.executable, "-m", "one_setup_all_agents", "sync", "--dry-run"],
            env=sub_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")
        self.assertIn("Nothing to do: all tools are in sync.", res.stdout)


if __name__ == "__main__":
    unittest.main()
