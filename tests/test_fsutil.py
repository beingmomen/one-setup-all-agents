"""Tests for _core.fsutil: Windows directory junction detection and path utilities."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401
from one_setup_all_agents._core import fsutil
from one_setup_all_agents.engine import fsops


class JunctionDetectionTests(unittest.TestCase):
    """`os.path.isjunction` is Python 3.12+; the helper must also work on 3.11 (the minimum, ADR 0004)."""

    def test_a_junction_is_detected_without_os_path_isjunction(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            base = Path(tmp_str)
            target = base / "target"
            target.mkdir()
            link = base / "link"
            fsops.create_dir_link(link, target)  # a junction on Windows, a symlink elsewhere
            with mock.patch.object(fsutil.os.path, "isjunction", None, create=True):
                self.assertIn(fsops.link_kind(link), ("junction", "symlink"))
                self.assertEqual(
                    fsutil.is_junction(link), os.name == "nt"
                )  # a plain symlink is not a junction
                self.assertEqual(fsops.link_kind(target), "dir")
                self.assertFalse(fsutil.is_junction(base / "missing"))

    def test_missing_path_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            missing = Path(tmp_str) / "nonexistent"
            self.assertFalse(fsutil.is_junction(missing))


if __name__ == "__main__":
    unittest.main()
