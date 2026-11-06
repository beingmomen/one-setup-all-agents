"""Store the "before" state of everything an op changes.

Layout and format are in docs/decisions/0005-journal-backup-ledger.md. A backup
captures, per path: absent / regular file (bytes, mode) / symlink (target) /
directory link (kind and target).
"""

from __future__ import annotations

from pathlib import Path


def capture(state_dir: Path, op_id: str, path: Path) -> dict:
    """Record the current state of `path` for operation `op_id`; return its journal entry."""
    raise NotImplementedError("engine.backup.capture is not implemented yet (Phase 3).")


def restore(state_dir: Path, entry: dict) -> None:
    """Put `path` back exactly as `entry` describes."""
    raise NotImplementedError("engine.backup.restore is not implemented yet (Phase 3).")
