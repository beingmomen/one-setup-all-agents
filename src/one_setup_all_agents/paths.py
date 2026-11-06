"""Locations of the user's source config and of the engine state, per OS.

Decided in docs/decisions/0002-config-and-state-locations.md:

    Windows  config: %APPDATA%/one-setup-all-agents       state: %LOCALAPPDATA%/one-setup-all-agents
    Linux    config: $XDG_CONFIG_HOME or ~/.config        state: $XDG_STATE_HOME or ~/.local/state
    macOS    the same as Linux

The overrides `ALL_AGENTS_CONFIG_DIR` and `ALL_AGENTS_STATE_DIR` beat everything.
The functions are pure: they take the environment mapping, the platform and the home
directory as arguments, so tests can use a fake home and never read the real one.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from .model import Env

APP_DIR = "one-setup-all-agents"
ENV_CONFIG_DIR = "ALL_AGENTS_CONFIG_DIR"
ENV_STATE_DIR = "ALL_AGENTS_STATE_DIR"


def detect_platform() -> str:
    """'windows', 'macos' or 'linux' (anything POSIX that is not macOS)."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _absolute_env(environ: Mapping[str, str], name: str) -> Path | None:
    """An environment variable holding an absolute path, else None (XDG ignores relative ones)."""
    value = environ.get(name)
    if value and Path(value).is_absolute():
        return Path(value)
    return None


def config_dir(environ: Mapping[str, str], platform: str, home: Path) -> Path:
    """Return the directory that holds the user's source config."""
    override = environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override)
    home = Path(home)
    if platform == "windows":
        base = _absolute_env(environ, "APPDATA") or home / "AppData" / "Roaming"
    else:
        base = _absolute_env(environ, "XDG_CONFIG_HOME") or home / ".config"
    return base / APP_DIR


def state_dir(environ: Mapping[str, str], platform: str, home: Path) -> Path:
    """Return the directory that holds the journal, backups and lock."""
    override = environ.get(ENV_STATE_DIR)
    if override:
        return Path(override)
    home = Path(home)
    if platform == "windows":
        base = _absolute_env(environ, "LOCALAPPDATA") or home / "AppData" / "Local"
    else:
        base = _absolute_env(environ, "XDG_STATE_HOME") or home / ".local" / "state"
    return base / APP_DIR


def build_env(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Env:
    """Build the `Env` for the running machine (or for a fake one in tests)."""
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    platform = detect_platform()
    return Env(
        home=home,
        platform=platform,
        environ=dict(environ),
        config_dir=config_dir(environ, platform, home),
        state_dir=state_dir(environ, platform, home),
    )
