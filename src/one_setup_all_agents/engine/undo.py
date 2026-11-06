"""Reverse an operation from its journal.

Guarantee and drift rules are in docs/decisions/0005-journal-backup-ledger.md:
entries are reversed in the opposite order; a file that changed after the
operation is reported and left alone unless `force` is set.
"""

from __future__ import annotations

from ..model import Env


def undo(env: Env, op_id: str | None = None, *, force: bool = False) -> None:
    """Undo operation `op_id`, or the most recent one when `op_id` is None."""
    raise NotImplementedError("engine.undo.undo is not implemented yet (Phase 5).")
