"""Fixture helpers for the Codex adapter tests. Not collected as tests itself.

The fake home has the user's own Codex config (`tests/fixtures/codex/config.toml`: comments,
unrelated tables, two servers of the user's own) and one skill folder installed by other means.
Neutral names only; the one secret-looking value is a fake and exists to prove it is never read.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.model import Env

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "codex" / "config.toml"


def seed_codex_home(env: Env) -> None:
    """The user's own Codex config before all-agents."""
    config = Path(env.home) / ".codex"
    config.mkdir(parents=True)
    shutil.copyfile(FIXTURE, config / "config.toml")
    skill = Path(env.home) / ".agents" / "skills" / "user-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: user-skill\n---\nThe user's skill.\n", "utf-8")
