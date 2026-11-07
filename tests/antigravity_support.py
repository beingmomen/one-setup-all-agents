"""Fixture helpers for the Antigravity adapter tests. Not collected as tests itself.

The fake home has the user's own `~/.gemini/config/mcp_config.json`
(`tests/fixtures/antigravity/mcp_config.json`: comments, other keys, two servers of the user's own),
a `skills.json` with one entry of the user's own (a customised folder entry) and one real skill
folder in the folder agy reads by itself. Neutral names only; the one secret-looking value is a
fake and exists to prove it is never read.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.model import Env

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "antigravity" / "mcp_config.json"


def seed_antigravity_home(env: Env) -> None:
    """The user's own Antigravity config before all-agents."""
    config = Path(env.home) / ".gemini" / "config"
    config.mkdir(parents=True)
    shutil.copyfile(FIXTURE, config / "mcp_config.json")
    shared = Path(env.home) / "user-skills"  # a folder of skills that the user's entry names
    (shared / "shared-skill").mkdir(parents=True)
    (shared / "shared-skill" / "SKILL.md").write_text("---\nname: shared-skill\n---\nX\n", "utf-8")
    manifest = {
        "exclude": ["hidden-skill"],
        "entries": [{"path": (shared).as_posix(), "exclude": ["load-test"]}],
    }
    (config / "skills.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    own = config / "skills" / "user-skill"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: user-skill\n---\nThe user's skill.\n", "utf-8")


def contract_namespace() -> dict:
    """Extra members for the contract class: `demo-remote` has a header, which agy cannot write
    (no variable reference), so it is the harness server that is not written; `demo-off` is."""
    return {
        "unwritten_mcp": lambda self: frozenset({"demo-remote"}),
        "removable_mcp": lambda self: "demo-off",
    }
