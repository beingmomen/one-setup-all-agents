# Copyright (c) 2026 one-setup-all-agents contributors.
# SPDX-License-Identifier: MIT

"""Unit test validating docs/github-metadata.md suggested description and repository topics."""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
METADATA_PATH = REPO_ROOT / "docs" / "github-metadata.md"

TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class DocsGithubMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(METADATA_PATH.is_file(), f"github-metadata.md missing at {METADATA_PATH}")
        self.text = METADATA_PATH.read_text(encoding="utf-8")

    def _extract_fenced_blocks(self) -> dict[str, str]:
        lines = self.text.splitlines()
        current_section = ""
        in_fence = False
        fence_lines: list[str] = []
        blocks: dict[str, str] = {}

        for line in lines:
            if line.startswith("## "):
                current_section = line[3:].strip().lower()
                continue
            if line.strip().startswith("```"):
                if in_fence:
                    blocks[current_section] = "\n".join(fence_lines).strip()
                    fence_lines = []
                    in_fence = False
                else:
                    in_fence = True
                    fence_lines = []
                continue
            if in_fence:
                fence_lines.append(line)
        return blocks

    def test_description_block_validity(self) -> None:
        blocks = self._extract_fenced_blocks()
        self.assertIn("description", blocks, "Description block missing in github-metadata.md")
        desc = blocks["description"]

        # Requirements:
        # 1. contains 'shared agent config'
        self.assertIn("shared agent config", desc)
        # 2. names the four tools (case-insensitive)
        desc_lower = desc.lower()
        for tool in ("claude code", "codex", "opencode", "antigravity"):
            self.assertIn(tool, desc_lower, f"Description missing tool '{tool}'")
        # 3. length <= 350
        self.assertLessEqual(len(desc), 350, f"Description too long: {len(desc)} > 350")

    def test_topics_block_validity(self) -> None:
        blocks = self._extract_fenced_blocks()
        self.assertIn("topics", blocks, "Topics block missing in github-metadata.md")
        topic_lines = [l.strip() for l in blocks["topics"].splitlines() if l.strip()]

        # Requirements:
        # 1. up to 20 entries
        self.assertLessEqual(len(topic_lines), 20, f"Too many topics: {len(topic_lines)} > 20")
        self.assertGreater(len(topic_lines), 0, "No topics found")

        # 2. No duplicates
        self.assertEqual(len(topic_lines), len(set(topic_lines)), "Duplicate topics found")

        # 3. Match format ^[a-z0-9][a-z0-9-]*$ and length <= 50
        for topic in topic_lines:
            self.assertTrue(
                bool(TOPIC_PATTERN.match(topic)),
                f"Topic '{topic}' does not match pattern ^[a-z0-9][a-z0-9-]*$",
            )
            self.assertLessEqual(len(topic), 50, f"Topic '{topic}' exceeds 50 characters")


if __name__ == "__main__":
    unittest.main()
