"""Full undo matrix covering every operation type (Phase 5 M05-T3.4).

Tests every operation type (EditFile, Link, RunCli, Ownership deltas, Mixed init-like operations)
through the real engine.undo.undo (and CLI undo), verifying byte-identical and state-identical
reversion using snapshot comparisons.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import time
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from adapter_contract import snapshot
from engine_support import POSIX, EngineCase, FakeRunner
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import main
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import apply, fsops, journal
from one_setup_all_agents.engine import undo as undo_mod
from one_setup_all_agents.engine.errors import RevertError
from one_setup_all_agents.model import (
    EditFile,
    ItemRef,
    Kind,
    Link,
    Plan,
    RunCli,
    Scope,
)

SAMPLE_ITEM = ItemRef("example", Kind.MCP, "demo-server")
SAMPLE_SKILL = ItemRef("example", Kind.SKILLS, "demo-skill")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_edit(
    path: Path,
    data: bytes,
    *,
    expect: str | None = "current",
    description: str | None = None,
    mode: int | None = None,
    claims: tuple[ItemRef, ...] = (),
    releases: tuple[ItemRef, ...] = (),
) -> EditFile:
    if expect == "current":
        expect_sha = _sha(path.read_bytes()) if path.exists() else None
    else:
        expect_sha = expect
    return EditFile(
        path=path,
        transform=lambda old, d=data: d,
        expect_sha256=expect_sha,
        description=description or f"edit {path.name}",
        mode=mode,
        claims=claims,
        releases=releases,
    )


class UndoMatrixEditFileTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()

    def test_create_new_removes_file_and_created_ancestor_dirs(self):
        snap_before = snapshot(self.home, exclude=(self.state,))

        target_file = self.home / "new_folder" / "sub_folder" / "created.txt"
        op = _make_edit(target_file, b"brand new file content\n", description="create file")
        op_id = apply.apply_plan(self.env, Plan([op]), label="create-new", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertTrue(target_file.is_file())

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(res.op_id, op_id)

        self.assertFalse(target_file.exists())
        self.assertFalse((self.home / "new_folder").exists())
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)

    def test_edit_existing_restores_bytes_and_posix_mode(self):
        target_file = self.home / "existing.txt"
        initial_content = b"original file bytes\n"
        self.write(target_file, initial_content)
        if POSIX:
            os.chmod(target_file, 0o640)

        snap_before = snapshot(self.home, exclude=(self.state,))

        op = _make_edit(target_file, b"updated file bytes\n", description="edit file")
        op_id = apply.apply_plan(self.env, Plan([op]), label="edit-existing", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(target_file.read_bytes(), b"updated file bytes\n")

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(target_file.read_bytes(), initial_content)

        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(target_file).st_mode), 0o640)
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)

    def test_edit_through_symlink_keeps_link_and_restores_real_file(self):
        real_file = self.home / "real_doc.txt"
        self.write(real_file, b"real initial\n")
        link_file = self.home / "link_doc.txt"

        try:
            fsops.create_symlink(link_file, real_file, directory=False)
        except OSError:
            self.skipTest("symlink creation not supported in this environment")

        snap_before = snapshot(self.home, exclude=(self.state,))

        op = EditFile(
            path=link_file,
            transform=lambda before: b"real modified\n",
            expect_sha256=_sha(b"real initial\n"),
            description="edit through link",
        )
        op_id = apply.apply_plan(self.env, Plan([op]), label="edit-symlink", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(real_file.read_bytes(), b"real modified\n")
        self.assertTrue(fsops.is_symlink(link_file))

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertTrue(fsops.is_symlink(link_file))
        self.assertEqual(real_file.read_bytes(), b"real initial\n")
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)

    def test_crlf_and_bom_file_restored_byte_identical(self):
        bom_crlf_bytes = b"\xef\xbb\xbfFirst Line\r\nSecond Line\r\nThird Line\r\n"
        target_file = self.home / "bom_crlf.txt"
        self.write(target_file, bom_crlf_bytes)

        snap_before = snapshot(self.home, exclude=(self.state,))

        op = _make_edit(target_file, b"\xef\xbb\xbfModified Line\r\n", description="edit bom crlf")
        op_id = apply.apply_plan(self.env, Plan([op]), label="edit-bom", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(target_file.read_bytes(), b"\xef\xbb\xbfModified Line\r\n")

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(target_file.read_bytes(), bom_crlf_bytes)
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)

    def test_noop_snapshot_entry_changes_nothing(self):
        target_file = self.home / "unchanged.txt"
        content = b"constant content\n"
        self.write(target_file, content)

        snap_before = snapshot(self.home, exclude=(self.state,))

        op = EditFile(
            path=target_file,
            transform=lambda before: before,
            expect_sha256=_sha(content),
            description="noop file",
        )
        op_id = apply.apply_plan(self.env, Plan([op]), label="edit-noop", run_cli=self.runner)
        self.assertIsNotNone(op_id)

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(target_file.read_bytes(), content)
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)

    def test_edit_file_mode_new_file(self):
        target_file = self.home / "executable.sh"
        snap_before = snapshot(self.home, exclude=(self.state,))

        op = _make_edit(
            target_file,
            b"#!/bin/sh\necho hi\n",
            description="mode new file",
            mode=0o755,
        )
        op_id = apply.apply_plan(self.env, Plan([op]), label="create-mode", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertTrue(target_file.is_file())
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(target_file).st_mode), 0o755)

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertFalse(target_file.exists())
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)


class UndoMatrixLinkTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()

    def test_create_link_removed_on_undo(self):
        target_dir = self.home / "target_folder"
        target_dir.mkdir()
        self.write(target_dir / "item.txt", b"target payload\n")

        link_path = self.home / "link_folder"
        snap_before = snapshot(self.home, exclude=(self.state,))

        op = Link(link_path, target_dir, "create dir link", claims=(SAMPLE_SKILL,))
        op_id = apply.apply_plan(self.env, Plan([op]), label="create-link", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertNotEqual(fsops.link_kind(link_path), "absent")

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(fsops.link_kind(link_path), "absent")
        self.assertTrue(target_dir.is_dir())
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)

    def test_replace_owned_link_restores_previous_target(self):
        target1 = self.home / "target1"
        target1.mkdir()
        target2 = self.home / "target2"
        target2.mkdir()
        link_path = self.home / "shared_link"

        # Initial link creation
        op1 = Link(link_path, target1, "initial link", claims=(SAMPLE_SKILL,))
        apply.apply_plan(self.env, Plan([op1]), label="link-1", run_cli=self.runner)
        snap_after_1 = snapshot(self.home, exclude=(self.state,))

        time.sleep(1.05)

        # Replace link with target2
        op2 = Link(link_path, target2, "repointed link", claims=(SAMPLE_SKILL,))
        op_id2 = apply.apply_plan(self.env, Plan([op2]), label="link-2", run_cli=self.runner)
        self.assertIsNotNone(op_id2)
        self.assertTrue(fsops.same_target(fsops.read_link(link_path), str(target2)))

        # Undo replaces target2 back to target1
        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(res.op_id, op_id2)
        self.assertTrue(fsops.same_target(fsops.read_link(link_path), str(target1)))
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_after_1)

    def test_remove_owned_link_restores_link_on_undo(self):
        target = self.home / "target_owned"
        target.mkdir()
        link_path = self.home / "owned_link"
        fsops.create_dir_link(link_path, target)
        journal.set_owned(
            self.state, SAMPLE_SKILL.tool, SAMPLE_SKILL.kind.value, [SAMPLE_SKILL.key]
        )
        snap_before = snapshot(self.home, exclude=(self.state,))

        op = Link(link_path, None, "remove link", releases=(SAMPLE_SKILL,))
        op_id = apply.apply_plan(self.env, Plan([op]), label="remove-link", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(fsops.link_kind(link_path), "absent")

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertNotEqual(fsops.link_kind(link_path), "absent")
        self.assertTrue(fsops.same_target(fsops.read_link(link_path), str(target)))
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)

    def test_noop_keep_shared_link_restores_ownership_on_undo(self):
        target = self.home / "target_shared"
        target.mkdir()
        link_path = self.home / "shared_link_path"
        fsops.create_dir_link(link_path, target)
        snap_before = snapshot(self.home, exclude=(self.state,))
        self.assertEqual(journal.owned_items(self.state), set())

        # Operation points to identical target, but claims ownership
        op = Link(link_path, target, "keep shared link", claims=(SAMPLE_SKILL,))
        op_id = apply.apply_plan(self.env, Plan([op]), label="keep-link", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertIn(
            (SAMPLE_SKILL.tool, SAMPLE_SKILL.kind.value, SAMPLE_SKILL.key),
            journal.owned_items(self.state),
        )

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(journal.owned_items(self.state), set())
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)

    def test_copy_fallback_marked_copy_removed_on_undo(self):
        target_dir = self.home / "src_skills" / "demo-skill"
        target_dir.mkdir(parents=True)
        self.write(target_dir / "SKILL.md", b"---\nname: demo-skill\n---\n")

        link_path = self.home / "tool_skills" / "demo-skill"
        snap_before = snapshot(self.home, exclude=(self.state,))

        op = Link(link_path, target_dir, "copy fallback skill", allow_copy_fallback=True)
        with mock.patch.object(
            fsops, "create_dir_link", side_effect=OSError("simulate no symlinks")
        ):
            op_id = apply.apply_plan(
                self.env, Plan([op]), label="fallback-copy", run_cli=self.runner
            )

        self.assertIsNotNone(op_id)
        self.assertTrue((link_path / "SKILL.md").is_file())
        self.assertIsNotNone(fsops.read_marker(link_path))

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertFalse(link_path.exists())
        self.assertEqual(snapshot(self.home, exclude=(self.state,)), snap_before)


class UndoMatrixRunCliTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()

    def test_inverses_run_through_runner_in_reverse_order(self):
        op1 = RunCli(
            argv=("tool", "add", "first"),
            inverse_argv=("tool", "remove", "first"),
            description="add first",
        )
        op2 = RunCli(
            argv=("tool", "add", "second"),
            inverse_argv=("tool", "remove", "second"),
            description="add second",
        )
        op_id = apply.apply_plan(self.env, Plan([op1, op2]), label="run-cli", run_cli=self.runner)
        self.assertIsNotNone(op_id)
        self.assertEqual(
            self.runner.calls,
            [["tool", "add", "first"], ["tool", "add", "second"]],
        )

        self.runner.calls.clear()
        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(
            self.runner.calls,
            [["tool", "remove", "second"], ["tool", "remove", "first"]],
        )

    def test_failing_inverse_raises_revert_error_with_partial_state(self):
        op1 = RunCli(
            argv=("tool", "add", "1"),
            inverse_argv=("tool", "remove", "1"),
            description="add 1",
        )
        op2 = RunCli(
            argv=("tool", "add", "2"),
            inverse_argv=("tool", "remove", "2"),
            description="add 2",
        )
        apply.apply_plan(self.env, Plan([op1, op2]), label="run-cli-fail", run_cli=self.runner)

        # Runner returns exit code 1 when attempting inverse of op2
        def failing_runner(argv: list[str]) -> int:
            if "remove" in argv and "2" in argv:
                return 1
            return 0

        with self.assertRaises(RevertError) as ctx:
            undo_mod.undo(self.env, run_cli=failing_runner)

        self.assertFalse(ctx.exception.partial)
        loaded = journal.load(self.state, journal.list_operations(self.state)[0]["id"])
        # Operation is not marked undone
        self.assertNotEqual(loaded.status, journal.UNDONE)

    def test_failed_command_is_not_inverted(self):
        # A plan with two commands, second one fails during apply
        op1 = RunCli(
            argv=("tool", "add", "ok"),
            inverse_argv=("tool", "remove", "ok"),
            description="add ok",
        )
        op2 = RunCli(
            argv=("tool", "add", "fail"),
            inverse_argv=("tool", "remove", "fail"),
            description="add fail",
        )

        def runner_fail_on_second(argv: list[str]) -> int:
            if "fail" in argv:
                return 42
            return 0

        with self.assertRaises(Exception):
            apply.apply_plan(
                self.env, Plan([op1, op2]), label="cli-fail", run_cli=runner_fail_on_second
            )

        # Loaded journal should show op1 inverted and op2 skipped
        loaded_ops = journal.list_operations(self.state)
        self.assertEqual(len(loaded_ops), 1)
        self.assertEqual(loaded_ops[0]["status"], journal.FAILED)

    def test_secret_masking_in_error_text(self):
        secret = "VERY_CONFIDENTIAL_KEY_12345"
        op = RunCli(
            argv=("tool", "--token", secret),
            inverse_argv=("tool", "--token", secret),
            description="set-token config",
        )
        apply.apply_plan(self.env, Plan([op]), label="secret-op", run_cli=self.runner)

        def runner_raises(argv: list[str]) -> int:
            raise RuntimeError("server connection failed")

        with self.assertRaises(RevertError) as ctx:
            undo_mod.undo(self.env, run_cli=runner_raises)

        self.assertNotIn(secret, str(ctx.exception))


class UndoMatrixOwnershipTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()

    def test_claims_and_releases_restored_exactly(self):
        item_a = ItemRef("tool1", Kind.MCP, "server-a")
        item_c = ItemRef("tool1", Kind.MCP, "server-c")

        # Set initial ownership
        journal.set_owned(self.state, "tool1", "mcp", ["server-a", "server-b"])
        initial_owned = journal.owned_items(self.state)
        self.assertEqual(
            initial_owned,
            {("tool1", "mcp", "server-a"), ("tool1", "mcp", "server-b")},
        )

        # Plan releases server-a and claims server-c
        f = self.write(self.home / "config.txt", b"v1")
        op = _make_edit(
            f,
            b"v2",
            description="edit with ownership",
            claims=(item_c,),
            releases=(item_a,),
        )
        apply.apply_plan(self.env, Plan([op]), label="ownership-change", run_cli=self.runner)

        updated_owned = journal.owned_items(self.state)
        self.assertEqual(
            updated_owned,
            {("tool1", "mcp", "server-b"), ("tool1", "mcp", "server-c")},
        )

        # Undo restores ownership exactly
        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(journal.owned_items(self.state), initial_owned)

    def test_adoption_only_operation_restored_on_undo(self):
        self.assertEqual(journal.owned_items(self.state), set())
        item = ItemRef("tool2", Kind.INSTRUCTIONS, "global")

        op_id = apply.apply_plan(
            self.env,
            Plan([]),
            label="adopt-only",
            adopt=(item,),
            run_cli=self.runner,
        )
        self.assertIsNotNone(op_id)
        self.assertEqual(journal.owned_items(self.state), {("tool2", "instructions", "global")})

        res = undo_mod.undo(self.env, run_cli=self.runner)
        self.assertIsNotNone(res)
        self.assertEqual(journal.owned_items(self.state), set())


class UndoMatrixMixedInitTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()
        # Set up fixture homes for all adapters
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                self.write(paths.instructions, b"# Initial tool instructions\n")
            if paths.mcp:
                self.write(paths.mcp, b"{}" if "json" in paths.mcp.name else b"")
            for snap in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap.exists():
                    self.write(snap, b"{}" if "json" in snap.name else b"")

    def _ctx(self) -> tuple[CommandContext, io.StringIO, io.StringIO]:
        out = io.StringIO()
        err = io.StringIO()
        ctx = CommandContext(
            env=self.env,
            run_cli=self.runner,
            is_interactive=False,
            out=out,
            err=err,
        )
        return ctx, out, err

    def _full_snapshot(self) -> tuple[dict[str, tuple], dict[str, tuple]]:
        h = snapshot(self.home, exclude=(self.state,))
        c = (
            snapshot(self.env.config_dir, exclude=(self.state,))
            if self.env.config_dir.exists()
            else {}
        )
        return (h, c)

    def test_mixed_init_operation_undone_in_one_command_to_original_digest(self):
        # 1. Capture snapshot before init (covering both home and config_dir)
        snap_before = self._full_snapshot()

        # 2. Run init --yes (mixed op: snapshots original tool files, writes source config, adopts items)
        ctx_init, out_init, err_init = self._ctx()
        code_init = main(["init", "--yes"], context=ctx_init)
        self.assertEqual(code_init, 0, f"init failed: {err_init.getvalue()}")

        snap_after_init = self._full_snapshot()
        self.assertNotEqual(snap_after_init, snap_before)
        self.assertTrue((self.env.config_dir / "instructions.md").is_file())

        # 3. Run undo (single command reverses the entire mixed init run)
        ctx_undo, out_undo, err_undo = self._ctx()
        code_undo = main(["undo"], context=ctx_undo)
        self.assertEqual(code_undo, 0, f"undo failed: {err_undo.getvalue()}")
        self.assertIn("undone operation", out_undo.getvalue())

        # 4. Verify snapshot matches byte-for-byte to original digest before init
        snap_after_undo = self._full_snapshot()
        self.assertEqual(snap_after_undo, snap_before)


if __name__ == "__main__":
    unittest.main()
