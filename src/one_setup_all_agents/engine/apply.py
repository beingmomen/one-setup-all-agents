"""Apply a plan: lock, snapshot, execute, verify, roll back on failure.

Order of events for one run (docs/decisions/0005):

    1. take the engine lock (engine/lock.py)
    2. open a new operation in the journal (write-ahead: intent is recorded first)
    3. for each op: back up the "before" state, execute it, verify, mark it done
    4. on any failure: undo the ops already done in this run, report, keep the journal
    5. release the lock
"""

from __future__ import annotations

from ..model import Env, Plan


def apply_plan(env: Env, plan: Plan, *, label: str) -> str:
    """Execute `plan` and return the id of the journal operation that records it."""
    raise NotImplementedError("engine.apply.apply_plan is not implemented yet (Phase 3).")
