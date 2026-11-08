"""engine/apply.py and engine/revert.py: write-ahead, automatic revert, drift, crash recovery (ADR 0005, 0010)."""

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import POSIX, EngineCase, FakeRunner
from one_setup_all_agents.engine import apply as apply_module
from one_setup_all_agents.engine import backup as backup_module
from one_setup_all_agents.engine import fsops, journal
from one_setup_all_agents.engine.apply import apply_plan, recover_operation
from one_setup_all_agents.engine.errors import (
    ApplyError,
    EngineLockedError,
    OpenOperationError,
    PlanError,
    RevertError,
)
from one_setup_all_agents.engine.lock import engine_lock
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import (
    Conflict,
    EditFile,
    ItemRef,
    Kind,
    Link,
    Plan,
    RunCli,
    Unsupported,
)

MCP = ItemRef("example", Kind.MCP, "demo-server")
SKILL = ItemRef("example", Kind.SKILLS, "demo-skill")
INSTR = ItemRef("example", Kind.INSTRUCTIONS, "instructions")


def sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


class Crash(BaseException):
    """Stands for the process dying: nothing may catch it and clean up."""


class ApplyCase(EngineCase):
    def edit(self, path, data, *, expect="current", **kw):
        if expect == "current":
            expect = sha(path.read_bytes()) if path.exists() else None
        return EditFile(path, lambda old, d=data: d, expect, f"write {path.name}", **kw)

    def apply(self, *ops, runner=None, label="test"):
        return apply_plan(self.env, Plan(list(ops)), label=label, run_cli=runner or FakeRunner())

    def revert(self, op_id, runner=None, force=False):
        return revert_operation(self.env, op_id, force=force, run_cli=runner or FakeRunner())

    def entries(self, op_id):
        return journal.load(self.state, op_id).entries

    @contextlib.contextmanager
    def process_dies(self):
        """Simulate the process being killed: `Crash` leaves the run and NO cleanup, entry
        marking or revert happens, so the journal stays exactly as the write-ahead left it."""
        with (
            mock.patch.object(apply_module._Run, "_fail_entry", lambda *args, **kwargs: None),
            mock.patch.object(apply_module, "_fail_run", side_effect=Crash),
            self.assertRaises(Crash),
        ):
            yield


class BasicApplyTests(ApplyCase):
    def test_a_plan_is_applied_journaled_and_owned(self):
        existing = self.write(self.home / "a.txt", b"old", 0o640)
        new = self.home / "deep" / "er" / "b.txt"
        op_id = self.apply(
            self.edit(existing, b"new", claims=(INSTR,)),
            self.edit(new, b"fresh", sensitive=True, claims=(MCP,)),
        )
        self.assertEqual(existing.read_bytes(), b"new")
        self.assertEqual(new.read_bytes(), b"fresh")
        if POSIX:
            self.assertEqual(self.mode(existing), 0o640, "an existing mode is preserved")
            self.assertEqual(self.mode(new), 0o600, "a new sensitive file is 0600")
        op = journal.load(self.state, op_id)
        self.assertEqual((op.status, [e["status"] for e in op.entries]), ("done", ["done", "done"]))
        first, second = op.entries
        self.assertEqual(first["before"]["state"], "file")
        self.assertEqual(first["before"]["sha256"], sha(b"old"))
        self.assertEqual(first["after"]["sha256"], sha(b"new"))
        self.assertEqual(second["before"], {"state": "absent"})
        self.assertEqual(len(second["created_dirs"]), 2)
        self.assertEqual(
            first["claims"], [{"tool": "example", "kind": "instructions", "key": "instructions"}]
        )
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"demo-server"})
        self.assertEqual(journal.owned_for(self.state, "example", "instructions"), {"instructions"})
        self.assertEqual(journal.list_operations(self.state)[0]["status"], "done")
        self.assertEqual(op.data["owned_delta"]["removed"], [])

    def test_unsupported_and_conflict_are_never_executed(self):
        target = self.home / "a.txt"
        plan = Plan(
            [
                Unsupported("example", Kind.HOOKS, "no hooks"),
                Conflict("example", Kind.MCP, "demo-server", "exists", target),
                self.edit(target, b"x"),
            ]
        )
        op_id = apply_plan(self.env, plan, label="t", run_cli=FakeRunner())
        self.assertEqual(len(self.entries(op_id)), 1)

    def test_a_plan_with_nothing_to_execute_records_nothing(self):
        plan = Plan([Unsupported("example", Kind.HOOKS, "r"), Conflict("e", Kind.MCP, "k", "r")])
        self.assertIsNone(apply_plan(self.env, plan, label="t"))
        self.assertIsNone(apply_plan(self.env, Plan(), label="t"))
        self.assertFalse(self.state.exists())

    def test_an_edit_through_a_symlink_keeps_the_link_and_undo_restores_the_real_file(self):
        if not POSIX:
            self.skipTest("file symlinks need privileges on Windows")
        real = self.write(self.home / "dotfiles" / "cfg.json", b"{}\n", 0o600)
        link = self.home / "cfg.json"
        os.symlink(real, link)
        op_id = self.apply(self.edit(link, b'{"a": 1}\n'))
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_bytes(), b'{"a": 1}\n')
        self.revert(op_id)
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_bytes(), b"{}\n")
        self.assertEqual(self.mode(real), 0o600)

    def test_an_edit_that_changes_nothing_writes_nothing(self):
        path = self.write(self.home / "a.txt", b"same")
        with mock.patch.object(apply_module, "atomic_write") as writer:
            self.apply(self.edit(path, b"same"))
        writer.assert_not_called()

    def test_state_files_are_private_on_posix(self):
        if not POSIX:
            self.skipTest("permission bits are a POSIX concept")
        path = self.write(self.home / "a.txt", b"old", 0o644)
        op_id = self.apply(self.edit(path, b"new"))
        self.assertEqual(self.mode(self.state), 0o700)
        for item in self.state.rglob("*"):
            self.assertEqual(self.mode(item), 0o700 if item.is_dir() else 0o600, str(item))
        self.assertTrue((self.state / "ops" / op_id / "files" / "0001").is_file())

    def test_a_plan_the_engine_cannot_run_is_refused_before_anything_happens(self):
        no_inverse = RunCli(["tool", "add"], [], "add without inverse")
        with self.assertRaises(PlanError) as ctx:
            self.apply(self.edit(self.home / "a.txt", b"x"), no_inverse)
        self.assertIn("no inverse", str(ctx.exception))
        self.assertFalse((self.home / "a.txt").exists())
        self.assertFalse(self.state.exists())


class WriteAheadTests(ApplyCase):
    def test_the_entry_and_its_backup_are_on_disk_before_the_file_is_touched(self):
        path = self.write(self.home / "a.txt", b"original", 0o640)
        seen = {}
        real_write = apply_module.atomic_write

        def spy(target, data, **kw):
            if target == path:
                (op_dir,) = (self.state / "ops").iterdir()
                doc = json.loads((op_dir / "journal.json").read_text("utf-8"))
                seen["status"] = doc["entries"][0]["status"]
                seen["before"] = doc["entries"][0]["before"]
                seen["payload"] = (op_dir / "files" / "0001").read_bytes()
                seen["file_still"] = path.read_bytes()
            return real_write(target, data, **kw)

        with mock.patch.object(apply_module, "atomic_write", spy):
            self.apply(self.edit(path, b"changed"))
        self.assertEqual(seen["status"], "planned")
        self.assertEqual(seen["before"]["sha256"], sha(b"original"))
        self.assertEqual(seen["payload"], b"original")
        self.assertEqual(seen["file_still"], b"original")

    def crash_during_write(self, path, data, *, after_write):
        """Run one edit and kill it right after (or instead of) the write. Returns the op id."""
        real_write = apply_module.atomic_write

        def write(target, payload, **kw):
            if after_write:
                real_write(target, payload, **kw)
            raise Crash()

        with mock.patch.object(apply_module, "atomic_write", write), self.process_dies():
            self.apply(self.edit(path, data))
        (op_id,) = journal.find_unfinished(self.state)
        return op_id

    def test_a_crash_before_the_write_leaves_a_revertable_open_journal(self):
        path = self.write(self.home / "a.txt", b"original", 0o640)
        op_id = self.crash_during_write(path, b"changed", after_write=False)
        op = journal.load(self.state, op_id)
        self.assertEqual(op.status, "open")
        self.assertEqual(op.entries[0]["status"], "planned")
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(recover_operation(self.env, run_cli=FakeRunner()), [op_id])
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(journal.load(self.state, op_id).status, "failed")

    def test_a_crash_after_the_write_is_put_back_by_recovery(self):
        path = self.write(self.home / "a.txt", b"original", 0o640)
        op_id = self.crash_during_write(path, b"changed", after_write=True)
        self.assertEqual(path.read_bytes(), b"changed")
        self.assertEqual(self.entries(op_id)[0]["status"], "planned")
        recover_operation(self.env, run_cli=FakeRunner())
        self.assertEqual(path.read_bytes(), b"original")
        if POSIX:
            self.assertEqual(self.mode(path), 0o640)
        self.assertEqual(journal.load(self.state, op_id).data["recovered"], True)

    def test_a_crash_while_creating_a_file_in_new_folders_removes_all_of_it(self):
        path = self.home / "new" / "dir" / "b.txt"
        before = self.listing()
        op_id = self.crash_during_write(path, b"x", after_write=True)
        self.assertTrue(path.exists())
        recover_operation(self.env, run_cli=FakeRunner())
        self.assertEqual(self.listing(), before)
        self.assertTrue(op_id)

    def test_the_next_apply_refuses_while_an_operation_is_open(self):
        path = self.write(self.home / "a.txt", b"original")
        op_id = self.crash_during_write(path, b"changed", after_write=True)
        with self.assertRaises(OpenOperationError) as ctx:
            self.apply(self.edit(self.home / "other.txt", b"x"))
        self.assertEqual(ctx.exception.op_ids, [op_id])
        self.assertIn("recover_operation", str(ctx.exception))
        self.assertFalse((self.home / "other.txt").exists())
        recover_operation(self.env, run_cli=FakeRunner())
        self.apply(self.edit(self.home / "other.txt", b"x"))
        self.assertEqual((self.home / "other.txt").read_bytes(), b"x")

    def test_recovery_with_nothing_to_recover_does_nothing(self):
        self.assertEqual(recover_operation(self.env, run_cli=FakeRunner()), [])

    def test_a_crash_during_a_link_replace_is_recovered(self):
        if not fsops_can_link(self):
            self.skipTest("this machine cannot create a directory link")
        a, b = self.home / "src-a", self.home / "src-b"
        a.mkdir()
        b.mkdir()
        dest = self.home / "tool" / "skill"
        self.apply(Link(dest, a, "link", claims=(SKILL,)))
        with mock.patch.object(fsops, "create_dir_link", side_effect=Crash), self.process_dies():
            self.apply(Link(dest, b, "repoint", claims=(SKILL,)))
        self.assertFalse(os.path.lexists(dest), "the crash left the replace half done")
        recover_operation(self.env, run_cli=FakeRunner())
        self.assertTrue(fsops.same_target(fsops.read_link(dest), a))


def fsops_can_link(case):
    from engine_support import can_make_dir_link

    return can_make_dir_link(case.root)


class AutomaticRevertTests(ApplyCase):
    def test_a_failure_in_the_middle_puts_back_bytes_modes_links_dirs_and_the_ledger(self):
        existing = self.write(self.home / "a.json", b'{"keep": true}\n', 0o640)
        script = self.write(self.home / "run.sh", b"#!/bin/sh\n", 0o755)
        journal.set_owned(self.state, "example", "mcp", ["old-server"])
        can_link = fsops_can_link(self)
        src = self.home / "source" / "demo-skill"
        src.mkdir(parents=True)
        before = self.listing()
        ledger_before = (self.state / "ledger.json").read_bytes()

        failing = RunCli(["tool", "add", "boom"], ["tool", "remove", "boom"], "add boom")
        runner = FakeRunner(fail_on=[["tool", "add", "boom"]])
        ops = [
            self.edit(existing, b'{"keep": false}\n', claims=(MCP,)),
            self.edit(script, b"#!/bin/sh\necho changed\n"),
            self.edit(self.home / "new" / "deep" / "c.txt", b"new file", sensitive=True),
            RunCli(["tool", "add", "ok"], ["tool", "remove", "ok"], "add ok", claims=(INSTR,)),
        ]
        if can_link:
            ops.append(
                Link(self.home / "tool" / "skills" / "demo-skill", src, "link", claims=(SKILL,))
            )
        ops.append(failing)
        with self.assertRaises(ApplyError) as ctx:
            self.apply(*ops, runner=runner)
        error = ctx.exception
        self.assertTrue(error.reverted)
        self.assertIn("put back", str(error))
        self.assertEqual(
            self.listing(), before, "bytes, modes, links and folders must be as before"
        )
        self.assertEqual(journal.owned_items(self.state), {("example", "mcp", "old-server")})
        self.assertEqual(
            runner.calls,
            [["tool", "add", "ok"], ["tool", "add", "boom"], ["tool", "remove", "ok"]],
            "the inverse of the step that worked ran; the step that failed was not inverted",
        )
        op = journal.load(self.state, error.op_id)
        self.assertEqual((op.status, op.data["pending_revert"]), ("failed", False))
        self.assertTrue(all(e.get("reverted") for e in op.entries))
        self.assertEqual(op.entries[-1]["status"], "failed")
        self.assertEqual(journal.list_operations(self.state)[0]["status"], "failed")
        self.assertNotEqual((self.state / "ledger.json").read_bytes(), ledger_before)  # index only
        ledger = journal.read_ledger(self.state)
        self.assertEqual(ledger["owned"], {"example": {"mcp": ["old-server"]}})

    def test_an_expect_sha256_mismatch_aborts_and_reverts_the_earlier_edits(self):
        first = self.write(self.home / "first.txt", b"one")
        second = self.write(self.home / "second.txt", b"two")
        stale = self.edit(second, b"changed", expect=sha(b"what the plan saw"))
        before = self.listing()
        with self.assertRaises(ApplyError) as ctx:
            self.apply(self.edit(first, b"1"), stale)
        self.assertIn("changed since it was planned", str(ctx.exception))
        self.assertEqual(self.listing(), before)

    def test_a_file_that_must_not_exist_but_does_aborts(self):
        path = self.write(self.home / "a.txt", b"already here")
        with self.assertRaises(ApplyError):
            self.apply(EditFile(path, lambda old: b"x", None, "create a.txt"))
        self.assertEqual(path.read_bytes(), b"already here")

    def test_the_file_changing_between_the_check_and_the_write_is_caught_by_the_atomic_write(self):
        path = self.write(self.home / "a.txt", b"original")
        real_write = apply_module.atomic_write

        def racing(target, data, **kw):
            path.write_bytes(b"someone else wrote this")
            return real_write(target, data, **kw)

        with mock.patch.object(apply_module, "atomic_write", racing):
            with self.assertRaises(ApplyError) as ctx:
                self.apply(self.edit(path, b"mine"))
        self.assertTrue(ctx.exception.reverted, "a refused write changed nothing: no drift")
        self.assertEqual(path.read_bytes(), b"someone else wrote this")

    def test_a_write_that_does_not_verify_triggers_the_revert(self):
        first = self.write(self.home / "first.txt", b"one")
        second = self.write(self.home / "second.txt", b"two", 0o640)
        real_write = apply_module.atomic_write

        def bad_write(target, data, **kw):
            real_write(target, data + (b"!" if target == second else b""), **kw)

        before = self.listing()
        with mock.patch.object(apply_module, "atomic_write", bad_write):
            with self.assertRaises(ApplyError) as ctx:
                self.apply(self.edit(first, b"1"), self.edit(second, b"2"))
        self.assertIn("does not match what was written", str(ctx.exception))
        self.assertTrue(ctx.exception.reverted)
        self.assertEqual(self.listing(), before)
        entry = self.entries(ctx.exception.op_id)[1]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("expected_after", entry)

    def test_a_transform_that_raises_aborts_cleanly(self):
        path = self.write(self.home / "a.txt", b"old")

        def broken(old):
            raise ValueError("cannot parse the file")

        with self.assertRaises(ApplyError):
            self.apply(EditFile(path, broken, sha(b"old"), "edit a.txt"))
        self.assertEqual(path.read_bytes(), b"old")

    def test_a_transform_that_returns_text_instead_of_bytes_is_refused(self):
        path = self.write(self.home / "a.txt", b"old")
        with self.assertRaises(ApplyError):
            self.apply(EditFile(path, lambda old: "text", sha(b"old"), "edit a.txt"))
        self.assertEqual(path.read_bytes(), b"old")

    def test_the_ledger_is_not_changed_by_a_failed_run(self):
        journal.set_owned(self.state, "example", "mcp", ["old"])
        path = self.write(self.home / "a.txt", b"x")
        with self.assertRaises(ApplyError):
            self.apply(
                self.edit(
                    path, b"y", claims=(MCP,), releases=(ItemRef("example", Kind.MCP, "old"),)
                ),
                RunCli(["t", "x"], ["t", "y"], "boom"),
                runner=FakeRunner(fail_on=[["t", "x"]]),
            )
        self.assertEqual(journal.owned(self.state), {"example": {"mcp": ["old"]}})

    def test_an_interrupt_is_reverted_and_then_let_through(self):
        path = self.write(self.home / "a.txt", b"original")
        real_write = apply_module.atomic_write

        def interrupted(target, data, **kw):
            real_write(target, data, **kw)
            raise KeyboardInterrupt()

        with mock.patch.object(apply_module, "atomic_write", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(self.edit(path, b"changed"))
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(journal.find_unfinished(self.state), [])

    def test_when_the_revert_itself_fails_the_journal_says_so_and_recovery_finishes_it(self):
        a = self.write(self.home / "a.txt", b"a-old")
        b = self.write(self.home / "b.txt", b"b-old")
        runner = FakeRunner(fail_on=[["t", "boom"]])
        real_restore = backup_module.restore
        broken = {"on": True}

        def restore(state_dir, op_id, path, state):
            if broken["on"] and path == a:
                raise OSError("disk went away")
            return real_restore(state_dir, op_id, path, state)

        with mock.patch("one_setup_all_agents.engine.backup.restore", restore):
            with self.assertRaises(ApplyError) as ctx:
                self.apply(
                    self.edit(a, b"a-new"),
                    self.edit(b, b"b-new"),
                    RunCli(["t", "boom"], ["t", "unboom"], "boom"),
                    runner=runner,
                )
        error = ctx.exception
        self.assertFalse(error.reverted)
        self.assertIn("did not finish", str(error))
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b"a-new", b"b-old"))
        self.assertEqual(journal.find_unfinished(self.state), [error.op_id])
        with self.assertRaises(OpenOperationError):
            self.apply(self.edit(self.home / "c.txt", b"c"))
        broken["on"] = False
        self.assertEqual(recover_operation(self.env, run_cli=FakeRunner()), [error.op_id])
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b"a-old", b"b-old"))
        self.assertEqual(journal.find_unfinished(self.state), [])

    def test_no_secret_reaches_an_error_message_or_the_output(self):
        secret = "FAKE-ARG-SECRET-bbb222"
        failing = RunCli(
            ["tool", "add", "--token", secret], ["tool", "remove"], f"add with token={secret}"
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(ApplyError) as ctx:
                self.apply(failing, runner=FakeRunner(fail_on=[failing.argv]))
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn(secret, str(ctx.exception.__cause__))
        self.assertNotIn(secret, out.getvalue() + err.getvalue())
        entry = self.entries(ctx.exception.op_id)[0]
        self.assertNotIn(secret, entry["description"])
        self.assertNotIn(secret, entry["error"])


class LockInteractionTests(ApplyCase):
    def test_a_second_apply_while_the_lock_is_held_fails_and_changes_nothing(self):
        path = self.write(self.home / "a.txt", b"original")
        with engine_lock(self.state):
            with self.assertRaises(EngineLockedError):
                self.apply(self.edit(path, b"changed"))
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(journal.list_operations(self.state), [])

    def test_revert_also_takes_the_lock(self):
        path = self.write(self.home / "a.txt", b"original")
        op_id = self.apply(self.edit(path, b"changed"))
        with engine_lock(self.state):
            with self.assertRaises(EngineLockedError):
                self.revert(op_id)
        self.assertEqual(path.read_bytes(), b"changed")

    def test_the_lock_is_released_after_a_failed_run(self):
        with self.assertRaises(ApplyError):
            self.apply(
                RunCli(["t", "x"], ["t", "y"], "boom"), runner=FakeRunner(fail_on=[["t", "x"]])
            )
        self.apply(self.edit(self.home / "a.txt", b"x"))


class RevertTests(ApplyCase):
    def test_a_done_operation_is_undone_exactly_and_marked_undone(self):
        a = self.write(self.home / "a.txt", b"old", 0o640)
        before = self.listing()
        op_id = self.apply(
            self.edit(a, b"new", claims=(MCP,)),
            self.edit(self.home / "x" / "y" / "z.txt", b"made", claims=(INSTR,)),
        )
        report = self.revert(op_id)
        self.assertEqual(sorted(report.reverted), [1, 2])
        self.assertEqual(self.listing(), before)
        self.assertEqual(journal.load(self.state, op_id).status, "undone")
        self.assertEqual(journal.owned(self.state), {})
        self.assertEqual(journal.list_operations(self.state)[0]["status"], "undone")

    def test_ownership_is_restored_including_releases(self):
        journal.set_owned(self.state, "example", "mcp", ["old-server"])
        path = self.write(self.home / "a.txt", b"x")
        op_id = self.apply(
            self.edit(
                path,
                b"y",
                claims=(MCP,),
                releases=(ItemRef("example", Kind.MCP, "old-server"),),
            )
        )
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"demo-server"})
        self.revert(op_id)
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"old-server"})

    def test_an_item_that_was_already_owned_stays_owned_after_undoing_a_claim_on_it(self):
        path = self.write(self.home / "a.txt", b"x")
        self.apply(self.edit(path, b"y", claims=(MCP,)))
        second = self.apply(self.edit(path, b"z", claims=(MCP,)))
        self.revert(second)
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"demo-server"})

    def test_a_drifted_path_refuses_the_whole_revert_and_changes_nothing(self):
        a = self.write(self.home / "a.txt", b"a-old")
        b = self.write(self.home / "b.txt", b"b-old")
        op_id = self.apply(self.edit(a, b"a-new", claims=(MCP,)), self.edit(b, b"b-new"))
        b.write_bytes(b"the tool changed this meanwhile")
        status_before = journal.load(self.state, op_id).data
        with self.assertRaises(RevertError) as ctx:
            self.revert(op_id)
        self.assertEqual(ctx.exception.drifted, [b])
        self.assertFalse(ctx.exception.partial)
        self.assertIn(str(b), str(ctx.exception))
        self.assertEqual(
            a.read_bytes(), b"a-new", "the path that did not drift must not be touched"
        )
        self.assertEqual(b.read_bytes(), b"the tool changed this meanwhile")
        self.assertEqual(journal.load(self.state, op_id).data, status_before)
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"demo-server"})

    def test_a_deleted_path_counts_as_drift(self):
        a = self.write(self.home / "a.txt", b"old")
        op_id = self.apply(self.edit(a, b"new"))
        a.unlink()
        with self.assertRaises(RevertError) as ctx:
            self.revert(op_id)
        self.assertEqual(ctx.exception.drifted, [a])
        self.assertFalse(a.exists())

    def test_revert_with_force_restores_drifted_modified_deleted_and_repointed_link(self):
        a = self.write(self.home / "a.txt", b"a-old")
        target1 = self.home / "target1_dir"
        target1.mkdir()
        target2 = self.home / "target2_dir"
        target2.mkdir()
        link = self.home / "my_link_dir"

        op_id = self.apply(
            self.edit(a, b"a-new"),
            Link(link, target1, "my link"),
        )

        # 1. Drift: modify a.txt, repoint link to target2
        a.write_bytes(b"a-tampered")
        fsops.remove_link(link)
        fsops.create_dir_link(link, target2)

        # Without force: refused
        with self.assertRaises(RevertError) as ctx:
            self.revert(op_id)
        self.assertIn(a, ctx.exception.drifted)
        self.assertIn(link, ctx.exception.drifted)

        # With force: restores both
        report = self.revert(op_id, force=True)
        self.assertIn(1, report.reverted)
        self.assertEqual(a.read_bytes(), b"a-old")
        self.assertFalse(link.exists())

        # 2. Test deleted file restored with force
        op_id2 = self.apply(self.edit(a, b"a-newer"))
        a.unlink()
        with self.assertRaises(RevertError):
            self.revert(op_id2)
        self.revert(op_id2, force=True)
        self.assertTrue(a.exists())
        self.assertEqual(a.read_bytes(), b"a-old")

    def test_apply_plan_prunes_journal_keeping_last_20_operations(self):
        from datetime import datetime, timezone

        for i in range(25):
            t = datetime(2026, 11, 6, 10, i, 0, tzinfo=timezone.utc)
            op = journal.begin(self.state, f"dummy-{i}", now=t)
            op.set_status(journal.DONE)

        a = self.write(self.home / "apply_prune.txt", b"start")
        self.apply(self.edit(a, b"finish"))

        # After apply, pruning kept at most 20 operations
        ops = journal.list_operations(self.state)
        self.assertEqual(len(ops), 20)

    def test_a_permission_change_counts_as_drift_on_posix(self):
        if not POSIX:
            self.skipTest("permission bits are a POSIX concept")
        a = self.write(self.home / "a.txt", b"old", 0o600)
        op_id = self.apply(self.edit(a, b"new"))
        os.chmod(a, 0o644)
        with self.assertRaises(RevertError):
            self.revert(op_id)

    def test_a_second_undo_is_refused(self):
        a = self.write(self.home / "a.txt", b"old")
        op_id = self.apply(self.edit(a, b"new"))
        self.revert(op_id)
        with self.assertRaises(RevertError) as ctx:
            self.revert(op_id)
        self.assertIn("undone", str(ctx.exception))

    def test_an_unknown_operation_is_a_clear_error(self):
        from one_setup_all_agents.engine.errors import JournalError

        with self.assertRaises(JournalError):
            self.revert("20260101T000000Z-0000")


class RunCliTests(ApplyCase):
    def test_the_command_runs_through_the_runner_and_its_inverse_runs_on_revert(self):
        runner = FakeRunner()
        op = RunCli(
            ["tool", "mcp", "add", "demo-server"],
            ["tool", "mcp", "remove", "demo-server"],
            "add demo-server",
            claims=(MCP,),
        )
        op_id = self.apply(op, runner=runner)
        self.assertEqual(runner.calls, [["tool", "mcp", "add", "demo-server"]])
        entry = self.entries(op_id)[0]
        self.assertEqual((entry["type"], entry["status"]), ("run_cli", "done"))
        self.assertEqual(entry["result"], {"exit_code": 0})
        self.assertEqual(entry["inverse_argv"], ["tool", "mcp", "remove", "demo-server"])
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"demo-server"})
        self.revert(op_id, runner=runner)
        self.assertEqual(runner.calls[-1], ["tool", "mcp", "remove", "demo-server"])
        self.assertEqual(journal.owned(self.state), {})

    def test_inverses_run_newest_first(self):
        runner = FakeRunner()
        ops = [RunCli(["t", str(i)], ["t", f"un{i}"], f"step {i}") for i in range(3)]
        op_id = self.apply(*ops, runner=runner)
        runner.calls.clear()
        self.revert(op_id, runner=runner)
        self.assertEqual(runner.calls, [["t", "un2"], ["t", "un1"], ["t", "un0"]])

    def test_a_non_zero_exit_is_a_failed_verification(self):
        runner = FakeRunner(fail_on=[["t", "x"]])
        with self.assertRaises(ApplyError) as ctx:
            self.apply(RunCli(["t", "x"], ["t", "y"], "x"), runner=runner)
        self.assertIn("exited with code 1", str(ctx.exception.__cause__))
        self.assertEqual(runner.calls, [["t", "x"]], "a failed command is not inverted")

    def test_a_runner_that_raises_is_a_failure_and_the_inverse_is_tried_anyway(self):
        runner = FakeRunner(raise_on=["x"])
        with self.assertRaises(ApplyError) as ctx:
            self.apply(RunCli(["t", "x"], ["t", "y"], "x"), runner=runner)
        self.assertTrue(ctx.exception.reverted)
        self.assertEqual(runner.calls, [["t", "x"], ["t", "y"]])

    def test_remove_then_add_where_the_add_fails_restores_the_old_entry(self):
        remove = RunCli(
            ["tool", "remove", "demo-server"],
            ["tool", "add", "demo-server", "OLD"],
            "remove old entry",
            releases=(MCP,),
        )
        add = RunCli(
            ["tool", "add", "demo-server", "NEW"],
            ["tool", "remove", "demo-server"],
            "add new entry",
            claims=(MCP,),
        )
        journal.set_owned(self.state, "example", "mcp", ["demo-server"])
        runner = FakeRunner(fail_on=[["tool", "add", "demo-server", "NEW"]])
        with self.assertRaises(ApplyError) as ctx:
            self.apply(remove, add, runner=runner)
        self.assertTrue(ctx.exception.reverted)
        self.assertEqual(
            runner.calls,
            [
                ["tool", "remove", "demo-server"],
                ["tool", "add", "demo-server", "NEW"],
                ["tool", "add", "demo-server", "OLD"],
            ],
            "the failed add is not inverted; the remove is, which brings the old entry back",
        )
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"demo-server"})

    def test_a_planned_command_after_a_crash_is_inverted_and_a_failing_inverse_is_tolerated(self):
        op = RunCli(["t", "x"], ["t", "undo-x"], "x")
        runner = FakeRunner(fail_on=[["t", "undo-x"]])
        with self.process_dies():
            self.apply(op, runner=mock.Mock(side_effect=Crash))
        (op_id,) = journal.find_unfinished(self.state)
        self.assertEqual(self.entries(op_id)[0]["status"], "planned")
        report = recover_operation_report(self, runner)
        self.assertEqual(runner.calls, [["t", "undo-x"]])
        self.assertEqual(report.uncertain, [1])
        self.assertIn("may never have run", self.entries(op_id)[0]["revert_note"])

    def test_an_inverse_that_fails_for_a_done_command_stops_the_revert_and_resumes_later(self):
        runner = FakeRunner()
        file_ = self.write(self.home / "a.txt", b"old")
        op_id = self.apply(
            self.edit(file_, b"new"),
            RunCli(["t", "x"], ["t", "undo-x"], "x"),
            runner=runner,
        )
        failing = FakeRunner(fail_on=[["t", "undo-x"]])
        with self.assertRaises(RevertError) as ctx:
            self.revert(op_id, runner=failing)
        self.assertIn("exited with code 1", str(ctx.exception))
        self.assertEqual(file_.read_bytes(), b"new")  # newest first: the CLI came first, it failed
        self.revert(op_id, runner=FakeRunner())
        self.assertEqual(file_.read_bytes(), b"old")
        self.assertEqual(journal.load(self.state, op_id).status, "undone")

    def test_the_default_runner_uses_an_argv_list_and_never_a_shell(self):
        from one_setup_all_agents.engine import runcli

        with mock.patch.object(runcli.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            code = runcli.default_runner()(["demo-tool", "arg with space", "--x=1"])
        self.assertEqual(code, 0)
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0][1:], ["arg with space", "--x=1"])
        self.assertIs(kwargs["shell"], False)
        self.assertTrue(kwargs["capture_output"])

    def test_the_default_runner_reports_a_missing_program_as_127(self):
        from one_setup_all_agents.engine import runcli

        self.assertEqual(runcli.default_runner()(["definitely-not-a-real-program-xyz"]), 127)

    def test_no_test_in_this_module_starts_a_real_tool(self):
        # the default runner is only reached when `run_cli` is None; every call above passes one
        with mock.patch.object(apply_module, "default_runner") as default:
            self.apply(self.edit(self.home / "a.txt", b"x"), runner=FakeRunner())
        default.assert_not_called()


def recover_operation_report(case, runner):
    """Like recover_operation, but return the RevertReport of the one open operation."""
    from one_setup_all_agents.engine.revert import revert_operation as revert

    (op_id,) = journal.find_unfinished(case.state)
    return revert(case.env, op_id, run_cli=runner)


class AdoptionCarrierTests(ApplyCase):
    def test_adoption_only_plan_creates_done_operation_with_delta(self):
        op_id = apply_plan(
            self.env,
            Plan([]),
            label="test_adopt",
            adopt=(MCP, SKILL),
            run_cli=FakeRunner(),
        )
        self.assertIsNotNone(op_id)
        op = journal.load(self.state, op_id)
        self.assertEqual(op.status, journal.DONE)
        self.assertEqual(len(op.entries), 0)
        delta = op.data.get("owned_delta", {})
        added = delta.get("added", [])
        expected_added = sorted(
            [
                [MCP.tool, MCP.kind.value, MCP.key],
                [SKILL.tool, SKILL.kind.value, SKILL.key],
            ]
        )
        self.assertEqual(sorted(added), expected_added)
        owned = journal.owned_items(self.state)
        self.assertIn((MCP.tool, MCP.kind.value, MCP.key), owned)
        self.assertIn((SKILL.tool, SKILL.kind.value, SKILL.key), owned)

    def test_revert_removes_adopted_items(self):
        op_id = apply_plan(
            self.env,
            Plan([]),
            label="test_adopt",
            adopt=(MCP,),
            run_cli=FakeRunner(),
        )
        self.assertIn((MCP.tool, MCP.kind.value, MCP.key), journal.owned_items(self.state))
        report = revert_operation(self.env, op_id, run_cli=FakeRunner())
        self.assertEqual(report.op_id, op_id)
        self.assertNotIn((MCP.tool, MCP.kind.value, MCP.key), journal.owned_items(self.state))
        op = journal.load(self.state, op_id)
        self.assertEqual(op.status, journal.UNDONE)

    def test_already_owned_item_not_duplicated_and_survives_revert(self):
        journal.set_owned(self.state, MCP.tool, MCP.kind.value, [MCP.key])
        self.assertIn((MCP.tool, MCP.kind.value, MCP.key), journal.owned_items(self.state))

        op_id = apply_plan(
            self.env,
            Plan([]),
            label="test_adopt_mixed",
            adopt=(MCP, SKILL),
            run_cli=FakeRunner(),
        )
        op = journal.load(self.state, op_id)
        added = op.data.get("owned_delta", {}).get("added", [])
        self.assertEqual(added, [[SKILL.tool, SKILL.kind.value, SKILL.key]])

        revert_operation(self.env, op_id, run_cli=FakeRunner())
        owned = journal.owned_items(self.state)
        self.assertIn((MCP.tool, MCP.kind.value, MCP.key), owned)
        self.assertNotIn((SKILL.tool, SKILL.kind.value, SKILL.key), owned)

    def test_failing_op_in_plan_reverts_adoption_too(self):
        with self.assertRaises(ApplyError):
            apply_plan(
                self.env,
                Plan([self.edit(self.home / "fail.txt", b"new", expect="wrong_hash")]),
                label="test_fail",
                adopt=(MCP,),
                run_cli=FakeRunner(),
            )
        owned = journal.owned_items(self.state)
        self.assertNotIn((MCP.tool, MCP.kind.value, MCP.key), owned)

    @unittest.skipUnless(sys.platform != "win32", "permission bits are a POSIX concept")
    def test_edit_file_honors_mode_for_new_file(self):
        dest = self.home / "run.sh"
        op = EditFile(
            path=dest,
            transform=lambda _old: b"#!/bin/sh\n",
            expect_sha256=None,
            description="create script",
            mode=0o755,
        )
        apply_plan(self.env, Plan([op]), label="mode_test", run_cli=FakeRunner())
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o755)

    def test_edit_file_mode_accepted_on_all_platforms(self):
        dest = self.home / "any.sh"
        op = EditFile(
            path=dest,
            transform=lambda _old: b"hello",
            expect_sha256=None,
            description="create script",
            mode=0o755,
        )
        apply_plan(self.env, Plan([op]), label="mode_test", run_cli=FakeRunner())
        self.assertEqual(dest.read_bytes(), b"hello")


if __name__ == "__main__":
    unittest.main()
