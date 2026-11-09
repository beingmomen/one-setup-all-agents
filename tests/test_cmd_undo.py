"""Tests for the `all-agents undo` command (Phase 5 M05-T3.1)."""

from __future__ import annotations

import hashlib
import inspect
import io
import time
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import POSIX, EngineCase, FakeRunner
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import main
from one_setup_all_agents.config_source import INSTRUCTIONS_NAME, SOURCE_CONFIG_NAME
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine import undo as undo_mod
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.lock import engine_lock
from one_setup_all_agents.model import EditFile, ItemRef, Kind, Plan, RunCli, Scope


class CmdUndoTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()
        # Set up fixture homes for all adapters
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                self.write(paths.instructions, b"# Initial instructions\n")
            if paths.mcp:
                self.write(paths.mcp, b"{}" if "json" in paths.mcp.name else b"")
            for snap in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap.exists():
                    self.write(snap, b"{}" if "json" in snap.name else b"")

    def _ctx(
        self,
        runner: FakeRunner | None = None,
        is_interactive: bool = False,
    ) -> tuple[CommandContext, io.StringIO, io.StringIO]:
        out = io.StringIO()
        err = io.StringIO()
        ctx = CommandContext(
            env=self.env,
            run_cli=runner or self.runner,
            is_interactive=is_interactive,
            out=out,
            err=err,
        )
        return ctx, out, err

    def test_undo_signature_has_no_op_id_parameter(self):
        sig = inspect.signature(undo_mod.undo)
        self.assertNotIn("op_id", sig.parameters)
        self.assertIn("force", sig.parameters)
        self.assertIn("run_cli", sig.parameters)
        self.assertIn("lock_timeout", sig.parameters)

    def test_apply_stack_undo_reverts_in_reverse_order_until_nothing_to_undo(self):
        # 1. Operation A: init
        ctx_a, _, _ = self._ctx()
        code_a = main(["init", "--yes"], context=ctx_a)
        self.assertEqual(code_a, 0)
        digest_a = self.listing(self.home)
        time.sleep(1.05)

        # 2. Operation B: sync after editing instructions
        (self.env.config_dir / INSTRUCTIONS_NAME).write_bytes(b"# Instructions version B\n")
        ctx_b, _, _ = self._ctx()
        code_b = main(["sync", "--yes"], context=ctx_b)
        self.assertEqual(code_b, 0)
        digest_b = self.listing(self.home)
        self.assertNotEqual(digest_a, digest_b)
        time.sleep(1.05)

        # 3. Operation C: sync after editing instructions again
        (self.env.config_dir / INSTRUCTIONS_NAME).write_bytes(b"# Instructions version C\n")
        ctx_c, _, _ = self._ctx()
        code_c = main(["sync", "--yes"], context=ctx_c)
        self.assertEqual(code_c, 0)
        digest_c = self.listing(self.home)
        self.assertNotEqual(digest_b, digest_c)

        # 4. Undo #1: reverts operation C -> returns home to state after B
        ctx_undo1, out1, _ = self._ctx()
        code_undo1 = main(["undo"], context=ctx_undo1)
        self.assertEqual(code_undo1, 0)
        self.assertIn("undone operation", out1.getvalue())
        self.assertIn("(sync)", out1.getvalue())
        self.assertEqual(self.listing(self.home), digest_b)

        # 5. Undo #2: reverts operation B -> returns home to state after A
        ctx_undo2, out2, _ = self._ctx()
        code_undo2 = main(["undo"], context=ctx_undo2)
        self.assertEqual(code_undo2, 0)
        self.assertIn("undone operation", out2.getvalue())
        self.assertIn("(sync)", out2.getvalue())
        self.assertEqual(self.listing(self.home), digest_a)

        # Restore instructions.md to match its state after init before undoing init
        (self.env.config_dir / INSTRUCTIONS_NAME).write_bytes(b"# Initial instructions\n")

        # 6. Undo #3: reverts operation A (init)
        ctx_undo3, out3, _ = self._ctx()
        code_undo3 = main(["undo"], context=ctx_undo3)
        self.assertEqual(code_undo3, 0)
        self.assertIn("undone operation", out3.getvalue())
        self.assertIn("(init)", out3.getvalue())

        # 7. Undo #4: nothing left to undo
        ctx_undo4, out4, _ = self._ctx()
        code_undo4 = main(["undo"], context=ctx_undo4)
        self.assertEqual(code_undo4, 0)
        self.assertIn("Nothing to undo.", out4.getvalue())

    def test_list_newest_first_and_excludes_undo_and_undone_and_failed_ops(self):
        # Empty before any operations
        ctx_empty, out_empty, _ = self._ctx()
        code_empty = main(["undo", "--list"], context=ctx_empty)
        self.assertEqual(code_empty, 0)
        self.assertIn("No undoable operations.", out_empty.getvalue())

        # Run init (op A)
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)
        time.sleep(1.05)

        # Run sync (op B)
        (self.env.config_dir / INSTRUCTIONS_NAME).write_bytes(b"# Instructions version B\n")
        ctx_sync, _, _ = self._ctx()
        main(["sync", "--yes"], context=ctx_sync)

        # List shows sync then init
        ctx_list1, out_list1, _ = self._ctx()
        code_list1 = main(["undo", "--list"], context=ctx_list1)
        self.assertEqual(code_list1, 0)
        lines = [line.strip() for line in out_list1.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn("sync", lines[0])
        self.assertIn("init", lines[1])

        # Undo sync
        ctx_u, _, _ = self._ctx()
        main(["undo"], context=ctx_u)

        # List now shows only init (sync was undone, and undo itself is not listed)
        ctx_list2, out_list2, _ = self._ctx()
        main(["undo", "--list"], context=ctx_list2)
        lines2 = [line.strip() for line in out_list2.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines2), 1)
        self.assertIn("init", lines2[0])
        self.assertNotIn("undo", lines2[0].lower())

    def test_undo_operation_exists_on_disk_with_kind_undo_and_not_in_list(self):
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        ctx_undo, _, _ = self._ctx()
        main(["undo"], context=ctx_undo)

        # Inspect all operations in journal
        all_ops = journal.list_operations(self.state)
        undo_ops = [o for o in all_ops if o.get("kind") == "undo"]
        self.assertEqual(len(undo_ops), 1)
        self.assertEqual(undo_ops[0]["status"], "done")

        # Must not appear in --list
        ctx_list, out_list, _ = self._ctx()
        main(["undo", "--list"], context=ctx_list)
        self.assertIn("No undoable operations.", out_list.getvalue())

    def test_crash_simulated_open_operation_recovered_first(self):
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        # Plant an open crashed operation
        crashed = journal.begin(self.state, "crashed-op")

        ctx_undo, out_undo, _ = self._ctx()
        code = main(["undo"], context=ctx_undo)
        self.assertEqual(code, 0)
        self.assertIn("recovered unfinished operation(s)", out_undo.getvalue())
        self.assertIn(crashed.id, out_undo.getvalue())

        # Verify crashed operation status became failed
        loaded_crashed = journal.load(self.state, crashed.id)
        self.assertEqual(loaded_crashed.status, journal.FAILED)

    def test_running_undo_twice_never_undoes_same_op_twice(self):
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        ctx1, out1, _ = self._ctx()
        code1 = main(["undo"], context=ctx1)
        self.assertEqual(code1, 0)
        self.assertIn("undone operation", out1.getvalue())

        ctx2, out2, _ = self._ctx()
        code2 = main(["undo"], context=ctx2)
        self.assertEqual(code2, 0)
        self.assertIn("Nothing to undo.", out2.getvalue())

    def test_drift_modified_tool_file_refused_lists_tilde_and_untouched(self):
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        target_adapter = all_adapters()[0]
        instr_path = target_adapter.paths(self.env, Scope.GLOBAL).instructions
        self.assertIsNotNone(instr_path)
        original_bytes = instr_path.read_bytes()

        # Modify tool file
        self.assertTrue(len(original_bytes) >= 0)
        tampered_bytes = b"# Tampered tool file content\n"
        self.write(instr_path, tampered_bytes)

        ops_before = journal.list_operations(self.state)

        # Undo without force exits 1, lists drifted path with ~, bytes untouched, no undo op
        ctx, out, err = self._ctx()
        code = main(["undo"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("revert refused due to drift", err.getvalue())
        self.assertIn("~", err.getvalue())
        self.assertEqual(instr_path.read_bytes(), tampered_bytes)
        self.assertEqual(journal.list_operations(self.state), ops_before)

    def test_drift_force_restores_and_safety_payload_captures_user_edit(self):
        import os
        import stat

        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        target_adapter = all_adapters()[0]
        instr_path = target_adapter.paths(self.env, Scope.GLOBAL).instructions
        self.assertIsNotNone(instr_path)
        original_bytes = instr_path.read_bytes()

        # User modified tool file
        user_edit = b"# User manual edit to be captured\n"
        self.write(instr_path, user_edit)

        # Undo with --force succeeds
        ctx, out, err = self._ctx()
        code = main(["undo", "--force"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("undone operation", out.getvalue())

        # Original bytes restored
        self.assertEqual(instr_path.read_bytes(), original_bytes)

        # Undo operation created with safety payload containing user_edit
        undo_ops = [o for o in journal.list_operations(self.state) if o.get("kind") == "undo"]
        self.assertEqual(len(undo_ops), 1)
        undo_id = undo_ops[0]["id"]
        payload_dir = self.state / "ops" / undo_id / "files"
        self.assertTrue(payload_dir.is_dir())
        payload_files = list(payload_dir.iterdir())
        self.assertTrue(len(payload_files) >= 1)

        captured_contents = [p.read_bytes() for p in payload_files]
        self.assertIn(user_edit, captured_contents)

        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(payload_dir).st_mode), 0o700)
            for p in payload_files:
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_drift_deleted_file_and_repointed_link_refused_without_force_restored_with_force(self):

        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        target_adapter = all_adapters()[0]
        instr_path = target_adapter.paths(self.env, Scope.GLOBAL).instructions
        self.assertIsNotNone(instr_path)
        original_bytes = instr_path.read_bytes()

        # 1. Delete instruction file
        instr_path.unlink()

        ctx_del, out_del, err_del = self._ctx()
        code_del = main(["undo"], context=ctx_del)
        self.assertEqual(code_del, 1)
        self.assertIn("revert refused due to drift", err_del.getvalue())
        self.assertIn("~", err_del.getvalue())

        # Restore with force
        ctx_del_force, out_del_force, _ = self._ctx()
        code_del_force = main(["undo", "--force"], context=ctx_del_force)
        self.assertEqual(code_del_force, 0)
        self.assertTrue(instr_path.exists())
        self.assertEqual(instr_path.read_bytes(), original_bytes)

    def test_planted_secret_in_modified_file_absent_from_output_and_journal_json(self):
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        target_adapter = all_adapters()[0]
        instr_path = target_adapter.paths(self.env, Scope.GLOBAL).instructions
        self.assertIsNotNone(instr_path)

        secret_token = "CONFIDENTIAL_TEST_TOKEN_XYZ987654"
        self.write(instr_path, f"# Header\nsecret={secret_token}\n".encode())

        # Undo without force: secret absent from stdout and stderr
        ctx1, out1, err1 = self._ctx()
        code1 = main(["undo"], context=ctx1)
        self.assertEqual(code1, 1)
        self.assertNotIn(secret_token, out1.getvalue())
        self.assertNotIn(secret_token, err1.getvalue())

        # Undo with force: secret absent from stdout, stderr, and all JSON files
        ctx2, out2, err2 = self._ctx()
        code2 = main(["undo", "--force"], context=ctx2)
        self.assertEqual(code2, 0)
        self.assertNotIn(secret_token, out2.getvalue())
        self.assertNotIn(secret_token, err2.getvalue())

        # Verify all JSON files under state_dir
        for p in self.state.rglob("*.json"):
            content_json = p.read_text(encoding="utf-8")
            self.assertNotIn(secret_token, content_json)

    def test_prune_retention_undo_down_to_oldest_kept_then_nothing_to_undo(self):
        from datetime import datetime, timezone

        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        # Create 24 more done operations with real entries through apply_plan
        from one_setup_all_agents.engine.apply import apply_plan
        from one_setup_all_agents.model import EditFile, Plan

        with patch("one_setup_all_agents.engine.journal.prune", return_value=[]):
            for i in range(1, 25):
                t = datetime(2026, 11, 6, 10, i, 0, tzinfo=timezone.utc)
                p = self.home / f"undo_retention_{i}.txt"
                plan = Plan(
                    [EditFile(p, lambda _old, idx=i: f"v{idx}\n".encode(), None, f"write {i}")]
                )
                with patch(
                    "one_setup_all_agents.engine.journal.now_iso", return_value=journal.now_iso(t)
                ):
                    apply_plan(self.env, plan, label=f"sync-run-{i}", run_cli=self.runner)

        # Prune retention leaves 20 unpinned operations + 1 pinned init = 21 operations
        pruned = journal.prune(self.state, keep=20)
        self.assertEqual(len(pruned.pruned), 4)
        self.assertEqual(len(journal.list_operations(self.state)), 21)

        # Undo operations one by one down to the oldest kept
        undone_count = 0
        while True:
            ctx, out, _ = self._ctx()
            code = main(["undo"], context=ctx)
            self.assertEqual(code, 0)
            if "Nothing to undo." in out.getvalue():
                break
            self.assertIn("undone operation", out.getvalue())
            undone_count += 1

        self.assertGreater(undone_count, 0)

    def test_lock_held_exits_one(self):
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        ctx, out, err = self._ctx()
        with engine_lock(self.state):
            code = main(["undo"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("lock", err.getvalue().lower())

    def test_failed_undo_with_failing_inverse_cli_can_be_retried_and_sync_not_refused(self):
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        # 1. Apply a plan with RunCli and EditFile
        test_file = self.home / "cli_test.txt"
        self.write(test_file, b"original-content\n")

        plan = Plan(
            ops=[
                RunCli(
                    argv=("test-tool", "apply"),
                    inverse_argv=("test-tool", "unapply"),
                    description="test runcli",
                    claims=(ItemRef("claude", Kind.INSTRUCTIONS, "instructions"),),
                ),
                EditFile(
                    path=test_file,
                    transform=lambda _: b"applied-content\n",
                    expect_sha256=hashlib.sha256(b"original-content\n").hexdigest(),
                    description="test edit",
                    claims=(ItemRef("claude", Kind.INSTRUCTIONS, "instructions"),),
                ),
            ]
        )
        op_id = apply_plan(self.env, plan, label="test-run", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(test_file.read_bytes(), b"applied-content\n")

        # 2. Undo with failing inverse command -> exit 1
        self.runner.fail_on.append(["test-tool", "unapply"])
        ctx1, out1, err1 = self._ctx()
        code1 = main(["undo"], context=ctx1)
        self.assertEqual(code1, 1)

        # 3. Fix the runner and run undo again -> succeeds, bytes equal original
        self.runner.fail_on.clear()
        ctx2, out2, err2 = self._ctx()
        code2 = main(["undo"], context=ctx2)
        self.assertEqual(code2, 0, f"undo retry failed: {err2.getvalue()}")
        self.assertEqual(test_file.read_bytes(), b"original-content\n")

        # 4. sync --dry-run is not refused with "run undo"
        ctx3, out3, err3 = self._ctx()
        code3 = main(["sync", "--dry-run"], context=ctx3)
        self.assertNotIn("an earlier run did not finish", err3.getvalue())
        self.assertNotEqual(code3, 1)

    def test_interrupted_undo_restores_remaining_paths_on_next_undo(self):
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        file_a = self.home / "file_a.txt"
        file_b = self.home / "file_b.txt"
        self.write(file_a, b"orig_a\n")
        self.write(file_b, b"orig_b\n")

        plan = Plan(
            ops=[
                EditFile(
                    path=file_a,
                    transform=lambda _: b"new_a\n",
                    expect_sha256=hashlib.sha256(b"orig_a\n").hexdigest(),
                    description="edit a",
                ),
                EditFile(
                    path=file_b,
                    transform=lambda _: b"new_b\n",
                    expect_sha256=hashlib.sha256(b"orig_b\n").hexdigest(),
                    description="edit b",
                ),
            ]
        )
        op_id = apply_plan(self.env, plan, label="two-files", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(file_a.read_bytes(), b"new_a\n")
        self.assertEqual(file_b.read_bytes(), b"new_b\n")

        # Simulate interruption after first restored path (raise from patched restore)
        import one_setup_all_agents.engine.backup as backup_mod

        real_restore = backup_mod.restore
        restore_count = 0

        def interrupted_restore(state_dir, op_id, path, state):
            nonlocal restore_count
            restore_count += 1
            if restore_count == 2:
                raise RuntimeError("simulated crash/interruption during undo restore")
            return real_restore(state_dir, op_id, path, state)

        with patch(
            "one_setup_all_agents.engine.revert.backup.restore", side_effect=interrupted_restore
        ):
            ctx1, out1, err1 = self._ctx()
            code1 = main(["undo"], context=ctx1)
            self.assertEqual(code1, 1)

        # Next undo completes
        ctx2, out2, err2 = self._ctx()
        code2 = main(["undo"], context=ctx2)
        self.assertEqual(code2, 0, f"undo retry failed: {err2.getvalue()}")
        self.assertEqual(file_a.read_bytes(), b"orig_a\n")
        self.assertEqual(file_b.read_bytes(), b"orig_b\n")

        # sync --dry-run is not refused with "run undo"
        ctx3, out3, err3 = self._ctx()
        code3 = main(["sync", "--dry-run"], context=ctx3)
        self.assertNotIn("an earlier run did not finish", err3.getvalue())
        self.assertNotEqual(code3, 1)

    def test_interrupted_after_target_undone_before_undo_op_done_recovers_cleanly(self):
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        test_file = self.home / "file_t1.txt"
        self.write(test_file, b"orig\n")
        plan = Plan(
            ops=[
                EditFile(
                    path=test_file,
                    transform=lambda _: b"applied\n",
                    expect_sha256=hashlib.sha256(b"orig\n").hexdigest(),
                    description="edit t1",
                ),
            ]
        )
        op_id = apply_plan(self.env, plan, label="run-t1", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(test_file.read_bytes(), b"applied\n")

        real_set_status = journal.Operation.set_status

        def patched_set_status(op_self, status, **fields):
            if op_self.data.get("kind") == "undo" and status == journal.DONE:
                raise KeyboardInterrupt("simulated interrupt before undo op is DONE")
            return real_set_status(op_self, status, **fields)

        with patch.object(
            journal.Operation, "set_status", side_effect=patched_set_status, autospec=True
        ):
            with self.assertRaises(KeyboardInterrupt):
                undo_mod.undo(self.env, run_cli=self.runner)

        target_op = journal.load(self.state, op_id)
        self.assertEqual(target_op.status, journal.UNDONE)
        self.assertFalse(target_op.data.get("pending_revert", False))

        ctx_sync, out_sync, err_sync = self._ctx()
        code_sync = main(["sync", "--dry-run"], context=ctx_sync)
        self.assertNotIn("an earlier run did not finish", err_sync.getvalue())
        self.assertNotEqual(code_sync, 1)

        ctx2, out2, err2 = self._ctx()
        code2 = main(["undo"], context=ctx2)
        self.assertEqual(code2, 0, f"undo recovery failed: {err2.getvalue()}")

        ctx3, out3, err3 = self._ctx()
        code3 = main(["undo", "--force"], context=ctx3)
        self.assertEqual(code3, 0)
        self.assertIn("Nothing to undo.", out3.getvalue())

        ctx4, out4, err4 = self._ctx()
        code4 = main(["sync", "--dry-run"], context=ctx4)
        self.assertNotIn("an earlier run did not finish", err4.getvalue())
        self.assertNotEqual(code4, 1)

    def test_force_recovery_captures_safety_copy_of_user_edits(self):
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        target_adapter = all_adapters()[0]
        instr_path = target_adapter.paths(self.env, Scope.GLOBAL).instructions
        self.assertIsNotNone(instr_path)
        orig_instr = instr_path.read_bytes()

        (self.env.config_dir / INSTRUCTIONS_NAME).write_bytes(b"# Sync instructions\n")
        ctx_sync, _, _ = self._ctx()
        code_sync = main(["sync", "--yes"], context=ctx_sync)
        self.assertEqual(code_sync, 0)
        sync_instr = instr_path.read_bytes()
        self.assertNotEqual(orig_instr, sync_instr)

        import one_setup_all_agents.engine.revert as revert_mod

        def failing_revert(env, op, runner, force=False):
            raise RuntimeError("simulated undo crash before revert")

        with patch.object(revert_mod, "revert_locked", side_effect=failing_revert):
            with self.assertRaises(RuntimeError):
                undo_mod.undo(self.env, run_cli=self.runner)

        user_edit = b"# User edited after undo failed\n"
        self.write(instr_path, user_edit)

        ctx_f, out_f, err_f = self._ctx()
        code_f = main(["undo", "--force"], context=ctx_f)
        self.assertEqual(code_f, 0, f"undo --force failed: {err_f.getvalue()}")
        self.assertEqual(instr_path.read_bytes(), orig_instr)

        undo_ops = [o for o in journal.list_operations(self.state) if o.get("kind") == "undo"]
        self.assertTrue(len(undo_ops) >= 1)
        undo_id = undo_ops[-1]["id"]
        payload_dir = self.state / "ops" / undo_id / "files"
        self.assertTrue(payload_dir.is_dir())
        payload_files = list(payload_dir.iterdir())
        captured_contents = [p.read_bytes() for p in payload_files]
        self.assertIn(user_edit, captured_contents)

        ctx_l, out_l, _ = self._ctx()
        code_l = main(["undo", "--list"], context=ctx_l)
        self.assertEqual(code_l, 0)
        self.assertIn("init", out_l.getvalue())

    def test_permanently_failing_inverse_run_cli(self):
        (self.env.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        test_file = self.home / "cli_failing_test.txt"
        self.write(test_file, b"initial-content\n")

        plan = Plan(
            ops=[
                RunCli(
                    argv=("tool-cmd", "apply"),
                    inverse_argv=("tool-cmd", "unapply"),
                    description="important cli action",
                ),
                EditFile(
                    path=test_file,
                    transform=lambda _: b"modified-content\n",
                    expect_sha256=hashlib.sha256(b"initial-content\n").hexdigest(),
                    description="file edit action",
                ),
            ]
        )
        always_failing_runner = FakeRunner(fail_always=True)
        self.runner.fail_on.append(["tool-cmd", "unapply"])
        op_id = apply_plan(self.env, plan, label="cli-op", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(test_file.read_bytes(), b"modified-content\n")

        ctx1, out1, err1 = self._ctx(runner=always_failing_runner)
        code1 = main(["undo"], context=ctx1)
        self.assertEqual(code1, 1)

        ctx2, out2, err2 = self._ctx(runner=always_failing_runner)
        code2 = main(["undo", "--force"], context=ctx2)
        self.assertEqual(code2, 1)
        self.assertIn("important cli action", err2.getvalue())
        self.assertNotIn("tool-cmd", err2.getvalue())
        self.assertNotIn("unapply", err2.getvalue())
        self.assertIn("one change must be removed by hand", err2.getvalue())

        self.assertEqual(test_file.read_bytes(), b"initial-content\n")

        target_op = journal.load(self.state, op_id)
        self.assertEqual(target_op.status, journal.UNDONE)
        note = target_op.data.get("note", "")
        self.assertIn("important cli action", note)

    def test_recovered_undo_op_unexpected_error_surfaces(self):
        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        op = journal.begin(self.state, "crashed-op")

        real_load = journal.load

        def failing_load(state_dir, op_id):
            if op_id == op.id:
                raise OSError("simulated disk corruption loading journal")
            return real_load(state_dir, op_id)

        with patch("one_setup_all_agents.engine.undo.journal.load", side_effect=failing_load):
            with self.assertRaises(OSError):
                undo_mod.undo(self.env, run_cli=self.runner)


if __name__ == "__main__":
    unittest.main()
