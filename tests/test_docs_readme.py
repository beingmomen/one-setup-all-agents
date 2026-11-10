# Copyright (c) 2026 one-setup-all-agents contributors.
# SPDX-License-Identifier: MIT

"""Unit test validating README.md documentation integrity and command examples."""

import re
import unittest
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ImportError:
    from tests import _bootstrap  # noqa: F401

from one_setup_all_agents import cli

from tests.test_docs_support_table import LINK_PATTERN, STATIC_VOCAB, VALID_V01_VOCAB, VERIFIED_PATTERN


REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"


class DocsReadmeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(README_PATH.is_file(), f"README.md missing at {README_PATH}")
        self.readme_text = README_PATH.read_text(encoding="utf-8")

    def test_readme_commands_are_valid(self) -> None:
        parser = cli.build_parser()
        # Extract commands from fenced code blocks
        in_fence = False
        all_agents_cmds: list[str] = []
        install_cmds: list[str] = []

        for line in self.readme_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                if stripped.startswith("all-agents "):
                    all_agents_cmds.append(stripped)
                elif stripped.startswith("uvx one-setup-all-agents ") or stripped.startswith("pipx install one-setup-all-agents"):
                    install_cmds.append(stripped)

        self.assertGreater(len(all_agents_cmds), 0, "No all-agents commands found in README")
        self.assertGreater(len(install_cmds), 0, "No install commands found in README")

        for cmd in all_agents_cmds:
            # Parse arguments without --help which causes SystemExit
            tokens = cmd.split()[1:]
            try:
                parsed = parser.parse_args(tokens)
                self.assertIsNotNone(parsed.command, f"Failed to parse valid subcommand from '{cmd}'")
            except SystemExit as exc:
                self.fail(f"Command '{cmd}' in README failed argparse validation with exit code {exc.code}")

    def test_no_hardcoded_user_paths_or_emails(self) -> None:
        # Check for forbidden personal path patterns and email addresses
        forbidden_patterns = [
            (re.compile(r"[A-Za-z]:\\Users\\[^\s`\)\"']+", re.IGNORECASE), "Windows user path"),
            (re.compile(r"/home/[^\s`\)\"']+"), "Linux home path"),
            (re.compile(r"/Users/[^\s`\)\"']+"), "macOS Users path"),
            (re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "Email address"),
        ]
        for pattern, label in forbidden_patterns:
            matches = pattern.findall(self.readme_text)
            self.assertEqual(matches, [], f"README contains forbidden {label}: {matches}")

    def test_support_table_vocabulary_in_readme(self) -> None:
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

        self.assertGreater(len(table_lines), 2, "Supported tools table not found")
        headers = [c.strip() for c in table_lines[0].strip("|").split("|")]
        for line in table_lines[2:]:
            cols = [c.strip() for c in line.strip("|").split("|")]
            row = dict(zip(headers, cols))
            v01 = row["v0.1 (global scope)"]
            self.assertIn(v01, VALID_V01_VOCAB)

            for os_col in ("Windows", "Linux", "macOS"):
                cell = row[os_col]
                link_match = LINK_PATTERN.match(cell)
                label = link_match.group("text").strip() if link_match else cell
                is_valid = (label in STATIC_VOCAB) or bool(VERIFIED_PATTERN.match(label))
                self.assertTrue(is_valid, f"Invalid vocabulary '{label}' in {os_col}")


if __name__ == "__main__":
    unittest.main()
