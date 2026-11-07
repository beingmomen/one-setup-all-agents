"""Link execution: junction/symlink, copy fallback and Link(target=None) removal (ADR 0007, 0009, 0010)."""

import os
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import POSIX, WINDOWS, EngineCase, can_make_dir_link
from one_setup_all_agents.engine import fsops, journal
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.errors import ApplyError
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import ItemRef, Kind, Link, Plan

REF = ItemRef("example", Kind.SKILLS, "demo-skill")
OTHER = ItemRef("example", Kind.SKILLS, "other-skill")


class LinkCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.src = self.home / "source" / "demo-skill"
        self.write(self.src / "SKILL.md", b"demo skill\n")
        self.write(self.src / "scripts" / "run.sh", b"#!/bin/sh\n", 0o755)
        self.dest = self.home / "tool" / "skills" / "demo-skill"

    def apply(self, *ops, label="test"):
        return apply_plan(self.env, Plan(list(ops)), label=label)

    def link(self, target=None, **kw):
        kw.setdefault("claims", (REF,))
        return Link(self.dest, target or self.src, "link demo-skill", **kw)

    def remove(self, **kw):
        kw.setdefault("releases", (REF,))
        return Link(self.dest, None, "withdraw demo-skill", **kw)


class NativeLinkTests(LinkCase):
    def setUp(self):
        super().setUp()
        if not can_make_dir_link(self.root):
            self.skipTest("this machine cannot create a directory link")

    def test_link_round_trip_creates_the_native_kind_and_undo_removes_it(self):
        before = self.listing()
        op_id = self.apply(self.link())
        self.assertTrue(fsops.same_target(fsops.read_link(self.dest), self.src))
        self.assertEqual(fsops.link_kind(self.dest), "junction" if WINDOWS else "symlink")
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), b"demo skill\n")
        revert_operation(self.env, op_id)
        self.assertEqual(self.listing(), before)
        self.assertTrue((self.src / "SKILL.md").exists(), "undo must never touch the target")

    @unittest.skipUnless(WINDOWS, "junctions are Windows only")
    def test_windows_uses_a_junction_without_administrator_rights(self):
        self.apply(self.link())
        self.assertTrue(fsops.is_junction(self.dest))
        self.assertFalse(os.path.islink(self.dest), "a junction is not a symlink")

    @unittest.skipUnless(WINDOWS, "junctions are Windows only")
    def test_the_mklink_junction_method_makes_the_same_kind_of_link(self):
        link = self.home / "via-mklink"
        fsops.create_junction_mklink(link, self.src)
        self.assertTrue(fsops.is_junction(link))
        self.assertTrue(fsops.same_target(fsops.read_link(link), self.src))
        fsops.remove_link(link)
        self.assertTrue(self.src.exists())

    @unittest.skipUnless(WINDOWS, "junctions are Windows only")
    def test_create_junction_falls_back_to_mklink_when_winapi_has_no_junction_call(self):
        import _winapi

        link = self.home / "via-fallback"
        with mock.patch.object(_winapi, "CreateJunction", create=True, new=None):
            del _winapi.CreateJunction  # the attribute is gone for this call
            with mock.patch.object(fsops, "create_junction_mklink") as mklink:
                fsops.create_junction(link, self.src)
        mklink.assert_called_once()

    @unittest.skipUnless(POSIX, "symlinks are the POSIX method")
    def test_posix_makes_a_symlink_with_an_absolute_target(self):
        self.apply(self.link())
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(os.readlink(self.dest), str(self.src))

    def test_a_repoint_of_an_owned_link_is_undone_to_the_old_target(self):
        other = self.home / "source" / "demo-skill-v2"
        self.write(other / "SKILL.md", b"v2\n")
        first = self.apply(self.link())
        second = self.apply(self.link(target=other))
        self.assertTrue(fsops.same_target(fsops.read_link(self.dest), other))
        revert_operation(self.env, second)
        self.assertTrue(fsops.same_target(fsops.read_link(self.dest), self.src))
        revert_operation(self.env, first)
        self.assertFalse(os.path.lexists(self.dest))

    def test_creating_a_link_that_already_exists_changes_nothing(self):
        self.apply(self.link())
        op_id = self.apply(self.link())
        entry = journal.load(self.state, op_id).entries[0]
        self.assertTrue(fsops.same_target(fsops.read_link(self.dest), self.src))
        self.assertEqual(entry["before"]["state"], "dir_link")
        self.assertEqual(entry["before"], {**entry["after"]})

    def test_the_target_must_be_an_existing_folder(self):
        with self.assertRaises(ApplyError) as ctx:
            self.apply(self.link(target=self.home / "no-such-folder"))
        self.assertIn("not an existing folder", str(ctx.exception.__cause__))
        self.assertFalse(os.path.lexists(self.dest))


class RemovalTests(LinkCase):
    def setUp(self):
        super().setUp()
        if not can_make_dir_link(self.root):
            self.skipTest("this machine cannot create a directory link")

    def test_an_owned_link_is_removed_and_undo_puts_it_back(self):
        self.apply(self.link())
        before = self.listing()
        op_id = self.apply(self.remove())
        self.assertFalse(os.path.lexists(self.dest))
        self.assertTrue((self.src / "SKILL.md").exists())
        self.assertEqual(journal.owned_for(self.state, "example", "skills"), frozenset())
        revert_operation(self.env, op_id)
        self.assertEqual(self.listing(), before)
        self.assertEqual(journal.owned_for(self.state, "example", "skills"), {"demo-skill"})

    def test_a_link_the_ledger_does_not_own_is_not_removed(self):
        fsops.make_dirs(self.dest.parent)
        fsops.create_dir_link(self.dest, self.src)  # the user's own link
        before = self.listing()
        with self.assertRaises(ApplyError) as ctx:
            self.apply(self.remove())
        self.assertIn("does not record it as owned", str(ctx.exception.__cause__))
        self.assertEqual(self.listing(), before)

    def test_a_removal_that_names_no_released_item_is_refused(self):
        self.apply(self.link())
        before = self.listing()
        with self.assertRaises(ApplyError) as ctx:
            self.apply(self.remove(releases=()))
        self.assertIn("does not say which owned item", str(ctx.exception.__cause__))
        self.assertEqual(self.listing(), before)

    def test_releasing_another_item_than_the_one_owned_is_refused(self):
        self.apply(self.link())
        with self.assertRaises(ApplyError):
            self.apply(self.remove(releases=(OTHER,)))
        self.assertTrue(os.path.lexists(self.dest))

    def test_a_real_folder_is_never_removed_or_replaced(self):
        self.write(self.dest / "SKILL.md", b"the user's own skill\n")
        journal.set_owned(self.state, "example", "skills", ["demo-skill"])  # even if "owned"
        before = self.listing()
        for op in (self.remove(), self.link()):
            with self.assertRaises(ApplyError):
                self.apply(op)
            self.assertEqual(self.listing(), before)

    def test_a_regular_file_is_never_replaced_by_a_link(self):
        self.write(self.dest, b"a file where the link should go")
        before = self.listing()
        with self.assertRaises(ApplyError) as ctx:
            self.apply(self.link())
        self.assertIn("regular file", str(ctx.exception.__cause__))
        self.assertEqual(self.listing(), before)

    def test_removing_something_already_gone_is_a_harmless_no_op(self):
        journal.set_owned(self.state, "example", "skills", ["demo-skill"])
        op_id = self.apply(self.remove())
        self.assertEqual(journal.load(self.state, op_id).entries[0]["before"], {"state": "absent"})
        self.assertEqual(journal.owned_for(self.state, "example", "skills"), frozenset())


class CopyFallbackTests(LinkCase):
    def fail_linking(self):
        return mock.patch.object(fsops, "create_dir_link", side_effect=OSError("no links here"))

    def test_without_the_flag_a_link_failure_is_an_error_and_leaves_nothing(self):
        before = self.listing()
        with self.fail_linking(), self.assertRaises(ApplyError) as ctx:
            self.apply(self.link())
        self.assertIn("cannot link", str(ctx.exception.__cause__))
        self.assertEqual(self.listing(), before)

    def test_with_the_flag_the_folder_is_copied_with_a_marker_and_undo_removes_it(self):
        before = self.listing()
        with self.fail_linking():
            op_id = self.apply(self.link(allow_copy_fallback=True))
        self.assertFalse(os.path.islink(self.dest) or fsops.is_junction(self.dest))
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), b"demo skill\n")
        marker = fsops.read_marker(self.dest)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["tree_sha256"], fsops.tree_hash(self.src))
        self.assertEqual(marker["tree_sha256"], fsops.tree_hash(self.dest))
        entry = journal.load(self.state, op_id).entries[0]
        self.assertEqual(entry["after"]["state"], "copy_tree")
        self.assertEqual(entry["status"], "done")
        revert_operation(self.env, op_id)
        self.assertEqual(self.listing(), before)
        self.assertEqual([p for p in self.dest.parent.parent.rglob(fsops._TMP_PREFIX + "*")], [])

    def test_a_marked_copy_can_be_refreshed_removed_and_restored(self):
        with self.fail_linking():
            self.apply(self.link(allow_copy_fallback=True))
        before_removal = self.listing()
        removal = self.apply(self.remove())
        self.assertFalse(os.path.lexists(self.dest))
        revert_operation(self.env, removal)
        self.assertEqual(self.listing(), before_removal)
        self.assertIsNotNone(fsops.read_marker(self.dest))
        # the source changed: linking again replaces the old copy (the old copy is backed up)
        self.write(self.src / "SKILL.md", b"new text\n")
        with self.fail_linking():
            refresh = self.apply(self.link(allow_copy_fallback=True))
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), b"new text\n")
        revert_operation(self.env, refresh)
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), b"demo skill\n")

    def test_a_copy_that_was_edited_afterwards_is_not_discarded(self):
        with self.fail_linking():
            self.apply(self.link(allow_copy_fallback=True))
        self.write(self.dest / "SKILL.md", b"edited by the user\n")
        with self.assertRaises(ApplyError) as ctx:
            self.apply(self.remove())
        self.assertIn("edited after", str(ctx.exception.__cause__))
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), b"edited by the user\n")

    def test_undo_refuses_to_delete_a_copy_that_changed_since(self):
        with self.fail_linking():
            op_id = self.apply(self.link(allow_copy_fallback=True))
        self.write(self.dest / "SKILL.md", b"edited by the user\n")
        from one_setup_all_agents.engine.errors import RevertError

        with self.assertRaises(RevertError) as ctx:
            revert_operation(self.env, op_id)
        self.assertEqual(ctx.exception.drifted, [self.dest])
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), b"edited by the user\n")

    def test_a_failed_copy_leaves_no_half_folder(self):
        before = self.listing()
        with (
            self.fail_linking(),
            mock.patch.object(fsops, "rename_dir", side_effect=OSError("disk full")),
            self.assertRaises(ApplyError),
        ):
            self.apply(self.link(allow_copy_fallback=True))
        self.assertEqual(self.listing(), before)

    def test_a_source_with_a_link_inside_cannot_be_copied(self):
        if not can_make_dir_link(self.root):
            self.skipTest("this machine cannot create a directory link")
        fsops.create_dir_link(self.src / "inner", self.home)
        before = self.listing()
        with self.fail_linking(), self.assertRaises(ApplyError):
            self.apply(self.link(allow_copy_fallback=True))
        self.assertEqual(self.listing(), before)


if __name__ == "__main__":
    unittest.main()
