"""Fixture helpers for the OpenCode adapter tests. Not collected as tests itself.

The fake home has the user's own OpenCode config (`tests/fixtures/opencode/opencode.jsonc`: comments,
trailing commas, two servers of the user's own) and one skill folder installed by other means.
Neutral names only; the one secret-looking value is a fake and exists to prove it is never read.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.model import Env

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opencode" / "opencode.jsonc"


def seed_opencode_home(env: Env) -> None:
    """The user's own OpenCode config before all-agents."""
    config = Path(env.home) / ".config" / "opencode"
    config.mkdir(parents=True)
    shutil.copyfile(FIXTURE, config / "opencode.jsonc")
    skill = Path(env.home) / ".agents" / "skills" / "user-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: user-skill\n---\nThe user's skill.\n", "utf-8")
