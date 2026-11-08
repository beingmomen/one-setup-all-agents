"""The single operation journal and ownership ledger.

One format for every kind of op (file edit, CLI call, link). The format, the write-ahead rule
and the `undo` guarantee are in docs/decisions/0005-journal-backup-ledger.md; the choices made
while building it (entry fields, the ownership delta, recovery) are in 0010.

State directory layout (docs/decisions/0002):

    ledger.json                  what the tool owns, and the list of operations
    ops/<op-id>/journal.json     per-operation entries, written before each step
    ops/<op-id>/files/NNNN       backup payloads referenced by the journal

Every file here is written with `engine/atomic.py` and is created `0600` on POSIX, in `0700`
folders (docs/decisions/0002). The functions do not take the engine lock: the caller holds it.

`owned` in the ledger is `{tool: {kind: [item key, ...]}}`. Inside the engine an item is the
triple `(tool, kind value, key)`.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from . import fsops
from .atomic import atomic_write
from .errors import JournalError

JOURNAL_FORMAT = "one-setup-all-agents/journal"
JOURNAL_VERSION = 1
LEDGER_FORMAT = "one-setup-all-agents/ledger"
LEDGER_VERSION = 1

OPEN, DONE, FAILED, UNDONE = "open", "done", "failed", "undone"
STATUSES = (OPEN, DONE, FAILED, UNDONE)

ENTRY_PLANNED, ENTRY_DONE, ENTRY_FAILED = "planned", "done", "failed"

Triple = tuple[str, str, str]  # (tool, kind value, item key)


# -- layout ------------------------------------------------------------------------------------


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / "ledger.json"


def ops_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "ops"


def op_dir(state_dir: Path, op_id: str) -> Path:
    return ops_dir(state_dir) / op_id


def journal_path(state_dir: Path, op_id: str) -> Path:
    return op_dir(state_dir, op_id) / "journal.json"


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_op_id(now: datetime | None = None) -> str:
    """UTC timestamp plus a short random suffix, for example `20261106T101500Z-a1b2`."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(2)}"


# -- json files --------------------------------------------------------------------------------


def _write_json(path: Path, doc: dict) -> None:
    data = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    atomic_write(path, data, sensitive=True)


def _read_json(path: Path, fmt: str, version: int) -> dict:
    try:
        doc = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise JournalError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    if not isinstance(doc, dict) or doc.get("format") != fmt:
        raise JournalError(f"{path.name} is not a {fmt} file")
    if doc.get("version") != version:
        raise JournalError(f"{path.name} has version {doc.get('version')}; expected {version}")
    return doc


# -- the ledger --------------------------------------------------------------------------------


def _empty_ledger() -> dict:
    return {"format": LEDGER_FORMAT, "version": LEDGER_VERSION, "owned": {}, "operations": []}


def read_ledger(state_dir: Path) -> dict:
    """The ledger, or an empty one when none exists yet."""
    path = ledger_path(state_dir)
    if not path.is_file():
        return _empty_ledger()
    return _read_json(path, LEDGER_FORMAT, LEDGER_VERSION)


def write_ledger(state_dir: Path, ledger: dict) -> None:
    fsops.ensure_private_dir(Path(state_dir))
    _write_json(ledger_path(state_dir), ledger)


def update_ledger(state_dir: Path, mutate: Callable[[dict], None]) -> None:
    """Read the ledger, let `mutate` change it, write it back atomically."""
    ledger = read_ledger(state_dir)
    mutate(ledger)
    write_ledger(state_dir, ledger)


def owned(state_dir: Path) -> dict[str, dict[str, list[str]]]:
    """The items this tool owns, `{tool: {kind: [keys]}}`: `sync` touches nothing else."""
    return read_ledger(state_dir)["owned"]


def owned_for(state_dir: Path, tool: str, kind: str) -> frozenset[str]:
    """The item keys of one tool and kind, as the `Owned` an adapter receives."""
    return frozenset(owned(state_dir).get(tool, {}).get(kind, ()))


def owned_items(state_dir: Path) -> set[Triple]:
    """Every owned item as a `(tool, kind, key)` triple."""
    return {
        (tool, kind, key)
        for tool, kinds in owned(state_dir).items()
        for kind, keys in kinds.items()
        for key in keys
    }


def _owned_table(items: Iterable[Triple]) -> dict[str, dict[str, list[str]]]:
    table: dict[str, dict[str, list[str]]] = {}
    for tool, kind, key in sorted(set(items)):
        table.setdefault(tool, {}).setdefault(kind, []).append(key)
    return table


def set_owned(state_dir: Path, tool: str, kind: str, keys: Iterable[str]) -> None:
    """Replace the owned keys of one tool and kind.

    This is the ledger reconciliation of docs/decisions/0009 (adopting an item that already
    equals the wanted state, dropping one that vanished). `init` will use it; the contract suite
    uses it to simulate a ledger that forgot or never knew an item.
    """
    items = {t for t in owned_items(state_dir) if (t[0], t[1]) != (tool, kind)}
    items |= {(tool, kind, key) for key in keys}
    update_ledger(state_dir, lambda ledger: ledger.__setitem__("owned", _owned_table(items)))


def replace_owned(state_dir: Path, items: Iterable[Triple], op_id: str, status: str) -> None:
    """In ONE ledger write: set `owned` to `items` and the index status of operation `op_id`."""
    table = _owned_table(items)

    def mutate(ledger: dict) -> None:
        ledger["owned"] = table
        _set_index_status(ledger, op_id, status)

    update_ledger(state_dir, mutate)


def revert_owned(state_dir: Path, delta: dict | None) -> None:
    """Undo an operation's ownership delta: remove what it added, put back what it removed.

    Set arithmetic, so it is safe to repeat (a crash during recovery can run it twice).
    """
    if not delta:
        return
    added = {tuple(t) for t in delta.get("added", ())}
    removed = {tuple(t) for t in delta.get("removed", ())}
    items = (owned_items(state_dir) - added) | removed
    update_ledger(state_dir, lambda ledger: ledger.__setitem__("owned", _owned_table(items)))


def _set_index_status(ledger: dict, op_id: str, status: str, **fields) -> None:
    for item in ledger["operations"]:
        if item["id"] == op_id:
            item["status"] = status
            item.update(fields)
            return
    entry = {"id": op_id, "status": status}
    entry.update(fields)
    ledger["operations"].append(entry)


def list_operations(state_dir: Path) -> list[dict]:
    """The operations of the ledger index, newest first: `[{id, label, created, status}]`."""
    return sorted(read_ledger(state_dir)["operations"], key=lambda o: o["id"], reverse=True)


def list_undoable(state_dir: Path) -> list[dict]:
    """The undoable operations of the ledger index, newest first.

    Returns index entries with status == "done" and kind != "undo".
    """
    return [
        op
        for op in list_operations(state_dir)
        if op.get("status") == DONE and op.get("kind", "apply") != "undo"
    ]


# -- one operation -----------------------------------------------------------------------------


class Operation:
    """One operation's journal, kept in memory and saved after every change."""

    def __init__(self, state_dir: Path, data: dict) -> None:
        self.state_dir = Path(state_dir)
        self.data = data

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def label(self) -> str:
        return self.data["label"]

    @property
    def kind(self) -> str:
        return self.data.get("kind", "apply")

    @property
    def undoes(self) -> str | None:
        return self.data.get("undoes")

    @property
    def entries(self) -> list[dict]:
        return self.data["entries"]

    @property
    def directory(self) -> Path:
        return op_dir(self.state_dir, self.id)

    def save(self) -> None:
        _write_json(journal_path(self.state_dir, self.id), self.data)

    def append(self, entry: dict) -> None:
        """Add an entry and save (this is the write-ahead save when the entry is `planned`)."""
        self.entries.append(entry)
        self.save()

    def set_status(self, status: str, **fields) -> None:
        """Set the operation status (journal first, then the ledger index) with extra fields."""
        if status not in STATUSES:
            raise JournalError(f"unknown operation status {status!r}")
        self.data["status"] = status
        self.data.update(fields)
        self.save()
        update_ledger(self.state_dir, lambda ledger: _set_index_status(ledger, self.id, status))


def begin(
    state_dir: Path,
    label: str,
    *,
    kind: str = "apply",
    undoes: str | None = None,
    now: datetime | None = None,
) -> Operation:
    """Open a new operation (status `open`) and return it."""
    state_dir = Path(state_dir)
    fsops.ensure_private_dir(state_dir)
    fsops.ensure_private_dir(ops_dir(state_dir))
    while True:
        op_id = new_op_id(now)
        if not op_dir(state_dir, op_id).exists():
            break
    fsops.ensure_private_dir(op_dir(state_dir, op_id))
    created = now_iso(now)
    data = {
        "format": JOURNAL_FORMAT,
        "version": JOURNAL_VERSION,
        "id": op_id,
        "label": label,
        "created": created,
        "status": OPEN,
        "kind": kind,
        "undoes": undoes,
        "entries": [],
    }
    op = Operation(state_dir, data)
    op.save()
    update_ledger(
        state_dir,
        lambda ledger: _set_index_status(
            ledger,
            op_id,
            OPEN,
            label=label,
            created=created,
            kind=kind,
            undoes=undoes,
        ),
    )
    return op


def load(state_dir: Path, op_id: str) -> Operation:
    """Load one operation's journal."""
    path = journal_path(state_dir, op_id)
    if not path.is_file():
        raise JournalError(f"no journal for operation {op_id}")
    return Operation(state_dir, _read_json(path, JOURNAL_FORMAT, JOURNAL_VERSION))


def find_unfinished(state_dir: Path) -> list[str]:
    """Ids of operations that need recovery, oldest first.

    Found by scanning `ops/`, not the ledger index: a crash can leave a journal the index does
    not know yet. An operation needs recovery when it is `open` (a crash) or `failed` with
    `pending_revert` (an automatic revert that could not finish).
    """
    base = ops_dir(state_dir)
    if not base.is_dir():
        return []
    found = []
    for child in sorted(base.iterdir()):
        path = child / "journal.json"
        if not path.is_file():
            continue
        data = _read_json(path, JOURNAL_FORMAT, JOURNAL_VERSION)
        if data["status"] == OPEN or (data["status"] == FAILED and data.get("pending_revert")):
            found.append(data["id"])
    return found


# -- retention pruning -------------------------------------------------------------------------

_OP_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")


def _is_terminal(state_dir: Path, op_entry: dict) -> bool:
    """True if operation has reached a terminal status and is not pending recovery."""
    status = op_entry.get("status")
    if status not in (DONE, UNDONE, FAILED):
        return False
    op_id = op_entry.get("id")
    if not op_id:
        return False
    # If failed, check whether it has pending_revert
    jpath = journal_path(state_dir, op_id)
    if jpath.is_file():
        try:
            data = _read_json(jpath, JOURNAL_FORMAT, JOURNAL_VERSION)
            if data.get("status") == OPEN or data.get("pending_revert"):
                return False
        except JournalError:
            return False
    return True


def prune(state_dir: Path, keep: int = 20) -> list[str]:
    """Prune old operations from disk and index, keeping at most keep terminal operations.

    Raises ValueError if keep < 1.
    Terminal statuses are done, undone, and ailed without pending_revert.
    Open and pending operations are never deleted. The newest operation is never deleted.
    Deletes oldest operations first until at most keep terminal operations remain.
    Symlinks or junctions inside ops/ named like an operation id are unlinked without
    following them.
    Returns the list of pruned operation IDs.
    """
    if keep < 1:
        raise ValueError("keep must be >= 1")

    state_dir = Path(state_dir)
    ledger = read_ledger(state_dir)
    operations = ledger.get("operations", [])
    if not operations:
        return []

    # Newest operation overall (by id) must never be pruned
    newest_op_id = max((op["id"] for op in operations if "id" in op), default=None)

    # Candidates = index entries whose directories exist, terminal status only
    candidates: list[dict] = []
    for entry in operations:
        op_id = entry.get("id")
        if not op_id:
            continue
        target = op_dir(state_dir, op_id)
        if fsops.link_kind(target) == "absent":
            continue
        if _is_terminal(state_dir, entry):
            candidates.append(entry)

    # Sort oldest first
    candidates.sort(key=lambda o: o["id"])

    # If count of terminal operations exceeds keep, remove the oldest
    excess = len(candidates) - keep
    if excess <= 0:
        return []

    to_prune = candidates[:excess]
    # Guard: never prune the newest operation
    if newest_op_id:
        to_prune = [op for op in to_prune if op.get("id") != newest_op_id]

    if not to_prune:
        return []

    ops_base = ops_dir(state_dir).resolve()
    pruned_ids: list[str] = []

    for op in to_prune:
        op_id = op["id"]
        if not _OP_ID_RE.match(op_id):
            continue
        target = op_dir(state_dir, op_id)
        kind = fsops.link_kind(target)
        if kind in ("symlink", "junction"):
            fsops.remove_link(target)
            pruned_ids.append(op_id)
        elif kind == "dir":
            if target.resolve().parent != ops_base:
                continue
            fsops.remove_tree(target)
            pruned_ids.append(op_id)
        elif kind == "file":
            target.unlink()
            pruned_ids.append(op_id)

    if pruned_ids:
        pruned_set = set(pruned_ids)
        update_ledger(
            state_dir,
            lambda led: led.__setitem__(
                "operations",
                [o for o in led.get("operations", []) if o.get("id") not in pruned_set],
            ),
        )

    return pruned_ids
