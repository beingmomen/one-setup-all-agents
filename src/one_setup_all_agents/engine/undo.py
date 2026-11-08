"""The user-facing `undo` command: reverse operations from the journal (Phase 5)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..model import Env
from . import backup, journal, revert
from .apply import recover_locked
from .errors import RevertError
from .lock import engine_lock
from .revert import drifted_paths
from .runcli import Runner, default_runner


@dataclass
class UndoResult:
    """The outcome of reversing the most recent operation."""

    op_id: str
    label: str
    reverted_entries: list[int]
    recovered: list[str]
    undo_op_id: str
    remaining: int


def undo(
    env: Env,
    *,
    force: bool = False,
    run_cli: Runner | None = None,
    lock_timeout: float = 0.0,
) -> UndoResult | None:
    """Reverse the most recent undoable operation (status `done` and kind != `undo`).

    A crashed operation (`open`) is recovered first. When `force` is False, drift in any
    affected path refuses the revert with `RevertError`. When `force` is True, safety copies
    of current path states are recorded in the undo operation's journal.
    """
    runner = run_cli or default_runner(env)
    with engine_lock(env.state_dir, timeout=lock_timeout):
        # (a) Recover unfinished operations
        recovered = recover_locked(env, runner)

        # (b) Get undoable operations
        undoable = journal.list_undoable(env.state_dir)
        if not undoable:
            return None

        # (c) Target newest undoable operation
        target_id = undoable[0]["id"]
        target_op = journal.load(env.state_dir, target_id)

        # (d) Preflight drift check unless force=True
        pending = [e for e in target_op.entries if not e.get("reverted")]
        if not force:
            drifted = drifted_paths(pending)
            if drifted:
                names = ", ".join(str(p) for p in drifted)
                raise RevertError(
                    f"operation {target_op.id}: these paths changed since it ran, "
                    f"nothing was reverted: {names}",
                    op_id=target_op.id,
                    drifted=drifted,
                )

        # (e) Create undo operation and record safety copies
        undo_op = journal.begin(
            env.state_dir,
            f"undo {target_op.id}",
            kind="undo",
            undoes=target_op.id,
        )

        slot = 1
        for entry in pending:
            entry_type = entry.get("type")
            if entry_type in ("edit_file", "link"):
                p = Path(entry.get("real_path") or entry.get("path"))
                current_state = backup.describe(p)
                if current_state["state"] in ("file", "copy_tree"):
                    before = backup.capture(
                        env.state_dir, undo_op.id, p, slot, current=current_state
                    )
                else:
                    before = current_state
                undo_op.append(
                    {
                        "seq": slot,
                        "type": entry_type,
                        "path": str(p),
                        "before": before,
                        "status": journal.ENTRY_DONE,
                    }
                )
                slot += 1

        # (f) Execute revert
        report = revert.revert_locked(env, target_op, runner, force=force)

        # (g) Mark undo_op status DONE
        undo_op.set_status(journal.DONE)

        try:
            journal.prune(env.state_dir)
        except OSError:
            pass

        # (h) Count remaining
        remaining = len(journal.list_undoable(env.state_dir))

        # (i) Return UndoResult
        return UndoResult(
            op_id=target_op.id,
            label=target_op.label,
            reverted_entries=report.reverted,
            recovered=recovered,
            undo_op_id=undo_op.id,
            remaining=remaining,
        )
