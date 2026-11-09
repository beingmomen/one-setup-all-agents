"""Tests verifying docs/adding-a-tool.md documentation accuracy, neutrality and coverage."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import ADAPTERS
from one_setup_all_agents.adapters.base import ToolAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "adding-a-tool.md"

FIXED_NAMES = ("opencode", "claude", "codex", "antigravity", "gemini", "agy")


class AddingAToolDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(DOC_PATH.is_file(), f"Missing docs/adding-a-tool.md at {DOC_PATH}")
        self.doc_text = DOC_PATH.read_text(encoding="utf-8")

    def test_every_public_tool_adapter_member_is_documented(self) -> None:
        public_members = [m for m in dir(ToolAdapter) if not m.startswith("_")]
        public_members.extend(["id", "label", "capabilities"])
        for member in sorted(set(public_members)):
            self.assertIn(
                member,
                self.doc_text,
                f"Member '{member}' of ToolAdapter is missing from docs/adding-a-tool.md",
            )

    def test_guide_names_demo_adapter_and_both_boundary_tests(self) -> None:
        expected_references = [
            "tests/demo_tool_adapter.py",
            "tests/test_tool_boundary.py",
            "tests/test_drop_in_tool.py",
        ]
        for ref in expected_references:
            self.assertIn(
                ref,
                self.doc_text,
                f"Expected reference '{ref}' is missing from docs/adding-a-tool.md",
            )

    def test_doc_contains_no_real_tool_names_outside_marked_excerpts(self) -> None:
        excerpt_pattern = re.compile(
            r"<!-- excerpt: tests/demo_tool_adapter.py -->\s*```.*?```",
            re.DOTALL,
        )
        outside_text = excerpt_pattern.sub("", self.doc_text)

        banned_names = {n.lower() for n in FIXED_NAMES}
        for cls in ADAPTERS:
            banned_names.add(cls.id.lower())
            banned_names.add(cls.label.lower())

        found_violations: list[str] = []
        for name in sorted(banned_names):
            escaped = re.escape(name)
            pattern = rf"(?<![A-Za-z]){escaped}(?![A-Za-z])" if len(name) <= 4 else escaped
            matches = re.findall(pattern, outside_text, re.IGNORECASE)
            if matches:
                found_violations.append(f"{name} ({len(matches)} match(es))")

        self.assertEqual(
            found_violations,
            [],
            f"Real tool names found in docs/adding-a-tool.md outside excerpt blocks: {found_violations}",
        )


if __name__ == "__main__":
    unittest.main()
