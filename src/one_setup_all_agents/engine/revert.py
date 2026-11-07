"""Put an operation's changes back from its journal (the engine under `undo`).

Guarantee and drift rules are in docs/decisions/0005; the details of this implementation are in
0010. This is the LOW-LEVEL revert used by the automatic revert of a failed run, by crash
recovery and, later, by the `undo` command (Phase 5, not built here: no `--force`, no stack
rule, no listing, no retention).

Behavior:

- Entries are reversed in the opposite order of execution.
- Drift preflight (the default and, for now, the only mode): every path of every entry that is
  still to be reverted is compared with the state the journal recorded. If any path differs
  (the tool or the user changed it since), NOTHING is changed, the paths are reported, and
  `RevertError` is raised.
- A `RunCli` entry is reversed by running its `inverse_argv`. Its external state cannot be
  checked, so it is skipped by the preflight. An entry whose command reported failure (a non-zero
  exit code) is not inverted: the command is assumed to have had no effect. An entry whose
  outcome is unknown (planned when a crash hit, or a runner exception) has its inverse tried,
  and a failing inverse is tolerated and recorded, because the command may never have run.
- Each entry is marked `reverted` in the journal as soon as it is put back, so an interrupted
  revert resumes where it stopped.
- The operation's ownership delta is reversed in the ledger (set arithmetic, safe to repeat).
- Folders the operation created are removed when empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .._core.mask import mask_args, mask_text
from ..model import Env
from . import backup, fsops, journal
from .errors import BackupError, RevertError
from .lock import engine_lock
from .runcli import Runner, default_runner

_PATH_TYPES = ("edit_file", "link")


@dataclass
class RevertReport:
    """What a revert did, by entry sequence number."""

    op_id: str
    reverted: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)  # nothing to put back
    uncertain: list[int] = field(default_factory=list)  # inverse tried, outcome not certain


def _entry_path(entry: dict) -> Path:
    return Path(entry.get("real_path") or entry["path"])


def _allowed_states(entry: dict) -> list[dict]:
    """The states a path may be in now without that counting as drift."""
    allowed = [s for s in (entry.get("after"), entry.get("before")) if s is not None]
    if entry["status"] != journal.ENTRY_DONE and entry["type"] == "link":
        allowed.append({"state": "absent"})  # a link replace removes first, then creates
    return allowed


def drifted_paths(entries: list[dict]) -> list[Path]:
    """Paths whose current state is neither what the journal recorded nor what it started from."""
    drifted = []
    for entry in entries:
        if entry["type"] not in _PATH_TYPES or entry.get("before") is None:
            continue
        if entry.get("effect") == "none":
            continue  # the write was refused: the path was never changed by this entry
        path = _entry_path(entry)
        try:
            current = backup.describe(path)
        except BackupError:
            drifted.append(path)
            continue
        if not any(backup.states_match(current, state) for state in _allowed_states(entry)):
            drifted.append(path)
    return drifted


def _run_inverse(entry: dict, runner: Runner, report: RevertReport, op_id: str) -> None:
    result = entry.get("result") or {}
    status = entry["status"]
    code = result.get("exit_code")
    if status == journal.ENTRY_FAILED and code not in (None, 0):
        report.skipped.append(entry["seq"])  # the command said it failed: no effect to undo
        return
    uncertain = status == journal.ENTRY_PLANNED or (status == journal.ENTRY_FAILED and code is None)
    shown = mask_args(entry["inverse_argv"][:3])
    try:
        exit_code = runner(list(entry["inverse_argv"]))
    except Exception as exc:
        if not uncertain:
            raise RevertError(
                f"entry {entry['seq']}: the inverse command {shown} could not run "
                f"({type(exc).__name__})",
                op_id=op_id,
                partial=bool(report.reverted),
            ) from exc
        exit_code = None
    if exit_code == 0:
        report.reverted.append(entry["seq"])
        return
    if not uncertain:
        raise RevertError(
            f"entry {entry['seq']}: the inverse command {shown} exited with code {exit_code}",
            op_id=op_id,
            partial=bool(report.reverted),
        )
    entry["revert_note"] = "the inverse did not succeed; the original command may never have run"
    report.uncertain.append(entry["seq"])


def _revert_path_entry(env: Env, op_id: str, entry: dict, report: RevertReport) -> None:
    before = entry.get("before")
    if before is None:
        report.skipped.append(entry["seq"])  # failed before anything was captured or changed
        return
    path = _entry_path(entry)
    if entry.get("effect") == "none" or backup.states_match(backup.describe(path), before):
        report.skipped.append(entry["seq"])
    else:
        backup.restore(env.state_dir, op_id, path, before)
        report.reverted.append(entry["seq"])
    fsops.remove_empty_dirs([Path(p) for p in entry.get("created_dirs", ())])


def revert_entries(env: Env, op: journal.Operation, runner: Runner) -> RevertReport:
    """Revert every entry of `op` not yet reverted. The caller holds the engine lock.

    Raises `RevertError`: with `drifted` set and nothing changed, or `partial` when it stopped
    half way. Does not set the final operation status; the caller does.
    """
    report = RevertReport(op.id)
    pending = [e for e in op.entries if not e.get("reverted")]
    drifted = drifted_paths(pending)
    if drifted:
        names = ", ".join(str(p) for p in drifted)
        raise RevertError(
            f"operation {op.id}: these paths changed since it ran, nothing was reverted: {names}",
            op_id=op.id,
            drifted=drifted,
        )
    op.data["pending_revert"] = True
    op.save()
    for entry in sorted(pending, key=lambda e: e["seq"], reverse=True):
        try:
            if entry["type"] == "run_cli":
                _run_inverse(entry, runner, report, op.id)
            else:
                _revert_path_entry(env, op.id, entry, report)
        except RevertError:
            op.save()
            raise
        except Exception as exc:
            op.save()
            raise RevertError(
                f"operation {op.id}: entry {entry['seq']} "
                f"({mask_text(entry.get('description', ''))}) could not be put back: "
                f"{type(exc).__name__}: {mask_text(str(exc))}",
                op_id=op.id,
                partial=bool(report.reverted),
            ) from exc
        entry["reverted"] = True
        op.save()
    journal.revert_owned(env.state_dir, op.data.get("owned_delta"))
    op.data["pending_revert"] = False
    op.save()
    return report


def revert_operation(
    env: Env, op_id: str, *, run_cli: Runner | None = None, lock_timeout: float = 0.0
) -> RevertReport:
    """Put back everything operation `op_id` changed, taking the engine lock.

    Works on an operation that is `done` (it becomes `undone`), `open` after a crash (it becomes
    `failed`, marked `recovered`), or `failed` with a revert that did not finish. Anything else
    is refused. Raises `RevertError` on drift (nothing changed) or when it stops half way.
    """
    runner = run_cli or default_runner(env)
    with engine_lock(env.state_dir, timeout=lock_timeout):
        op = journal.load(env.state_dir, op_id)
        return revert_locked(env, op, runner)


def revert_locked(env: Env, op: journal.Operation, runner: Runner) -> RevertReport:
    """`revert_operation` for a caller that already holds the lock."""
    status = op.status
    unfinished = status == journal.FAILED and op.data.get("pending_revert")
    if status not in (journal.OPEN, journal.DONE) and not unfinished:
        raise RevertError(f"operation {op.id} is {status}; there is nothing to revert", op_id=op.id)
    report = revert_entries(env, op, runner)
    if status == journal.DONE:
        op.set_status(journal.UNDONE, reverted_at=journal.now_iso())
    else:
        op.set_status(journal.FAILED, recovered=True, pending_revert=False)
    return report
