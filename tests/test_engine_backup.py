"""engine/backup.py and engine/fsops.py: capture and restore every kind of before-state (ADR 0005)."""

import os
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import POSIX, WINDOWS, EngineCase, can_make_dir_link
from one_setup_all_agents.engine import backup, fsops, journal
from one_setup_all_agents.engine.errors import BackupError

OP = "20261106T101500Z-a1b2"


class CaptureRestoreTests(EngineCase):
    def setUp(self):
        super().setUp()
        journal.begin(self.state, "test")  # makes the state layout; its own id is not used below
        fsops.ensure_private_dir(journal.op_dir(self.state, OP))

    def test_absent_path_is_described_and_restored_as_absent(self):
        path = self.home / "missing.txt"
        state = backup.capture(self.state, OP, path, 1)
        self.assertEqual(state, {"state": "absent"})
        path.write_bytes(b"made later")
        backup.restore(self.state, OP, path, state)
        self.assertFalse(path.exists())

    def test_file_bytes_and_hash_and_mode_are_captured_and_restored(self):
        path = self.write(self.home / "a.txt", b"original", 0o640)
        state = backup.capture(self.state, OP, path, 1)
        self.assertEqual(state["state"], "file")
        self.assertEqual(state["backup"], "files/0001")
        self.assertEqual(len(state["sha256"]), 64)
        self.assertEqual(
            backup.payload_path(self.state, OP, state["backup"]).read_bytes(), b"original"
        )
        path.write_bytes(b"changed")
        if POSIX:
            os.chmod(path, 0o600)
        backup.restore(self.state, OP, path, state)
        self.assertEqual(path.read_bytes(), b"original")
        if POSIX:
            self.assertEqual(self.mode(path), 0o640)

    def test_a_deleted_file_comes_back_with_its_mode(self):
        path = self.write(self.home / "gone.txt", b"keep me", 0o600)
        state = backup.capture(self.state, OP, path, 1)
        path.unlink()
        backup.restore(self.state, OP, path, state)
        self.assertEqual(path.read_bytes(), b"keep me")
        if POSIX:
            self.assertEqual(self.mode(path), 0o600)

    def test_a_damaged_payload_is_refused_and_its_content_is_not_printed(self):
        path = self.write(self.home / "a.txt", b"SECRET-LOOKING-CONTENT-123")
        state = backup.capture(self.state, OP, path, 1)
        backup.payload_path(self.state, OP, state["backup"]).write_bytes(b"tampered")
        path.write_bytes(b"other")
        with self.assertRaises(BackupError) as ctx:
            backup.restore(self.state, OP, path, state)
        self.assertNotIn("SECRET-LOOKING-CONTENT", str(ctx.exception))
        self.assertNotIn("tampered", str(ctx.exception))
        self.assertEqual(path.read_bytes(), b"other")

    def test_payloads_and_state_folders_are_private_on_posix(self):
        if not POSIX:
            self.skipTest("permission bits are a POSIX concept")
        path = self.write(self.home / "a.txt", b"x", 0o644)
        state = backup.capture(self.state, OP, path, 1)
        payload = backup.payload_path(self.state, OP, state["backup"])
        self.assertEqual(self.mode(payload), 0o600)
        self.assertEqual(self.mode(payload.parent), 0o700)
        self.assertEqual(self.mode(self.state), 0o700)

    def test_a_real_folder_without_a_marker_is_never_captured(self):
        folder = self.home / "skills" / "mine"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("the user's own", "utf-8")
        with self.assertRaises(BackupError):
            backup.describe(folder)
        with self.assertRaises(BackupError):
            backup.restore(self.state, OP, folder, {"state": "absent"})
        self.assertTrue((folder / "SKILL.md").exists())

    def test_symlink_to_a_file_round_trip(self):
        if not POSIX:
            self.skipTest("file symlinks need privileges on Windows")
        real = self.write(self.home / "real.txt", b"x")
        link = self.home / "link.txt"
        os.symlink(real, link)
        state = backup.capture(self.state, OP, link, 1)
        self.assertEqual(state["state"], "symlink")
        link.unlink()
        backup.restore(self.state, OP, link, state)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(real))

    def test_directory_link_round_trip(self):
        if not can_make_dir_link(self.root):
            self.skipTest("this machine cannot create a directory link")
        target = self.home / "target"
        target.mkdir()
        link = self.home / "link"
        fsops.create_dir_link(link, target)
        state = backup.capture(self.state, OP, link, 1)
        self.assertEqual(state["state"], "dir_link")
        self.assertEqual(state["kind"], "junction" if WINDOWS else "symlink")
        self.assertTrue(fsops.same_target(state["target"], target))
        fsops.remove_link(link)
        self.assertTrue(target.exists(), "removing a link must not remove its target")
        backup.restore(self.state, OP, link, state)
        self.assertEqual(backup.describe(link)["kind"], state["kind"])
        self.assertTrue(fsops.same_target(fsops.read_link(link), target))

    def test_copy_tree_round_trip_with_content_and_modes(self):
        folder = self.home / "copy"
        (folder / "sub").mkdir(parents=True)
        self.write(folder / "SKILL.md", b"skill text")
        self.write(folder / "sub" / "run.sh", b"#!/bin/sh\n", 0o755)
        digest = fsops.tree_hash(folder)
        (folder / fsops.MARKER_NAME).write_bytes(fsops.marker_bytes(self.home / "src", digest))
        state = backup.capture(self.state, OP, folder, 1)
        self.assertEqual(state["state"], "copy_tree")
        self.assertEqual(state["tree_sha256"], digest)
        self.assertEqual(state["marker"]["tree_sha256"], digest)
        fsops.remove_tree(folder)
        backup.restore(self.state, OP, folder, state)
        self.assertEqual(fsops.tree_hash(folder), digest)
        self.assertEqual((folder / "sub" / "run.sh").read_bytes(), b"#!/bin/sh\n")
        self.assertIsNotNone(fsops.read_marker(folder))
        if POSIX:
            self.assertEqual(self.mode(folder / "sub" / "run.sh"), 0o755)

    def test_restore_does_nothing_when_the_path_already_matches(self):
        path = self.write(self.home / "a.txt", b"same", 0o644)
        state = backup.capture(self.state, OP, path, 1)
        with mock.patch.object(backup, "atomic_write") as writer:
            backup.restore(self.state, OP, path, state)
        writer.assert_not_called()


class StateMatchingTests(EngineCase):
    def test_file_mode_is_compared_on_posix_only_and_only_when_both_known(self):
        a = {"state": "file", "sha256": "x", "mode": 0o600}
        b = {"state": "file", "sha256": "x", "mode": 0o644}
        unknown = {"state": "file", "sha256": "x", "mode": None}
        self.assertEqual(backup.states_match(a, b), WINDOWS)
        self.assertTrue(backup.states_match(a, unknown))
        self.assertFalse(backup.states_match(a, {"state": "file", "sha256": "y", "mode": 0o600}))

    def test_kinds_never_match_each_other(self):
        self.assertFalse(
            backup.states_match({"state": "absent"}, {"state": "symlink", "target": "t"})
        )
        self.assertTrue(backup.states_match({"state": "absent"}, {"state": "absent"}))

    def test_a_junction_and_a_symlink_to_the_same_place_differ(self):
        j = {"state": "dir_link", "kind": "junction", "target": "t"}
        s = {"state": "dir_link", "kind": "symlink", "target": "t"}
        self.assertFalse(backup.states_match(j, s))


class TreeHashTests(EngineCase):
    def test_hash_depends_on_names_and_bytes_but_not_on_the_marker(self):
        one, two = self.home / "one", self.home / "two"
        for folder in (one, two):
            (folder / "d").mkdir(parents=True)
            self.write(folder / "d" / "f.txt", b"same")
        self.assertEqual(fsops.tree_hash(one), fsops.tree_hash(two))
        (one / fsops.MARKER_NAME).write_bytes(b"{}")
        self.assertEqual(fsops.tree_hash(one), fsops.tree_hash(two))
        (two / "d" / "f.txt").write_bytes(b"other")
        self.assertNotEqual(fsops.tree_hash(one), fsops.tree_hash(two))
        (two / "d" / "f.txt").write_bytes(b"same")
        (two / "d" / "extra").mkdir()
        self.assertNotEqual(fsops.tree_hash(one), fsops.tree_hash(two))

    def test_the_cost_guard_refuses_a_big_tree_before_copying(self):
        folder = self.home / "big"
        folder.mkdir()
        for i in range(4):
            self.write(folder / f"{i}.txt", b"x" * 10)
        with mock.patch.object(fsops, "MAX_TREE_FILES", 3):
            with self.assertRaises(BackupError):
                fsops.tree_hash(folder)
        with mock.patch.object(fsops, "MAX_TREE_BYTES", 30):
            with self.assertRaises(BackupError):
                fsops.tree_hash(folder)
        fsops.tree_hash(folder)  # within the real limits

    def test_a_link_inside_a_tree_is_refused(self):
        if not can_make_dir_link(self.root):
            self.skipTest("this machine cannot create a directory link")
        folder = self.home / "with-link"
        folder.mkdir()
        other = self.home / "other"
        other.mkdir()
        fsops.create_dir_link(folder / "inner", other)
        with self.assertRaises(BackupError):
            fsops.tree_hash(folder)


if __name__ == "__main__":
    unittest.main()
