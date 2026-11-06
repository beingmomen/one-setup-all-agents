"""The engine: the only code that writes to disk.

Adapters return `Op` objects (see `one_setup_all_agents.model`). The engine turns a
plan into effects, and records every effect so that `undo` can reverse it:

    plan.py     merge the adapters' ops into one reviewable `Plan` (dry run output)
    apply.py    lock, snapshot, run each op, verify, roll back on failure
    atomic.py   crash-safe file writes (docs/decisions/0006)
    backup.py   store the "before" state of every file or link an op changes
    journal.py  the single operation journal and ownership ledger (docs/decisions/0005)
    undo.py     reverse an operation from its journal
    lock.py     one engine run at a time per state directory

Phase 0: every function is a documented stub that raises NotImplementedError.
"""
