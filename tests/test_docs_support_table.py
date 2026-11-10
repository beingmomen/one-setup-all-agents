# Copyright (c) 2026 one-setup-all-agents contributors.
# SPDX-License-Identifier: MIT

"""Unit test enforcing that README.md Supported tools table stays consistent with verification docs."""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
DOCS_VERIFICATION_DIR = REPO_ROOT / "docs" / "verification"

VALID_V01_VOCAB = {"supported", "partial", "planned"}

# Pattern for valid cell vocabulary:
# - verified (<tool version>, <date>)
# - fixtures only (CI)
# - unverified
# - planned
# - not supported
VERIFIED_PATTERN = re.compile(r"^verified \([^,]+,\s*\d{4}-\d{2}-\d{2}\)$")
STATIC_VOCAB = {"fixtures only (CI)", "unverified", "planned", "not supported"}

LINK_PATTERN = re.compile(r"^\[(?P<text>[^\]]+)\]\((?P<url>[^\)]+)\)$")


class DocsSupportTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(README_PATH.is_file(), f"README.md missing at {README_PATH}")
        self.readme_text = README_PATH.read_text(encoding="utf-8")

    def _extract_table_rows(self) -> list[dict[str, str]]:
        """Extract the rows of the ## Supported tools table."""
        lines = self.readme_text.splitlines()
        in_section = False
        table_lines: list[str] = []
        for line in lines:
            if line.strip() == "## Supported tools":
                in_section = True
                continue
            if in_section:
                if line.startswith("## "):
                    break
                if line.strip().startswith("|"):
                    table_lines.append(line.strip())

        self.assertGreater(len(table_lines), 2, "Supported tools table not found or empty")
        headers = [c.strip() for c in table_lines[0].strip("|").split("|")]
        # separator is table_lines[1]
        rows: list[dict[str, str]] = []
        for line in table_lines[2:]:
            cols = [c.strip() for c in line.strip("|").split("|")]
            self.assertEqual(len(cols), len(headers), f"Malformed table row: {line}")
            rows.append(dict(zip(headers, cols)))
        return rows

    def test_support_table_columns_and_tools(self) -> None:
        rows = self._extract_table_rows()
        adapter_ids = {r["Adapter id"].strip("`") for r in rows}
        expected_adapters = {"claude", "codex", "opencode", "agy"}
        self.assertEqual(adapter_ids, expected_adapters, "All registered adapters must be in support table")

    def test_support_table_vocabulary_and_links(self) -> None:
        rows = self._extract_table_rows()
        for row in rows:
            tool_name = row["Tool"]
            adapter_id = row["Adapter id"].strip("`")

            # Check v0.1 column
            v01 = row["v0.1 (global scope)"]
            self.assertIn(v01, VALID_V01_VOCAB, f"Invalid v0.1 value '{v01}' for {tool_name}")

            # Check OS columns
            for os_col in ("Windows", "Linux", "macOS"):
                cell = row[os_col]
                link_match = LINK_PATTERN.match(cell)
                if link_match:
                    label = link_match.group("text").strip()
                    url = link_match.group("url").strip()
                else:
                    label = cell
                    url = None

                # Validate label against vocabulary
                is_valid = (label in STATIC_VOCAB) or bool(VERIFIED_PATTERN.match(label))
                self.assertTrue(
                    is_valid,
                    f"Invalid vocabulary word '{label}' in column '{os_col}' for tool '{tool_name}'",
                )

                # If starts with verified, link must exist and target file must contain verification proof
                if label.startswith("verified"):
                    self.assertIsNotNone(
                        url,
                        f"Cell '{cell}' starts with 'verified' but has no link",
                    )
                    target_file = (REPO_ROOT / url).resolve()
                    self.assertTrue(
                        target_file.is_file(),
                        f"Verification file '{url}' does not exist",
                    )
                    file_content = target_file.read_text(encoding="utf-8")
                    # Target file must mention the tool (by adapter id or name) and have a row with 'verified'
                    has_tool_mention = (
                        adapter_id in file_content.lower()
                        or tool_name.split()[0].lower() in file_content.lower()
                    )
                    self.assertTrue(
                        has_tool_mention,
                        f"Verification doc '{url}' does not mention tool '{tool_name}'",
                    )
                    self.assertIn(
                        "verified",
                        file_content.lower(),
                        f"Verification doc '{url}' has no verified rows",
                    )


if __name__ == "__main__":
    unittest.main()
