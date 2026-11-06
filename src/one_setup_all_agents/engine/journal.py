"""The single operation journal and ownership ledger.

One format for every kind of op (file edit, CLI call, link). The format, the
write-ahead rule and the `undo` guarantee are in
docs/decisions/0005-journal-backup-ledger.md.

State directory layout (see docs/decisions/0002):

    ledger.json                  what the tool owns, and the list of operations
    ops/<op-id>/journal.json     per-operation entries, written before each step
    ops/<op-id>/files/           backup payloads referenced by the journal
"""

from __future__ import annotations

from pathlib import Path

JOURNAL_FORMAT = "one-setup-all-agents/journal"
JOURNAL_VERSION = 1


def begin(state_dir: Path, label: str) -> str:
    """Open a new operation and return its id."""
    raise NotImplementedError("engine.journal.begin is not implemented yet (Phase 3).")


def record(state_dir: Path, op_id: str, entry: dict) -> None:
    """Append an entry to the operation's journal (write-ahead, then fsync)."""
    raise NotImplementedError("engine.journal.record is not implemented yet (Phase 3).")


def list_operations(state_dir: Path) -> list[dict]:
    """Return the known operations, newest first."""
    raise NotImplementedError("engine.journal.list_operations is not implemented yet (Phase 3).")


def owned(state_dir: Path) -> dict:
    """Return the ledger of items this tool owns (so `sync` never touches the rest)."""
    raise NotImplementedError("engine.journal.owned is not implemented yet (Phase 3).")
