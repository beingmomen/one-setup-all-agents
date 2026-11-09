"""The user-facing `undo` command: reverse operations from the journal (Phase 5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .._core.mask import mask_text
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
    reverted_descriptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
        recovered = recover_locked(env, runner, force=force)

        # Check if an unfinished undo operation was recovered
        recovered_undo_op = None
        for r_id in recovered:
            try:
                r_op = journal.load(env.state_dir, r_id)
                if r_op.kind == "undo" and r_op.undoes:
                    recovered_undo_op = r_op
                    break
            except Exception:
                pass

        if recovered_undo_op is not None:
            target_op = journal.load(env.state_dir, recovered_undo_op.undoes)
            remaining = len(journal.list_undoable(env.state_dir))
            reverted_seqs = [e["seq"] for e in target_op.entries if e.get("reverted")]
            prune_warnings = journal.prune(env.state_dir)
            return UndoResult(
                op_id=target_op.id,
                label=target_op.label,
                reverted_entries=reverted_seqs,
                recovered=recovered,
                undo_op_id=recovered_undo_op.id,
                remaining=remaining,
                reverted_descriptions=[
                    e.get("description") or f"entry #{e['seq']}"
                    for e in target_op.entries
                    if e.get("reverted") and e.get("description")
                ],
                warnings=list(prune_warnings),
            )

        # (b) Get undoable operations
        undoable = journal.list_undoable(env.state_dir)
        if not undoable:
            if recovered:
                return UndoResult(
                    op_id="",
                    label="",
                    reverted_entries=[],
                    recovered=recovered,
                    undo_op_id="",
                    remaining=0,
                )
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

        # Validate and describe every path BEFORE journal.begin
        # If any path causes BackupError, it is raised BEFORE the undo operation
        # is created on disk, keeping disk and journal untouched.
        path_states: list[tuple[dict, Path | None, dict | None]] = []
        for entry in pending:
            entry_type = entry.get("type")
            if entry_type in ("edit_file", "link"):
                p = Path(entry.get("real_path") or entry.get("path"))
                current_state = backup.describe(p)
                path_states.append((entry, p, current_state))
            else:
                path_states.append((entry, None, None))

        # (e) Create undo operation and record safety copies
        undo_op = journal.begin(
            env.state_dir,
            f"undo {target_op.id}",
            kind="undo",
            undoes=target_op.id,
        )

        slot = 1
        for entry, p, current_state in path_states:
            entry_type = entry.get("type")
            if entry_type in ("edit_file", "link") and p is not None and current_state is not None:
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
                        "after": entry.get("before"),
                        "status": journal.ENTRY_DONE,
                    }
                )
                slot += 1

        # (f) Execute revert
        try:
            target_op.data["pending_revert"] = True
            target_op.save()
            report = revert.revert_locked(env, target_op, runner, force=force)
            undo_op.set_status(journal.DONE)
            undo_op.data["pending_revert"] = False
            undo_op.save()
            target_op.data["pending_revert"] = False
            target_op.save()
        except BaseException as exc:
            undo_op.set_status(
                journal.FAILED,
                error=mask_text(f"{type(exc).__name__}: {exc}"),
                pending_revert=True,
            )
            target_op.data["pending_revert"] = True
            target_op.save()
            raise

        prune_warnings = journal.prune(env.state_dir)

        # (h) Count remaining
        remaining = len(journal.list_undoable(env.state_dir))

        reverted_set = set(report.reverted)
        reverted_descriptions = [
            e.get("description") or f"entry #{e['seq']}"
            for e in target_op.entries
            if e.get("seq") in reverted_set and e.get("description")
        ]

        # (i) Return UndoResult
        return UndoResult(
            op_id=target_op.id,
            label=target_op.label,
            reverted_entries=report.reverted,
            recovered=recovered,
            undo_op_id=undo_op.id,
            remaining=remaining,
            reverted_descriptions=reverted_descriptions,
            warnings=list(prune_warnings),
        )
