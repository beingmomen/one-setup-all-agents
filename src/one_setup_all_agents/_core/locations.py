"""Where the transitional core keeps its own files (ledger, backups, lock), derived from ADR 0002.

Every function takes `home` because the core is tested against a fake home. The real
environment (XDG_STATE_HOME, LOCALAPPDATA) is consulted only when `home` IS the real home,
so a test with a fake home can never reach into the real state directory. The explicit
override `ALL_AGENTS_STATE_DIR` always wins (and is how tests redirect everything).

Phase 3 replaces this with the unified journal of ADR 0005.
"""

import os
from pathlib import Path

from .. import paths


def _is_real_home(home):
    try:
        return os.path.samefile(home, Path.home())
    except OSError:
        return False


def state_root(home):
    """The directory for all engine state of this machine."""
    home = Path(home)
    environ = (
        os.environ
        if _is_real_home(home)
        else {k: v for k, v in os.environ.items() if k == paths.ENV_STATE_DIR}
    )
    return paths.state_dir(environ, paths.detect_platform(), home)


def backups_dir(home):
    """Backup folders (`backup-<timestamp>-<kind>`) live here."""
    return state_root(home) / "backups"


def ledger_file(home):
    """Ownership ledger of the transitional core (not the ADR 0005 ledger)."""
    return state_root(home) / "legacy-ledger.json"


def lock_file(home):
    return state_root(home) / "engine.lock"


def skills_journal_dir(home):
    return state_root(home) / "skills-journal"


def config_root(home):
    """The user's source config directory (ADR 0003). Same isolation rule as `state_root`."""
    home = Path(home)
    environ = (
        os.environ
        if _is_real_home(home)
        else {k: v for k, v in os.environ.items() if k == paths.ENV_CONFIG_DIR}
    )
    return paths.config_dir(environ, paths.detect_platform(), home)
