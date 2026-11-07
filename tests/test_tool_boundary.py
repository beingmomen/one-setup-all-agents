"""Static half of the "a new tool touches one file" proof (docs/decisions/0008, "Consequences").

No code outside a registered adapter's own module may name a concrete tool. If `engine/`,
`model.py`, `cli.py`, the adapter base or the shared helpers said "claude" or "codex" anywhere, a
new tool would have to edit them, or would be treated as second class. The scan is textual and
strict: comments and docstrings count too, because they are where a special case starts.

Scope:
- Scanned: every `.py` file under `src/one_setup_all_agents/` EXCEPT the modules of the adapters in
  `ADAPTERS` (discovered from the registry, so a new adapter is exempt and its name is banned
  everywhere else automatically) and `_core/`.
- `_core/` is out of scope on purpose: it is the ported prototype, reachable only through the
  internal `_legacy` command, and Phase 5 removes it. Its tool names are not part of the new
  architecture.
- `adapters/__init__.py` may carry the tool names only on its import lines and registry lines.

The dynamic half, with a real drop-in adapter, is `test_drop_in_tool.py`.
"""

import importlib
import re
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import ADAPTERS

PACKAGE = Path(_bootstrap.SRC) / "one_setup_all_agents"

#: Tool names and the tools' own config file names, spelled out so the test does not depend on
#: the registry alone. The registry's ids and labels are added on top (see `banned_patterns`).
FIXED_NAMES = (
    "opencode",
    "claude",
    "codex",
    "antigravity",
    "gemini",
    "agy",
    "opencode.jsonc",
    "config.toml",
    ".claude.json",
    "mcp_config.json",
    "skills.json",
    "settings.json",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
)

#: Only these shapes are allowed to name a tool in `adapters/__init__.py`.
IMPORT_LINE = re.compile(r"^from \.(?P<module>\w+) import (?P<name>\w+)$")
REGISTRY_LINE = re.compile(r"^\s+(?P<name>\w+),$")


def adapter_files() -> set[Path]:
    """The source file of every registered adapter: the only place a tool may be named."""
    files = set()
    for cls in ADAPTERS:
        module = importlib.import_module(cls.__module__)
        files.add(Path(module.__file__).resolve())
    return files


def banned_patterns() -> list[re.Pattern]:
    names = {n.lower() for n in FIXED_NAMES}
    for cls in ADAPTERS:
        names.update({cls.id.lower(), cls.label.lower()})
    patterns = []
    for name in sorted(names):
        # a short name ("agy") must stand alone, or "legacy" would match
        escaped = re.escape(name)
        text = rf"(?<![A-Za-z]){escaped}(?![A-Za-z])" if len(name) <= 4 else escaped
        patterns.append(re.compile(text, re.IGNORECASE))
    return patterns


def find_leaks(text: str, patterns: list[re.Pattern]) -> list[tuple[int, str]]:
    """(line number, line) for every line that names a banned word."""
    return [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), 1)
        if any(p.search(line) for p in patterns)
    ]


def scanned_files() -> list[Path]:
    skip = adapter_files()
    out = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).parts
        if relative[0] == "_core" or path.resolve() in skip:
            continue
        out.append(path)
    return out


def registry_line_is_allowed(line: str) -> bool:
    """An import of a registered adapter's class, or one registry entry."""
    names = {cls.__name__ for cls in ADAPTERS}
    modules = {cls.__module__.rsplit(".", 1)[-1] for cls in ADAPTERS}
    stripped = line.rstrip()
    match = IMPORT_LINE.match(stripped)
    if match:
        return match["name"] in names and match["module"] in modules
    match = REGISTRY_LINE.match(stripped)
    return bool(match) and match["name"] in names


class ToolBoundaryTests(unittest.TestCase):
    def test_no_code_outside_the_adapter_modules_names_a_tool(self):
        patterns = banned_patterns()
        found = []
        for path in scanned_files():
            text = path.read_text("utf-8")
            if path == PACKAGE / "adapters" / "__init__.py":
                lines = text.splitlines()
                leaks = [
                    (number, line.strip())
                    for number, line in find_leaks(text, patterns)
                    if not registry_line_is_allowed(lines[number - 1])
                ]
            else:
                leaks = find_leaks(text, patterns)
            found.extend(f"{path.relative_to(PACKAGE).as_posix()}:{n}: {line}" for n, line in leaks)
        self.assertEqual(found, [], "a tool is named outside its own adapter module")

    def test_the_scan_covers_the_files_that_matter(self):
        names = {p.relative_to(PACKAGE).as_posix() for p in scanned_files()}
        for must in (
            "cli.py",
            "model.py",
            "adapters/__init__.py",
            "adapters/base.py",
            "adapters/_common.py",
            "engine/plan.py",
            "engine/apply.py",
            "engine/revert.py",
        ):
            self.assertIn(must, names)
        self.assertFalse([n for n in names if n.startswith("_core/")])

    def test_each_registered_adapter_is_exempt_and_nothing_else_is(self):
        exempt = {p.relative_to(PACKAGE).as_posix() for p in adapter_files()}
        self.assertEqual(len(exempt), len(ADAPTERS), "two adapters share one module")
        self.assertTrue(all(name.startswith("adapters/") for name in exempt))
        scanned = {p.relative_to(PACKAGE).as_posix() for p in scanned_files()}
        self.assertFalse(exempt & scanned)

    def test_the_scan_can_fail(self):
        """A check that cannot fail proves nothing: planted leaks are found, clean text is not."""
        patterns = banned_patterns()
        planted = (
            'if adapter.id == "claude":\n'
            "    path = home / '.codex' / 'config.toml'\n"
            "# special case for Antigravity\n"
            "NAME = 'mcp_config.json'\n"
            "tool = 'agy'\n"
        )
        self.assertEqual([n for n, _ in find_leaks(planted, patterns)], [1, 2, 3, 4, 5])
        clean = "legacy = 1  # not a tool\nfor adapter in adapters:\n    adapter.plan_mcp()\n"
        self.assertEqual(find_leaks(clean, patterns), [])

    def test_a_registry_line_naming_an_unregistered_tool_is_not_allowed(self):
        self.assertTrue(registry_line_is_allowed("    OpenCodeAdapter,"))
        self.assertTrue(registry_line_is_allowed("from .claude_code import ClaudeCodeAdapter"))
        self.assertFalse(registry_line_is_allowed("    SpecialCase = OpenCodeAdapter"))
        self.assertFalse(registry_line_is_allowed("from .claude_code import helper"))
        self.assertFalse(registry_line_is_allowed("    NotRegisteredAdapter,"))


if __name__ == "__main__":
    unittest.main()
