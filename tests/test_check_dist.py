# Copyright (c) 2026 one-setup-all-agents contributors.
# SPDX-License-Identifier: MIT

"""Unit test validating scripts/check_dist.py behavior against valid and invalid distribution artifacts."""

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_DIST_PATH = REPO_ROOT / "scripts" / "check_dist.py"

import sys
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from check_dist import check_dist


class CheckDistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dist_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_mock_wheel(self, filename: str, files: dict[str, str]) -> Path:
        p = self.dist_dir / filename
        with zipfile.ZipFile(p, "w") as zf:
            for arcname, content in files.items():
                zf.writestr(arcname, content)
        return p

    def _create_mock_sdist(self, filename: str, files: dict[str, str]) -> Path:
        p = self.dist_dir / filename
        with tarfile.open(p, "w:gz") as tf:
            for arcname, content in files.items():
                data = content.encode("utf-8")
                ti = tarfile.TarInfo(name=arcname)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        return p

    def test_valid_wheel_and_sdist_pass(self) -> None:
        valid_whl_files = {
            "one_setup_all_agents/__init__.py": "__version__ = '0.0.0'\n",
            "one_setup_all_agents-0.0.0.dist-info/entry_points.txt": (
                "[console_scripts]\n"
                "all-agents = one_setup_all_agents.cli:main\n"
                "one-setup-all-agents = one_setup_all_agents.cli:main\n"
            ),
            "one_setup_all_agents-0.0.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\n"
                "Name: one-setup-all-agents\n"
                "Version: 0.0.0\n"
                "Requires-Python: >=3.11\n"
            ),
            "one_setup_all_agents-0.0.0.dist-info/LICENSE": "MIT License\n",
        }
        self._create_mock_wheel("one_setup_all_agents-0.0.0-py3-none-any.whl", valid_whl_files)

        valid_sdist_files = {
            "one_setup_all_agents-0.0.0/pyproject.toml": "[project]\nname = 'one-setup-all-agents'\n",
            "one_setup_all_agents-0.0.0/src/one_setup_all_agents/__init__.py": "",
        }
        self._create_mock_sdist("one_setup_all_agents-0.0.0.tar.gz", valid_sdist_files)

        errors = check_dist(self.dist_dir)
        self.assertEqual(errors, [], f"Expected clean pass, got: {errors}")

    def test_wheel_with_tests_or_forbidden_files_fails(self) -> None:
        bad_whl_files = {
            "one_setup_all_agents/__init__.py": "",
            "tests/test_something.py": "",
            ".personal-patterns": "forbidden",
            "one_setup_all_agents-0.0.0.dist-info/entry_points.txt": (
                "[console_scripts]\nall-agents = one_setup_all_agents.cli:main\n"
            ),
            "one_setup_all_agents-0.0.0.dist-info/METADATA": "Requires-Python: >=3.11\nRequires-Dist: requests\n",
        }
        self._create_mock_wheel("bad-0.0.0-py3-none-any.whl", bad_whl_files)
        self._create_mock_sdist("bad-0.0.0.tar.gz", {"bad-0.0.0/pyproject.toml": ""})

        errors = check_dist(self.dist_dir)
        self.assertGreater(len(errors), 0, "Bad wheel should trigger errors")
        err_str = " ".join(errors)
        self.assertIn("contains test files", err_str)
        self.assertIn("forbidden personal patterns", err_str)
        self.assertIn("missing one-setup-all-agents console script", err_str)
        self.assertIn("Requires-Dist", err_str)


if __name__ == "__main__":
    unittest.main()
