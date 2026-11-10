# Copyright (c) 2026 one-setup-all-agents contributors.
# SPDX-License-Identifier: MIT

"""Unit test validating SECURITY.md documentation and cited code references."""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SECURITY_PATH = REPO_ROOT / "SECURITY.md"
ADVISORY_URL = "https://github.com/beingmomen/one-setup-all-agents/security/advisories/new"


class DocsSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(SECURITY_PATH.is_file(), f"SECURITY.md missing at {SECURITY_PATH}")
        self.security_text = SECURITY_PATH.read_text(encoding="utf-8")

    def test_security_doc_required_elements(self) -> None:
        # Check advisory URL
        self.assertIn(
            ADVISORY_URL,
            self.security_text,
            f"Advisory URL '{ADVISORY_URL}' missing from SECURITY.md",
        )

        # Check required phrases
        self.assertIn("supported versions", self.security_text.lower())
        self.assertIn("report", self.security_text.lower())

        # Check that no email addresses are disclosed
        email_pattern = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
        emails = email_pattern.findall(self.security_text)
        self.assertEqual(emails, [], f"SECURITY.md contains forbidden email addresses: {emails}")

    def test_cited_python_files_exist(self) -> None:
        # Parse all backticked code references matching *.py
        py_ref_pattern = re.compile(r"`([a-zA-Z0-9_/\\]+\.py)`")
        refs = py_ref_pattern.findall(self.security_text)
        self.assertGreater(len(refs), 0, "No cited python files found in SECURITY.md")

        for ref in refs:
            # Normalize path
            ref_path = (REPO_ROOT / ref.replace("\\", "/")).resolve()
            self.assertTrue(
                ref_path.is_file(),
                f"Cited file '{ref}' in SECURITY.md does not exist at {ref_path}",
            )


if __name__ == "__main__":
    unittest.main()
