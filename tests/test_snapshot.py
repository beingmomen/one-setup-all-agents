"""Tests for safety snapshot of original files before first write (ADR 0017)."""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import POSIX, EngineCase, FakeRunner
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.backup import payload_path
from one_setup_all_agents.engine.journal import op_dir
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import Detection, Plan, Scope
from one_setup_all_agents.snapshot import snapshot_ops

PLANTED_SECRET = "sk-" + "s3cr3tk3y" * 5


class SnapshotTests(EngineCase):
    def _installed_detections(self, adapters):
        return [
            Detection(installed=True, tool=a.id, label=a.label, binary=Path(f"/bin/{a.id}"))
            for a in adapters
        ]

    def test_snapshot_ops_leaves_home_unchanged_and_payload_equals_original(self):
        adapters = all_adapters()
        adapters_by_id = {a.id: a for a in adapters}

        # Create sample files for adapters
        original_contents = {}
        for adapter in adapters:
            for path in adapter.snapshot_files(self.env, Scope.GLOBAL):
                self.write(path, f"# config for {adapter.id}\n".encode("utf-8"))
                original_contents[path] = path.read_bytes()

        detections = self._installed_detections(adapters)
        ops, messages = snapshot_ops(self.env, adapters_by_id, detections)
        self.assertTrue(ops)
        self.assertEqual(messages, [])

        before_listing = self.listing(self.home)

        # Apply snapshot-only plan
        plan = Plan(tuple(ops))
        op_id = apply_plan(self.env, plan, label="snapshot", run_cli=FakeRunner())
        self.assertIsNotNone(op_id)

        # 1. No byte of home changed
        after_listing = self.listing(self.home)
        self.assertEqual(before_listing, after_listing)

        # 2. Journal has one done entry per existing file
        operations = journal.list_operations(self.state)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["status"], "done")

        entries = journal.load(self.state, op_id).entries
        self.assertEqual(len(entries), len(original_contents))
        for entry in entries:
            self.assertEqual(entry["status"], "done")
            self.assertIn("backup", entry["before"])
            backup_ref = entry["before"]["backup"]
            payload = payload_path(self.state, op_id, backup_ref)
            self.assertTrue(payload.is_file())
            target_path = Path(entry["real_path"])
            self.assertEqual(payload.read_bytes(), original_contents[target_path])

    @unittest.skipUnless(POSIX, "permission bits are a POSIX concept")
    def test_snapshot_payload_mode_is_0600_on_posix(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        for path in adapter.snapshot_files(self.env, Scope.GLOBAL):
            self.write(path, b"data\n")
            break

        detections = self._installed_detections([adapter])
        ops, _ = snapshot_ops(self.env, adapters_by_id, detections)
        op_id = apply_plan(self.env, Plan(tuple(ops)), label="snapshot", run_cli=FakeRunner())

        entries = journal.load(self.state, op_id).entries
        payload = payload_path(self.state, op_id, entries[0]["before"]["backup"])
        self.assertEqual(stat.S_IMODE(os.stat(payload).st_mode), 0o600)

    def test_reverting_snapshot_operation_changes_nothing_in_home(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        for path in adapter.snapshot_files(self.env, Scope.GLOBAL):
            self.write(path, b"initial\n")

        detections = self._installed_detections([adapter])
        ops, _ = snapshot_ops(self.env, adapters_by_id, detections)
        before_listing = self.listing(self.home)

        op_id = apply_plan(self.env, Plan(tuple(ops)), label="snapshot", run_cli=FakeRunner())
        self.assertEqual(self.listing(self.home), before_listing)

        report = revert_operation(self.env, op_id, run_cli=FakeRunner())
        self.assertEqual(report.op_id, op_id)
        self.assertEqual(self.listing(self.home), before_listing)

    def test_planted_secret_not_in_journal_json_or_plan(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        for path in adapter.snapshot_files(self.env, Scope.GLOBAL):
            self.write(path, f"secret_key = {PLANTED_SECRET}\n".encode("utf-8"))
            break

        detections = self._installed_detections([adapter])
        ops, _ = snapshot_ops(self.env, adapters_by_id, detections)
        plan = Plan(tuple(ops))

        # Check rendered plan description and repr
        for op in plan.executable:
            self.assertNotIn(PLANTED_SECRET, op.description)
            self.assertNotIn(PLANTED_SECRET, repr(op))

        op_id = apply_plan(self.env, plan, label="snapshot", run_cli=FakeRunner())

        # Check journal JSON files (journal.json)
        operation_dir = op_dir(self.state, op_id)
        journal_file = operation_dir / "journal.json"
        self.assertTrue(journal_file.is_file())
        journal_text = journal_file.read_text(encoding="utf-8")
        self.assertNotIn(PLANTED_SECRET, journal_text)

        # Assert payload folder is under state dir
        payload_dir = operation_dir / "files"
        self.assertTrue(payload_dir.is_dir())
        self.assertTrue(payload_dir.is_relative_to(self.state))

    def test_missing_file_skipped_silently_without_warnings(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        for path in adapter.snapshot_files(self.env, Scope.GLOBAL):
            if path.exists():
                path.unlink()

        detections = self._installed_detections([adapter])
        ops, messages = snapshot_ops(self.env, adapters_by_id, detections)
        self.assertEqual(ops, [])
        self.assertEqual(messages, [])

    def test_broken_symlink_emits_warning(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        paths = list(adapter.snapshot_files(self.env, Scope.GLOBAL))
        self.assertTrue(paths)
        snap_path = paths[0]
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        if snap_path.exists():
            snap_path.unlink()

        missing_target = self.home / "nonexistent_target_file.json"
        try:
            os.symlink(missing_target, snap_path)
        except OSError:
            self.skipTest("Symlinks not supported in this environment")

        detections = self._installed_detections([adapter])
        ops, messages = snapshot_ops(self.env, adapters_by_id, detections)
        self.assertEqual(ops, [])
        self.assertEqual(messages, [f"snapshot skipped broken symlink: {snap_path}"])

    def test_broken_symlink_mocked_emits_broken_symlink_warning(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        paths = list(adapter.snapshot_files(self.env, Scope.GLOBAL))
        self.assertTrue(paths)
        snap_path = paths[0]
        missing_target = self.home / "nonexistent_target_file.json"

        def fake_islink(p):
            return Path(p) == snap_path

        def fake_realpath(p):
            if Path(p) == snap_path:
                return str(missing_target)
            return str(p)

        def fake_lexists(p):
            if Path(p) == snap_path:
                return True
            return os.path.exists(p)

        detections = self._installed_detections([adapter])
        with (
            patch("os.path.islink", side_effect=fake_islink),
            patch("os.path.realpath", side_effect=fake_realpath),
            patch("os.path.lexists", side_effect=fake_lexists),
        ):
            ops, messages = snapshot_ops(self.env, adapters_by_id, detections)

        self.assertEqual(ops, [])
        self.assertEqual(messages, [f"snapshot skipped broken symlink: {snap_path}"])

    def test_symlink_to_directory_mocked_emits_not_a_regular_file_warning(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        paths = list(adapter.snapshot_files(self.env, Scope.GLOBAL))
        self.assertTrue(paths)
        snap_path = paths[0]
        target_dir = self.home / "target_directory"
        target_dir.mkdir(parents=True, exist_ok=True)

        def fake_islink(p):
            return Path(p) == snap_path

        def fake_realpath(p):
            if Path(p) == snap_path:
                return str(target_dir)
            return str(p)

        def fake_lexists(p):
            if Path(p) == snap_path:
                return True
            return os.path.exists(p)

        detections = self._installed_detections([adapter])
        with (
            patch("os.path.islink", side_effect=fake_islink),
            patch("os.path.realpath", side_effect=fake_realpath),
            patch("os.path.lexists", side_effect=fake_lexists),
        ):
            ops, messages = snapshot_ops(self.env, adapters_by_id, detections)

        self.assertEqual(ops, [])
        self.assertEqual(
            messages, [f"snapshot skipped path that is not a regular file: {snap_path}"]
        )

    def test_existing_directory_emits_warning(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        paths = list(adapter.snapshot_files(self.env, Scope.GLOBAL))
        self.assertTrue(paths)
        snap_path = paths[0]
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        if snap_path.exists():
            snap_path.unlink()
        snap_path.mkdir()

        detections = self._installed_detections([adapter])
        ops, messages = snapshot_ops(self.env, adapters_by_id, detections)
        self.assertEqual(ops, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("not a regular file", messages[0].lower())

    def test_large_file_several_megabytes_snapshotted_without_cap(self):
        adapter = all_adapters()[0]
        adapters_by_id = {adapter.id: adapter}
        paths = list(adapter.snapshot_files(self.env, Scope.GLOBAL))
        self.assertTrue(paths)
        snap_path = paths[0]
        # Write 3 MiB of data
        large_data = b"x" * (3 * 1024 * 1024)
        self.write(snap_path, large_data)

        detections = self._installed_detections([adapter])
        ops, messages = snapshot_ops(self.env, adapters_by_id, detections)
        self.assertEqual(messages, [])
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].path, snap_path)


if __name__ == "__main__":
    unittest.main()
