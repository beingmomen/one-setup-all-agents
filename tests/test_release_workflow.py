# Copyright (c) 2026 one-setup-all-agents contributors.
# SPDX-License-Identifier: MIT

"""Unit test validating .github/workflows/release.yml safety guarantees and trusted publishing configuration."""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(WORKFLOW_PATH.is_file(), f"release.yml missing at {WORKFLOW_PATH}")
        self.workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_top_level_permissions_and_triggers(self) -> None:
        # Assert tag trigger and workflow_dispatch
        self.assertIn('tags:\n      - "v*"', self.workflow_text)
        self.assertIn("workflow_dispatch:", self.workflow_text)

        # Assert top level permissions are contents: read and NOT write-all
        self.assertNotIn("write-all", self.workflow_text)
        self.assertIn("permissions:\n  contents: read", self.workflow_text)

    def test_no_pypi_token_or_passwords(self) -> None:
        self.assertNotIn("PYPI_TOKEN", self.workflow_text)
        self.assertNotIn("password:", self.workflow_text)

        # Ensure no secrets references (except potentially secrets.GITHUB_TOKEN if ever used)
        secret_matches = re.findall(r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}", self.workflow_text)
        for s in secret_matches:
            self.assertEqual(s, "GITHUB_TOKEN", f"Forbidden secret reference: secrets.{s}")

    def test_id_token_write_only_in_publish_job(self) -> None:
        self.assertIn("publish:", self.workflow_text)
        build_part, publish_part = self.workflow_text.split("publish:", 1)

        # Build part must not have id-token: write
        self.assertNotIn("id-token: write", build_part)

        # Publish part must have id-token: write and environment: pypi
        self.assertIn("id-token: write", publish_part)
        self.assertEqual(
            self.workflow_text.count("id-token: write"),
            1,
            "id-token: write must appear exactly once",
        )
        self.assertIn("environment: pypi", publish_part)

    def test_version_verification_step_present(self) -> None:
        self.assertIn("Verify tag matches __version__", self.workflow_text)
        self.assertIn("check_dist.py", self.workflow_text)


if __name__ == "__main__":
    unittest.main()
