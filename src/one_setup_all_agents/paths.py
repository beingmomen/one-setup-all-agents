"""Locations of the user's source config and of the engine state, per OS.

Decided in docs/decisions/0002-config-and-state-locations.md.

Planned behavior (not implemented in Phase 0):

    config_dir(environ, platform, home) -> Path
        $ALL_AGENTS_CONFIG_DIR if set, else the per-OS default.
    state_dir(environ, platform, home) -> Path
        $ALL_AGENTS_STATE_DIR if set, else the per-OS default.
    build_env(...) -> Env
        Assemble the `Env` that adapters and the engine receive.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .model import Env


def config_dir(environ: Mapping[str, str], platform: str, home: Path) -> Path:
    """Return the directory that holds the user's source config."""
    raise NotImplementedError(
        "paths.config_dir is not implemented yet (Phase 4: source layout and init)."
    )


def state_dir(environ: Mapping[str, str], platform: str, home: Path) -> Path:
    """Return the directory that holds the journal, backups and lock."""
    raise NotImplementedError("paths.state_dir is not implemented yet (Phase 3: engine).")


def build_env() -> Env:
    """Build the `Env` for the running machine."""
    raise NotImplementedError("paths.build_env is not implemented yet (Phase 3: engine).")
