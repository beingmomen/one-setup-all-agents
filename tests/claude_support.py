"""Fixture helpers for the Claude Code adapter tests. Not collected as tests itself.

The fake home has the user's own `.claude.json` (`tests/fixtures/claude/claude.json`: unrelated
keys, a project-scoped server, two user-scope servers of the user's own) and one skill folder
installed by other means. Neutral names only; the one secret-looking value is a fake and exists
to prove it is never read.

`FakeClaudeCli` is an in-process stand-in for `claude mcp add-json|remove -s user`. It never
starts a program. It keeps the `mcpServers` names sorted, so that an update followed by its undo
(which re-adds the old entry at the end) gives back the exact bytes; the real CLI does not promise
that order, which is why the real undo is "the same servers", not "the same bytes".
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.model import Env

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude" / "claude.json"
BIN_NAME = "claude"


def canonical_bytes(doc: dict) -> bytes:
    """The bytes the fake CLI (and the seed) write: 2-space indent, `mcpServers` sorted by name."""
    doc = dict(doc)
    if "mcpServers" in doc:
        doc["mcpServers"] = dict(sorted(doc["mcpServers"].items()))
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


def seed_claude_home(env: Env) -> None:
    """The user's own Claude Code state before all-agents."""
    doc = json.loads(FIXTURE.read_text("utf-8"))
    (Path(env.home) / ".claude.json").write_bytes(canonical_bytes(doc))
    skill = Path(env.home) / ".claude" / "skills" / "user-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: user-skill\n---\nThe user's skill.\n", "utf-8")


class FakeClaudeCli:
    """Stand-in for `claude mcp add-json <name> <json> -s user` and `claude mcp remove <name> -s user`.

    Any other call (another verb, another scope, no scope) returns 2, so a plan that forgets `-s
    user` fails loudly. `fail_if(argv)` makes a call return 1 without changing anything.
    """

    def __init__(self, path: Path, fail_if: Callable[[list[str]], bool] | None = None) -> None:
        self.path = Path(path)
        self.fail_if = fail_if
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        argv = list(argv)
        self.calls.append(argv)
        if self.fail_if is not None and self.fail_if(argv):
            return 1
        if argv[1:2] != ["mcp"] or len(argv) < 6 or argv[-2:] != ["-s", "user"]:
            return 2
        verb, name = argv[2], argv[3]
        doc = json.loads(self.path.read_text("utf-8")) if self.path.is_file() else {}
        servers = doc.setdefault("mcpServers", {})
        if verb == "add-json" and len(argv) == 7:
            if name in servers:
                return 1
            try:
                servers[name] = json.loads(argv[4])
            except ValueError:
                return 1
        elif verb == "remove" and len(argv) == 6:
            if name not in servers:
                return 1
            del servers[name]
        else:
            return 2
        if not servers:
            del doc["mcpServers"]
        if doc:
            self.path.write_bytes(canonical_bytes(doc))
        elif self.path.is_file():
            self.path.unlink()
        return 0


def make_fake_binary(directory: Path, name: str = BIN_NAME) -> Path:
    """An empty file named like the claude binary (executable on POSIX). Never run."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


def contract_namespace() -> dict:
    """Extra hooks for the contract suite: an environment that finds the fake binary, and the fake
    CLI that edits the fake home's `.claude.json`."""

    def make_env(self, root):
        binary = make_fake_binary(root / "bin")
        return Env(
            home=root / "home",
            platform=_platform(),
            environ={"ALL_AGENTS_CLAUDE_BIN": str(binary)},
            config_dir=root / "config",
            state_dir=root / "state",
        )

    def make_cli(self, env):
        return FakeClaudeCli(Path(env.home) / ".claude.json")

    return {"make_env": make_env, "make_cli": make_cli}


def _platform() -> str:
    from one_setup_all_agents.paths import detect_platform

    return detect_platform()
