"""Unit tests for FIX3-T3: Secrets and corrupt state."""

from __future__ import annotations

import io
from unittest.mock import patch

import _bootstrap  # noqa: F401
from engine_support import EngineCase
from example_adapter import ExampleAdapter
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import main
from one_setup_all_agents.config_source import (
    INSTRUCTIONS_NAME,
    MCP_NAME,
    SOURCE_CONFIG_NAME,
    SourceError,
    load_source,
    load_source_texts,
)
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.errors import PlanError
from one_setup_all_agents.engine.plan import DesiredState, build_plan
from one_setup_all_agents.model import (
    Conflict,
    ItemRef,
    Kind,
    Plan,
    RunCli,
    Scope,
)


class _SecretAdapter(ExampleAdapter):
    id = "secret-tool"

    def plan_instructions(self, env, scope, desired, owned):
        return []

    def plan_mcp(self, env, scope, desired, owned):
        return [
            RunCli(
                argv=[
                    "tool",
                    "add-server",
                    "planted-secret-item",
                    "--token",
                    "ghp_11111111111111111111",
                ],
                inverse_argv=["tool", "remove-server", "planted-secret-item"],
                description="add planted-secret-item with token",
                claims=(ItemRef(self.id, Kind.MCP, "planted-secret-item"),),
            )
        ]

    def plan_skills(self, env, scope, desired, owned):
        return []

    def plan_hooks(self, env, scope, desired, owned):
        return []

    def plan_permissions(self, env, scope, desired, owned):
        return []

    def plan_memory(self, env, scope, desired, owned):
        return []


class FixT3Tests(EngineCase):
    def setUp(self):
        super().setUp()
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                paths.instructions.parent.mkdir(parents=True, exist_ok=True)
                paths.instructions.write_bytes(b"# Initial instructions\n")
            if paths.mcp:
                paths.mcp.parent.mkdir(parents=True, exist_ok=True)
                paths.mcp.write_bytes(b"{}" if "json" in paths.mcp.name else b"")
            for snap in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap.exists():
                    snap.parent.mkdir(parents=True, exist_ok=True)
                    snap.write_bytes(b"{}" if "json" in snap.name else b"")

    def _ctx(self, is_interactive=False):
        out = io.StringIO()
        err = io.StringIO()
        ctx = CommandContext(
            env=self.env,
            out=out,
            err=err,
            is_interactive=is_interactive,
        )
        return ctx, out, err

    def test_runcli_planted_secret_build_plan_becomes_conflict(self):
        adapter = _SecretAdapter()
        plan = build_plan(self.env, [adapter], DesiredState(mcp={}))
        self.assertEqual(len(plan.ops), 1)
        conflict = plan.ops[0]
        self.assertIsInstance(conflict, Conflict)
        self.assertEqual(conflict.key, "planted-secret-item")
        self.assertEqual(conflict.reason, "command looks like a secret")

    def test_runcli_planted_secret_apply_plan_refuses_and_stores_no_secret(self):
        secret = "ghp_11111111111111111111"
        op = RunCli(
            argv=["tool", "add-server", "planted-secret-item", "--token", secret],
            inverse_argv=["tool", "remove-server", "planted-secret-item"],
            description="add planted-secret-item with token",
            claims=(ItemRef("secret-tool", Kind.MCP, "planted-secret-item"),),
        )
        plan = Plan(ops=[op])
        with self.assertRaises(PlanError) as ctx:
            apply_plan(self.env, plan, label="test-secret")

        err_msg = str(ctx.exception)
        self.assertIn(
            "refusing RunCli for planted-secret-item: command looks like a secret", err_msg
        )
        self.assertNotIn(secret, err_msg)
        self.assertIsInstance(plan.ops[0], Conflict)
        self.assertEqual(plan.ops[0].key, "planted-secret-item")

        # Verify no file in state_dir contains the secret
        if self.env.state_dir.exists():
            for p in self.env.state_dir.rglob("*"):
                if p.is_file():
                    self.assertNotIn(secret.encode("utf-8"), p.read_bytes())

    def test_runcli_planted_secret_sync_command_names_item_only(self):
        secret = "ghp_11111111111111111111"
        # Initialise shared config
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text(
            'version = 1\n[tools."secret-tool"]\nenabled = true\n',
            encoding="utf-8",
        )
        ctx, out, err = self._ctx()
        adapter = _SecretAdapter()
        with patch("one_setup_all_agents.commands.sync.all_adapters", return_value=[adapter]):
            with patch("one_setup_all_agents.commands.sync.detect_all") as mock_detect:
                from one_setup_all_agents.model import Detection

                mock_detect.return_value = [
                    Detection(tool="secret-tool", installed=True, version="1.0", warnings=())
                ]
                code = main(["sync", "--yes"], context=ctx)

        self.assertNotEqual(code, 0)
        output = out.getvalue() + err.getvalue()
        self.assertNotIn(secret, output)
        self.assertIn("planted-secret-item", output)
        if self.env.state_dir.exists():
            for p in self.env.state_dir.rglob("*"):
                if p.is_file():
                    self.assertNotIn(secret.encode("utf-8"), p.read_bytes())

    def test_unknown_top_level_key_masks_secret_looking_key(self):
        secret_key = "ghp_" + "1" * 20
        # 1. config.toml
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text(
            f"version = 1\n{secret_key} = true\n",
            encoding="utf-8",
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.env.config_dir, ["example"])
        self.assertNotIn(secret_key, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))
        self.assertIn(f"unknown top-level key '***' in '{SOURCE_CONFIG_NAME}'", str(ctx.exception))

        # 2. mcp.jsonc
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.env.config_dir / MCP_NAME).write_text(
            f'{{"version": 1, "{secret_key}": {{}}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.env.config_dir, ["example"])
        self.assertNotIn(secret_key, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))
        self.assertIn(f"unknown top-level key '***' in '{MCP_NAME}'", str(ctx.exception))

    def test_instructions_unknown_tool_id_skipped_without_reading(self):
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.env.config_dir / INSTRUCTIONS_NAME).write_text("# Core\n", encoding="utf-8")
        # Plant invalid non-UTF-8 bytes in instructions.unknown_tool.md
        bad_file = self.env.config_dir / "instructions.unknown_tool.md"
        bad_file.write_bytes(b"\xff\xfe\x00\x80\x81")

        # Must be skipped with warning, NOT raise SourceError("cannot read '...'")
        cfg = load_source(self.env.config_dir, ["example"])
        self.assertNotIn("unknown_tool", cfg.appendices)
        self.assertTrue(
            any("instructions.unknown_tool.md" in w and "unknown_tool" in w for w in cfg.warnings),
            f"Warnings: {cfg.warnings}",
        )

    def test_load_source_texts_requires_valid_tool_ids_argument(self):
        with self.assertRaises(TypeError):
            # Calling without valid_tool_ids must fail
            load_source_texts("version = 1\n")  # type: ignore[call-arg]

    def test_corrupt_ledger_sync_exits_1_clean_warning_no_traceback(self):
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text(
            "version = 1\n[tools.claude]\nenabled = true\n",
            encoding="utf-8",
        )
        self.env.state_dir.mkdir(parents=True, exist_ok=True)
        corrupt_content = "NOT VALID JSON!"
        (self.env.state_dir / "ledger.json").write_text(corrupt_content, encoding="utf-8")

        ctx, out, err = self._ctx()
        code = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code, 1)
        err_val = err.getvalue()
        self.assertIn("ledger.json", err_val)
        self.assertIn("restore from backup or reinitialise", err_val)
        self.assertNotIn("Traceback", err_val)
        self.assertNotIn("Traceback", out.getvalue())
        self.assertEqual(
            (self.env.state_dir / "ledger.json").read_text(encoding="utf-8"), corrupt_content
        )
        self.assertFalse((self.env.state_dir / "ops").exists())

    def test_corrupt_ledger_undo_exits_1_clean_warning_no_traceback(self):
        self.env.state_dir.mkdir(parents=True, exist_ok=True)
        corrupt_content = "NOT VALID JSON!"
        (self.env.state_dir / "ledger.json").write_text(corrupt_content, encoding="utf-8")

        ctx, out, err = self._ctx()
        code = main(["undo"], context=ctx)
        self.assertEqual(code, 1)
        err_val = err.getvalue()
        self.assertIn("ledger.json", err_val)
        self.assertIn("restore from backup or reinitialise", err_val)
        self.assertNotIn("Traceback", err_val)
        self.assertNotIn("Traceback", out.getvalue())
        self.assertEqual(
            (self.env.state_dir / "ledger.json").read_text(encoding="utf-8"), corrupt_content
        )
        self.assertFalse((self.env.state_dir / "ops").exists())

    def test_corrupt_ledger_undo_list_exits_1_clean_warning_no_traceback(self):
        self.env.state_dir.mkdir(parents=True, exist_ok=True)
        corrupt_content = "NOT VALID JSON!"
        (self.env.state_dir / "ledger.json").write_text(corrupt_content, encoding="utf-8")

        ctx, out, err = self._ctx()
        code = main(["undo", "--list"], context=ctx)
        self.assertEqual(code, 1)
        err_val = err.getvalue()
        self.assertIn("ledger.json", err_val)
        self.assertIn("restore from backup or reinitialise", err_val)
        self.assertNotIn("Traceback", err_val)
        self.assertNotIn("Traceback", out.getvalue())
        self.assertEqual(
            (self.env.state_dir / "ledger.json").read_text(encoding="utf-8"), corrupt_content
        )
        self.assertFalse((self.env.state_dir / "ops").exists())
