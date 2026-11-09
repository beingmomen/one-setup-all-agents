"""The engine: the only code that writes to disk.

Adapters return `Op` objects (see `one_setup_all_agents.model`). The engine turns a
plan into effects, and records every effect so that `undo` can reverse it:

    plan.py     merge the adapters' ops into one reviewable `Plan`; render it (dry run output)
    apply.py    lock, journal, run each op, verify, revert on failure; crash recovery
    revert.py   put an operation back from its journal (the low-level undo, with the drift check)
    atomic.py   crash-safe file writes (docs/decisions/0006)
    backup.py   capture and restore the "before" state of every file, link or copied tree
    journal.py  the single operation journal and ownership ledger (docs/decisions/0005)
    links.py    execute a `Link` op: junction, symlink, copy fallback, owned removal
    runcli.py   run a `RunCli` op through an injectable runner
    fsops.py    the non-atomic file-system operations (folders, links, copied trees), in one place
    lock.py     one engine run at a time per state directory
    errors.py   the engine's exceptions
    undo.py     the user-facing `undo` command (reverses operations using the journal)

The engine and public commands are fully implemented.
"""
