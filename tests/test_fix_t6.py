"""Comprehensive unit tests for FIX-T6: Undo, journal and status."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
from engine_support import EngineCase
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import build_parser, main
from one_setup_all_agents.config_source import (
    INSTRUCTIONS_NAME,
    MCP_NAME,
    SOURCE_CONFIG_NAME,
)
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import fsops, journal
from one_setup_all_agents.model import Scope


class FixT6Tests(EngineCase):
    def setUp(self):
        super().setUp()
        # Set up fixture homes for all registered adapters so they are detected as installed
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

    def _init_source(self):
        tools = {a.id: {"enabled": True, "skills_exclude": []} for a in all_adapters()}
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text(
            "version = 1\n" + "\n".join(f"[tools.{k}]\nenabled = true\n" for k in tools),
            encoding="utf-8",
        )
        (self.env.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {}}', encoding="utf-8"
        )
        (self.env.config_dir / INSTRUCTIONS_NAME).write_text("# Core\n", encoding="utf-8")

    # 1. Stack order: two applies in the same second, undo takes newest
    def test_stack_order_same_second_undo_takes_newest(self):
        ctx, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx)

        fixed_time = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

        # Force second op to have an ID that sorts alphabetically BEFORE first op
        with patch("secrets.token_hex", side_effect=["ffff", "0000"]):
            op1 = journal.begin(self.env.state_dir, "first-op", now=fixed_time)
            op1.set_status(journal.DONE)
            op2 = journal.begin(self.env.state_dir, "second-op", now=fixed_time)
            op2.set_status(journal.DONE)

        self.assertEqual(op1.id, "20261108T120000Z-ffff")
        self.assertEqual(op2.id, "20261108T120000Z-0000")

        # Alphabetically, op1 > op2, but op2 was created after op1!
        # undo must take op2 (the newest)
        undoable = journal.list_undoable(self.env.state_dir)
        self.assertEqual(undoable[0]["id"], op2.id)

    # 2. undo --force over a real folder where a file was: validates BEFORE journal.begin
    def test_undo_force_over_real_folder_validates_before_journal_begin(self):
        ctx, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx)

        # Apply a sync that modifies instructions
        (self.env.config_dir / INSTRUCTIONS_NAME).write_text(
            "# New instructions\n", encoding="utf-8"
        )
        ctx_s, _, _ = self._ctx()
        main(["sync", "--yes"], context=ctx_s)

        # Find one edited instructions file and replace it with a real unmanaged folder
        last_op_id = journal.list_undoable(self.env.state_dir)[0]["id"]
        last_op = journal.load(self.env.state_dir, last_op_id)
        target_path = None
        for e in last_op.entries:
            if e.get("type") == "edit_file":
                target_path = Path(e["path"])
                break
        self.assertIsNotNone(target_path)

        if target_path.exists():
            target_path.unlink()
        target_path.mkdir(parents=True)

        ops_before = set(p.name for p in (self.env.state_dir / "ops").iterdir())

        ctx_u, out_u, err_u = self._ctx()
        code = main(["undo", "--force"], context=ctx_u)
        self.assertEqual(code, 1)
        self.assertIn("nothing changed", err_u.getvalue())

        # Assert no undo operation was created in ops/
        ops_after = set(p.name for p in (self.env.state_dir / "ops").iterdir())
        self.assertEqual(ops_before, ops_after)

    # 3. Retention: 25 operations, repeated undo can undo 20, init snapshot survives
    def test_retention_rules_and_pinned_init_snapshot(self):
        ctx, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx)

        ops = journal.list_operations(self.env.state_dir)
        self.assertGreater(len(ops), 0)
        init_op_id = ops[0]["id"]

        # Create 24 sync apply operations
        for i in range(1, 25):
            t = datetime(2026, 11, 8, 12, i, 0, tzinfo=UTC)
            op = journal.begin(self.env.state_dir, f"sync-{i}", now=t)
            op.set_status(journal.DONE)

        # Also add an old open operation
        old_open = journal.begin(
            self.env.state_dir, "old-open", now=datetime(2026, 11, 8, 11, 0, 0, tzinfo=UTC)
        )

        # Decoy symlink inside a backup folder
        target_dir = self.home / "decoy_target"
        target_dir.mkdir()
        (target_dir / "survive.txt").write_text("survived", encoding="utf-8")

        # Find sync-1 op directory
        prune_candidates = [
            o for o in journal.list_operations(self.env.state_dir) if o["label"] == "sync-1"
        ]
        self.assertEqual(len(prune_candidates), 1)
        sync1_dir = self.env.state_dir / "ops" / prune_candidates[0]["id"]
        (sync1_dir / "files").mkdir(exist_ok=True)
        decoy_link = sync1_dir / "files" / "decoy"
        fsops.create_dir_link(decoy_link, target_dir)

        # Run prune with keep=20
        warnings = journal.prune(self.env.state_dir, keep=20)
        self.assertEqual(warnings, [])

        # Decoy target still exists!
        self.assertTrue((target_dir / "survive.txt").is_file())

        # Init op survives because it is pinned
        op_ids = {o["id"] for o in journal.list_operations(self.env.state_dir)}
        self.assertIn(init_op_id, op_ids)

        # Old open op survives
        self.assertIn(old_open.id, op_ids)

        # Repeated undo can undo exactly 20 operations
        undone = 0
        while True:
            u_ctx, out, _ = self._ctx()
            c = main(["undo"], context=u_ctx)
            if "Nothing to undo." in out.getvalue():
                break
            self.assertEqual(c, 0)
            undone += 1
        self.assertEqual(undone, 20)

    # 4. keep=0 behavior raises ValueError
    def test_prune_keep_zero_raises_value_error(self):
        with self.assertRaises(ValueError):
            journal.prune(self.env.state_dir, keep=0)

    # 5. status undoable count uses list_undoable
    def test_status_undoable_count_uses_list_undoable(self):
        ctx, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx)

        (self.env.config_dir / INSTRUCTIONS_NAME).write_text("# Edit 1\n", encoding="utf-8")
        ctx_s, _, _ = self._ctx()
        main(["sync", "--yes"], context=ctx_s)

        # Now undo
        ctx_u, _, _ = self._ctx()
        main(["undo"], context=ctx_u)

        # status --json
        ctx_stat, out_stat, _ = self._ctx()
        main(["status", "--json"], context=ctx_stat)
        stat_data = json.loads(out_stat.getvalue())

        expected_undoable = len(journal.list_undoable(self.env.state_dir))
        self.assertEqual(stat_data["undoable_operations"], expected_undoable)

    # 6. status --json with SourceError prints JSON with error field and exits 1
    def test_status_json_with_source_error_prints_json_and_exits_one(self):
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text(
            "invalid toml = [[[\n", encoding="utf-8"
        )
        ctx, out, err = self._ctx()
        code = main(["status", "--json"], context=ctx)
        self.assertEqual(code, 1)
        data = json.loads(out.getvalue())
        self.assertIn("error", data)

    # 7. undo messages:
    #    - drift vs non-drift RevertError
    #    - print reverted entry descriptions
    #    - keep recovered ids when followed by Nothing to undo
    #    - parser description drops "or a chosen one"
    #    - undo with empty journal creates no state folder or lock file
    def test_undo_empty_journal_creates_no_state_folder_or_lock(self):
        if self.env.state_dir.exists():
            fsops.remove_tree(self.env.state_dir)
        self.assertFalse(self.env.state_dir.exists())
        ctx, out, err = self._ctx()
        code = main(["undo"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("Nothing to undo.", out.getvalue())
        self.assertFalse(self.env.state_dir.exists())

    def test_undo_parser_description_drops_or_a_chosen_one(self):
        parser = build_parser()
        help_text = parser.format_help()
        self.assertNotIn("or a chosen one", help_text)

    def test_undo_prints_recovered_ids_when_followed_by_nothing_to_undo(self):
        self.env.state_dir.mkdir(exist_ok=True)
        # Plant an unfinished open operation with no done operations
        journal.begin(self.env.state_dir, "crashed-op")
        # op is left open

        ctx, out, _ = self._ctx()
        code = main(["undo"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("recovered unfinished operation(s):", out.getvalue())
        self.assertIn("Nothing to undo.", out.getvalue())

    def test_undo_prints_reverted_entry_descriptions(self):
        ctx, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx)

        (self.env.config_dir / INSTRUCTIONS_NAME).write_text(
            "# Changed instructions\n", encoding="utf-8"
        )
        ctx_s, _, _ = self._ctx()
        main(["sync", "--yes"], context=ctx_s)

        ctx_u, out_u, _ = self._ctx()
        code = main(["undo"], context=ctx_u)
        self.assertEqual(code, 0)
        self.assertIn("instructions", out_u.getvalue())

    # 8. Docstring fixes in revert.py and journal.py
    def test_docstrings_in_revert_and_journal_have_no_corruptions(self):
        for path in (
            _bootstrap.SRC / "one_setup_all_agents" / "engine" / "journal.py",
            _bootstrap.SRC / "one_setup_all_agents" / "engine" / "revert.py",
        ):
            raw = path.read_bytes()
            self.assertNotIn(b"\x0c", raw)
            text = raw.decode("utf-8")
            self.assertNotIn(" ailed", text)
            self.assertNotIn('"ailed', text)
            self.assertNotIn(" ecovered", text)
            self.assertNotIn('"ecovered', text)

    # 9. Missing sync tests
    def test_sync_tool_flag_restricts_ops_directly(self):
        ctx, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx)

        # Edit instructions in source
        (self.env.config_dir / INSTRUCTIONS_NAME).write_text(
            "# Tool restriction check\n", encoding="utf-8"
        )

        chosen = all_adapters()[0]
        ctx_s, _, _ = self._ctx()
        code = main(["sync", "--yes", "--tool", chosen.id], context=ctx_s)
        self.assertEqual(code, 0)

        # Inspect newest journal op
        last_op_id = journal.list_operations(self.env.state_dir)[0]["id"]
        last_op = journal.load(self.env.state_dir, last_op_id)

        # All entries must belong to chosen tool, not other tools
        chosen_paths = chosen.paths(self.env, Scope.GLOBAL)
        self.assertIn(str(chosen_paths.instructions), [e.get("path") for e in last_op.entries])
        for e in last_op.entries:
            path_str = e.get("path", "")
            for other in all_adapters():
                if other.id != chosen.id:
                    other_paths = other.paths(self.env, Scope.GLOBAL)
                    if other_paths.instructions:
                        self.assertNotEqual(path_str, str(other_paths.instructions))

    def test_sync_dry_run_exit_three_with_conflicts(self):
        ctx, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx)

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

        ctx_s, out_s, _ = self._ctx()
        code = main(["sync", "--dry-run"], context=ctx_s)
        self.assertEqual(code, 3)
        self.assertIn("Dry run", out_s.getvalue())

    def test_sync_secrets_planted_in_tool_config_never_appear_in_sync_output(self):
        ctx, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx)

        secret = "SUPER_SECRET_TOKEN_XYZ_999"
        literal_header = "Bearer secret-header-literal-abc"
        # Plant secret in claude tool config
        claude_cfg = self.home / ".claude.json"
        self.assertTrue(claude_cfg.is_file())
        data = json.loads(claude_cfg.read_text("utf-8"))
        data.setdefault("mcpServers", {})["secret_srv"] = {
            "command": "demo",
            "env": {"TOKEN": secret},
            "headers": {"Authorization": literal_header},
        }
        claude_cfg.write_text(json.dumps(data), encoding="utf-8")

        ctx_s, out_s, err_s = self._ctx()
        main(["sync", "--dry-run"], context=ctx_s)
        self.assertNotIn(secret, out_s.getvalue())
        self.assertNotIn(secret, err_s.getvalue())
        self.assertNotIn(literal_header, out_s.getvalue())
        self.assertNotIn(literal_header, err_s.getvalue())
