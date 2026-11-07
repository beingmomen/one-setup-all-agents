"""The user-facing `undo` command (Phase 5, not built yet).

Guarantee and drift rules are in docs/decisions/0005-journal-backup-ledger.md. The low-level
revert it will call already exists: `engine.revert.revert_operation` reverses an operation from
its journal in the opposite order, with the drift preflight (a drifted path changes nothing and
raises `RevertError`). What is left for this module is what the ADR adds on top: choosing the
latest operation that is not undone (the stack rule), `--force` (save the drifted content as a
new operation first), `--list`, the retention of 20 operations, and recording the undo as an
operation of its own.
"""

from __future__ import annotations

from ..model import Env


def undo(env: Env, op_id: str | None = None, *, force: bool = False) -> None:
    """Undo operation `op_id`, or the most recent one when `op_id` is None."""
    raise NotImplementedError("engine.undo.undo is not implemented yet (Phase 5).")
