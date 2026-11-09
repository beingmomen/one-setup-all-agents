"""Apply a plan: lock, journal, execute, verify, revert on failure.

Order of events for one run (docs/decisions/0005, 0010):

    1. take the engine lock (engine/lock.py); a second run fails fast
    2. refuse to start if an earlier operation is unfinished (`recover_operation` fixes it)
    3. open a new operation in the journal
    4. for each executable op, in order:
         capture the "before" state and write the backup payload (fsynced)
         save the entry as `planned`          <- write-ahead: from here a crash can be put right
         execute it (atomic write, link, or the CLI through the runner)
         verify it against the expected "after", save the entry as `done`
    5. record the ownership delta, update the ledger, mark the operation `done`
    6. on ANY failure: revert the entries already done in this run, newest first, mark the
       operation `failed`, keep the journal, and raise `ApplyError`
    7. release the lock

`Unsupported` and `Conflict` entries are never executed. The engine never reads or edits the
adapters' source of truth; it only runs the ops it is given.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path

from .._core.mask import mask_args, mask_text
from ..model import Conflict, EditFile, Env, ItemRef, Kind, Link, Op, Plan, RunCli
from . import backup, fsops, journal, links
from .atomic import atomic_write, resolve_target
from .errors import (
    ApplyError,
    EngineError,
    OpenOperationError,
    PlanError,
    VerificationError,
)
from .lock import engine_lock
from .plan import runcli_has_secret, secret_conflict_reason
from .revert import revert_entries, revert_locked
from .runcli import Runner, default_runner


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _refs(refs: tuple[ItemRef, ...]) -> list[dict]:
    return [{"tool": r.tool, "kind": r.kind.value, "key": r.key} for r in refs]


def _validate(ops: list) -> None:
    """Refuse a plan the engine cannot run, before anything is touched (docs/decisions/0005)."""
    for op in ops:
        if isinstance(op, RunCli):
            if not list(op.argv) or not all(isinstance(a, str) for a in op.argv):
                raise PlanError(f"RunCli '{mask_text(op.description)}' has no usable argv")
            if not list(op.inverse_argv) or not all(isinstance(a, str) for a in op.inverse_argv):
                raise PlanError(
                    f"RunCli '{mask_text(op.description)}' has no inverse; an op without an "
                    "inverse is not allowed"
                )
        elif isinstance(op, Link):
            if not str(op.link):
                raise PlanError("a Link op has no link path")
        elif isinstance(op, EditFile):
            if not callable(op.transform):
                raise PlanError(f"EditFile '{mask_text(op.description)}' has no transform")


class _Run:
    """The state of one apply run: the open operation, the runner and the working ownership."""

    def __init__(
        self,
        env: Env,
        op: journal.Operation,
        runner: Runner,
        adopt: Sequence[ItemRef] = (),
    ) -> None:
        self.env = env
        self.state_dir = env.state_dir
        self.op = op
        self.runner = runner
        self.initial_owned = journal.owned_items(env.state_dir)
        self.owned = set(self.initial_owned)
        for ref in adopt:
            self.owned.add((ref.tool, ref.kind.value, ref.key))

    # -- shared pieces ---------------------------------------------------------------------

    def _entry(self, kind: str, op, **fields) -> dict:
        return {
            "seq": len(self.op.entries) + 1,
            "type": kind,
            "status": journal.ENTRY_PLANNED,
            "description": mask_text(op.description),
            **fields,
            "claims": _refs(op.claims),
            "releases": _refs(op.releases),
        }

    def _fail_entry(self, entry: dict, exc: BaseException) -> None:
        entry["status"] = journal.ENTRY_FAILED
        entry["error"] = mask_text(f"{type(exc).__name__}: {exc}")
        if not any(e is entry for e in self.op.entries):
            self.op.entries.append(entry)
        self.op.save()

    def _done(self, entry: dict, op) -> None:
        entry["status"] = journal.ENTRY_DONE
        self.op.save()
        for ref in op.claims:
            self.owned.add((ref.tool, ref.kind.value, ref.key))
        for ref in op.releases:
            self.owned.discard((ref.tool, ref.kind.value, ref.key))

    # -- the three executable ops ------------------------------------------------------------

    def execute(self, op) -> None:
        if isinstance(op, EditFile):
            self._edit(op)
        elif isinstance(op, Link):
            self._link(op)
        else:
            self._run_cli(op)

    def _edit(self, op: EditFile) -> None:
        entry = self._entry("edit_file", op, path=str(op.path))
        try:
            real = resolve_target(Path(op.path))  # follows a symlink; a dangling one is an error
            entry["real_path"] = str(real)
            before = backup.capture(self.state_dir, self.op.id, real, entry["seq"])
            entry["before"] = before
            if before["state"] == "absent":
                old = None
                current_sha = None
            elif before["state"] == "file":
                old = real.read_bytes()
                current_sha = before["sha256"]
            else:
                raise VerificationError(f"{real} is not a regular file")
            if current_sha != op.expect_sha256:
                raise EngineError(f"{real.name} changed since it was planned; nothing written")
            new = op.transform(old)
            if not isinstance(new, bytes):
                raise EngineError(f"the transform for {real.name} must return bytes")
            mode = before.get("mode", op.mode or (0o600 if op.sensitive else None))
            expected = {"state": "file", "sha256": _sha256(new), "mode": mode}
            entry["after"] = expected
            entry["created_dirs"] = [str(p) for p in fsops.missing_ancestors(real)]
        except BaseException as exc:
            self._fail_entry(entry, exc)
            raise
        self.op.append(entry)  # write-ahead: `planned`, with `before` and its payload on disk
        try:
            fsops.make_dirs(real.parent)
            if expected["sha256"] != current_sha:
                try:
                    atomic_write(
                        real, new, sensitive=op.sensitive, mode=mode, expect_sha256=op.expect_sha256
                    )
                except Exception:
                    # a refused or failed atomic write leaves the target as it was (ADR 0006)
                    entry["effect"] = "none"
                    raise
            observed = backup.describe(real)
            entry["after"] = observed
            if not backup.states_match(observed, expected):
                entry["expected_after"] = expected
                raise VerificationError(f"{real.name} does not match what was written")
        except BaseException as exc:
            self._fail_entry(entry, exc)
            raise
        self._done(entry, op)

    def _link(self, op: Link) -> None:
        entry = self._entry("link", op, path=str(op.link))
        try:
            current = backup.describe(Path(op.link))
            links.check(op, current, self.owned)
            entry["before"] = backup.capture(
                self.state_dir, self.op.id, Path(op.link), entry["seq"], current=current
            )
            entry["after"] = links.expected_after(op)
            entry["created_dirs"] = (
                [str(p) for p in fsops.missing_ancestors(Path(op.link))]
                if op.target is not None
                else []
            )
        except BaseException as exc:
            self._fail_entry(entry, exc)
            raise
        self.op.append(entry)
        try:

            def expect(after: dict) -> None:
                entry["after"] = after
                self.op.save()  # write-ahead again: the copy fallback is about to start

            links.execute(op, current, expect)
            observed = backup.describe(Path(op.link))
            if not backup.states_match(observed, entry["after"]):
                entry["expected_after"] = entry["after"]
                entry["after"] = observed
                raise VerificationError(f"{Path(op.link).name} does not match the requested link")
            entry["after"] = observed
        except BaseException as exc:
            self._fail_entry(entry, exc)
            raise
        self._done(entry, op)

    def _run_cli(self, op: RunCli) -> None:
        entry = self._entry(
            "run_cli",
            op,
            argv=list(op.argv),
            inverse_argv=list(op.inverse_argv),
            before=None,
            after=None,
        )
        self.op.append(entry)
        shown = mask_args(list(op.argv)[:3])
        try:
            code = self.runner(list(op.argv))
        except BaseException as exc:  # no exit code is recorded: the outcome is unknown
            self._fail_entry(entry, exc)
            raise
        entry["result"] = {"exit_code": code}
        if code != 0:
            self._fail_entry(entry, VerificationError(f"command {shown} exited with code {code}"))
            raise VerificationError(f"command {shown} exited with code {code}")
        self._done(entry, op)

    # -- the end of a successful run ---------------------------------------------------------

    def finish(self) -> None:
        added = sorted(self.owned - self.initial_owned)
        removed = sorted(self.initial_owned - self.owned)
        self.op.data["owned_delta"] = {"added": added, "removed": removed}
        self.op.save()  # the delta is on disk before the ledger changes, so a revert can undo it
        journal.replace_owned(self.state_dir, self.owned, self.op.id, journal.DONE)
        self.op.set_status(journal.DONE)


def apply_plan(
    env: Env,
    plan: Plan,
    *,
    label: str,
    adopt: Sequence[ItemRef] = (),
    run_cli: Runner | None = None,
    lock_timeout: float = 0.0,
    pinned: bool = False,
    warn: Callable[[str], None] | None = None,
) -> str | None:
    """Execute `plan` and return the id of the journal operation that records it.

    Returns None, and records nothing, when the plan has nothing to execute and no items to
    adopt. Raises: `EngineLockedError` if another run holds the lock, `OpenOperationError` if an
    earlier run is unfinished, `PlanError` for a plan that cannot run, and `ApplyError` if an op
    failed (the entries already done were reverted; `ApplyError.reverted` says whether that
    finished).
    """
    new_ops: list[Op] = []
    secret_refusal: str | None = None
    for op in plan.ops:
        if isinstance(op, RunCli) and runcli_has_secret(op):
            ref = next(iter(op.claims + op.releases), None)
            tool = ref.tool if ref else "engine"
            kind = ref.kind if ref else Kind.MCP
            key = ref.key if ref else mask_text(op.description)
            item = key
            new_ops.append(
                Conflict(
                    tool=tool,
                    kind=kind,
                    key=key,
                    reason=secret_conflict_reason(item),
                )
            )
            if secret_refusal is None:
                secret_refusal = f"refusing RunCli for {item}: command looks like a secret"
        else:
            new_ops.append(op)

    if secret_refusal is not None:
        raise PlanError(secret_refusal)

    working_plan = Plan(new_ops)
    ops = working_plan.executable
    if not ops and not adopt:
        return None
    if ops:
        _validate(ops)
    runner = run_cli or default_runner(env)
    with engine_lock(env.state_dir, timeout=lock_timeout):
        unfinished = journal.find_unfinished(env.state_dir)
        if unfinished:
            raise OpenOperationError(unfinished)
        op = journal.begin(env.state_dir, label, pinned=pinned)
        run = _Run(env, op, runner, adopt=adopt)
        try:
            for item in ops:
                run.execute(item)
            run.finish()
        except BaseException as exc:
            _fail_run(env, op, runner, exc)
        prune_warnings = journal.prune(env.state_dir)
        if warn:
            for w in prune_warnings:
                warn(w)
        return op.id


def _fail_run(env: Env, op: journal.Operation, runner: Runner, exc: BaseException) -> None:
    """Revert what this run did, mark the operation failed, and raise (always raises)."""
    reason = mask_text(f"{type(exc).__name__}: {exc}")
    where = f"operation {op.id} failed ({reason})"
    try:
        revert_entries(env, op, runner)
    except Exception as revert_exc:
        op.set_status(journal.FAILED, error=reason, pending_revert=True)
        raise ApplyError(
            f"{where}; the automatic revert did not finish: {revert_exc}. The journal is kept at "
            f"{journal.journal_path(env.state_dir, op.id)}; fix the cause and call "
            "engine.revert.revert_operation or engine.apply.recover_operation",
            op_id=op.id,
            reverted=False,
        ) from exc
    op.set_status(journal.FAILED, error=reason, pending_revert=False)
    if not isinstance(exc, Exception):
        raise exc  # KeyboardInterrupt and the like: reverted, then let it through
    raise ApplyError(
        f"{where}; everything this run changed was put back. The journal is kept at "
        f"{journal.journal_path(env.state_dir, op.id)}",
        op_id=op.id,
        reverted=True,
    ) from exc


def _all_entries_reverted(env: Env, target_op: journal.Operation) -> bool:
    if not target_op.entries:
        return False
    for entry in target_op.entries:
        if entry.get("reverted"):
            continue
        entry_type = entry.get("type")
        if entry_type in ("edit_file", "link"):
            before = entry.get("before")
            if before is None or entry.get("effect") == "none":
                continue
            path = Path(entry.get("real_path") or entry.get("path"))
            try:
                current = backup.describe(path)
            except Exception:
                return False
            if not backup.states_match(current, before):
                return False
        else:
            return False
    return True


def _recapture_safety_copies(env: Env, op: journal.Operation, target_op: journal.Operation) -> None:
    pending = [e for e in target_op.entries if not e.get("reverted")]
    for entry in pending:
        entry_type = entry.get("type")
        if entry_type in ("edit_file", "link"):
            p = Path(entry.get("real_path") or entry.get("path"))
            try:
                current_state = backup.describe(p)
            except Exception:
                continue
            if current_state.get("state") in ("file", "copy_tree"):
                existing = None
                for e in op.entries:
                    if e.get("path") == str(p) or e.get("real_path") == str(p):
                        existing = e
                        break
                if existing is not None:
                    if not backup.states_match(current_state, existing.get("before")):
                        slot = max((e.get("seq", 0) for e in op.entries), default=0) + 1
                        before = backup.capture(
                            env.state_dir, op.id, p, slot, current=current_state
                        )
                        existing["before"] = before
                        op.save()
                else:
                    slot = max((e.get("seq", 0) for e in op.entries), default=0) + 1
                    before = backup.capture(env.state_dir, op.id, p, slot, current=current_state)
                    op.append(
                        {
                            "seq": slot,
                            "type": entry_type,
                            "path": str(p),
                            "before": before,
                            "after": entry.get("before"),
                            "status": journal.ENTRY_DONE,
                        }
                    )
                    op.save()


def recover_locked(env: Env, runner: Runner, *, force: bool = False) -> list[str]:
    """Put right every unfinished operation for a caller that already holds the lock."""
    recovered = []
    for op_id in reversed(journal.find_unfinished(env.state_dir)):
        op = journal.load(env.state_dir, op_id)
        if op.kind == "undo" and op.undoes:
            target_op = journal.load(env.state_dir, op.undoes)
            if target_op.status == journal.UNDONE or _all_entries_reverted(env, target_op):
                if target_op.status != journal.UNDONE:
                    target_op.set_status(journal.UNDONE, reverted_at=journal.now_iso())
                journal.revert_owned(env.state_dir, target_op.data.get("owned_delta"))
                target_op.data["pending_revert"] = False
                target_op.save()
                op.set_status(journal.DONE)
                op.data["pending_revert"] = False
                op.save()
                recovered.append(op_id)
            else:
                if force:
                    _recapture_safety_copies(env, op, target_op)
                revert_locked(env, target_op, runner, force=force)
                op.set_status(journal.DONE)
                op.data["pending_revert"] = False
                op.save()
                recovered.append(op_id)
        else:
            revert_locked(env, op, runner, force=force)
            recovered.append(op_id)
    return recovered


def recover_operation(
    env: Env, *, force: bool = False, run_cli: Runner | None = None, lock_timeout: float = 0.0
) -> list[str]:
    """Put right every unfinished operation (a crash left it `open`), newest first.

    Returns the ids that were recovered. Each one is reverted from its journal with the same
    drift preflight as any revert: if a path changed since, nothing is changed for that
    operation and `RevertError` is raised, naming the paths. Nothing to recover returns [].
    """
    runner = run_cli or default_runner(env)
    with engine_lock(env.state_dir, timeout=lock_timeout):
        return recover_locked(env, runner, force=force)
