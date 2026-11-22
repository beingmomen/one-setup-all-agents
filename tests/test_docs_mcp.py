# Copyright (c) 2026 one-setup-all-agents contributors.
# SPDX-License-Identifier: MIT

"""Tests verifying docs/mcp.md examples against the real source loader and CLI parser."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ImportError:
    from tests import _bootstrap  # noqa: F401

from one_setup_all_agents import cli
from one_setup_all_agents.adapters import ADAPTERS
from one_setup_all_agents.config_source import MCP_NAME, SOURCE_CONFIG_NAME, load_source

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "mcp.md"

EXAMPLE_BLOCK_RE = re.compile(
    r"<!--\s*example:\s*mcp\.jsonc\s*-->\s*```jsonc\r?\n(.*?)\r?\n```",
    re.DOTALL,
)


class DocsMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(DOC_PATH.is_file(), f"docs/mcp.md missing at {DOC_PATH}")
        self.doc_text = DOC_PATH.read_text(encoding="utf-8")

    def test_mcp_jsonc_examples_load_with_real_source_loader(self) -> None:
        blocks = EXAMPLE_BLOCK_RE.findall(self.doc_text)
        self.assertGreater(
            len(blocks),
            0,
            "No <!-- example: mcp.jsonc --> ```jsonc blocks found in docs/mcp.md",
        )
        adapter_ids = [cls.id for cls in ADAPTERS]

        for idx, block in enumerate(blocks, 1):
            with self.subTest(example_index=idx), tempfile.TemporaryDirectory() as tmp:
                config_dir = Path(tmp)
                (config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
                (config_dir / MCP_NAME).write_text(block + "\n", encoding="utf-8")

                loaded = load_source(config_dir, adapter_ids)
                self.assertGreater(
                    len(loaded.servers),
                    0,
                    f"Example #{idx} in docs/mcp.md loaded with no servers",
                )

    def test_documented_cli_commands_parse(self) -> None:
        parser = cli.build_parser()
        in_fence = False
        commands: list[str] = []

        for line in self.doc_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence and stripped.startswith("all-agents "):
                commands.append(stripped)

        self.assertGreater(len(commands), 0, "No all-agents commands found in docs/mcp.md")
        for cmd in commands:
            tokens = cmd.split()[1:]
            try:
                parsed = parser.parse_args(tokens)
                self.assertIsNotNone(parsed.command, f"No subcommand parsed from '{cmd}'")
            except SystemExit as exc:
                self.fail(f"Command '{cmd}' in docs/mcp.md failed argparse with exit {exc.code}")


if __name__ == "__main__":
    unittest.main()
