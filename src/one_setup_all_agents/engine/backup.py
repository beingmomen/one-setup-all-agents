"""Capture and restore the state of a path.

Layout and format are in docs/decisions/0005-journal-backup-ledger.md. A path is described as
one of these states (a dict with a `state` key, stored as `before` and `after` in the journal):

    {"state": "absent"}
    {"state": "file", "sha256", "mode"}                      (+ "backup" in a before-state)
    {"state": "symlink", "target"}
    {"state": "dir_link", "kind": "junction" | "symlink", "target"}
    {"state": "copy_tree", "tree_sha256", "marker": {...}}   (+ "backup", "modes" in a before-state)

`capture` records the state of a path and, for a file or a copied tree, writes its content into
the operation's `files/NNNN` payload (fsynced, `0600` in a `0700` folder on POSIX). `restore`
puts a path back to a recorded state. Backup contents are never printed: errors name the path
and the problem only.

A real folder that all-agents did not create (no copy marker) has no state: `describe` raises
`BackupError`, and the engine never replaces or deletes it (docs/decisions/0007).
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from . import fsops
from .atomic import atomic_write
from .errors import BackupError
from .journal import op_dir

BACKUP_DIR = "files"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def backup_ref(slot: int) -> str:
    """The journal's reference to a payload: `files/0001` (relative to the operation folder)."""
    return f"{BACKUP_DIR}/{slot:04d}"


def payload_path(state_dir: Path, op_id: str, ref: str) -> Path:
    return op_dir(state_dir, op_id) / ref


def describe(path: Path) -> dict:
    """The current state of `path`, without writing anything."""
    path = Path(path)
    kind = fsops.link_kind(path)
    if kind == "absent":
        return {"state": "absent"}
    if kind in ("symlink", "junction"):
        target = fsops.read_link(path)
        if kind == "junction":
            return {"state": "dir_link", "kind": "junction", "target": target}
        if os.path.isdir(path):
            return {"state": "dir_link", "kind": "symlink", "target": target}
        return {"state": "symlink", "target": target}
    if kind == "file":
        data = path.read_bytes()
        return {
            "state": "file",
            "sha256": _sha256(data),
            "mode": stat.S_IMODE(os.stat(path).st_mode),
        }
    if kind == "dir":
        marker = fsops.read_marker(path)
        if marker is None:
            raise BackupError(
                f"{path} is a real folder that all-agents did not create (no copy marker); "
                "it is never replaced or removed"
            )
        return {"state": "copy_tree", "tree_sha256": fsops.tree_hash(path), "marker": marker}
    raise BackupError(f"{path} is not a regular file, folder or link")


def states_match(a: dict | None, b: dict | None) -> bool:
    """True when two recorded states describe the same thing.

    A file's permission bits are compared on POSIX only, and only when both states know them.
    """
    if a is None or b is None:
        return a is b
    if a["state"] != b["state"]:
        return False
    state = a["state"]
    if state == "absent":
        return True
    if state == "file":
        if a["sha256"] != b["sha256"]:
            return False
        modes = (a.get("mode"), b.get("mode"))
        return fsops.IS_WINDOWS or None in modes or modes[0] == modes[1]
    if state == "symlink":
        return fsops.same_target(a["target"], b["target"])
    if state == "dir_link":
        return a["kind"] == b["kind"] and fsops.same_target(a["target"], b["target"])
    if state == "copy_tree":
        return a["tree_sha256"] == b["tree_sha256"]
    raise BackupError(f"unknown state {state!r}")


def capture(
    state_dir: Path, op_id: str, path: Path, slot: int, *, current: dict | None = None
) -> dict:
    """Record the state of `path` for operation `op_id`; return it as a journal `before` state.

    For a file or a copied tree the content is also written to the payload `files/<slot>`.
    `current` can pass a state the caller just read with `describe`, to avoid hashing twice.
    """
    path = Path(path)
    state = dict(describe(path) if current is None else current)
    if state["state"] == "file":
        data = path.read_bytes()
        if _sha256(data) != state["sha256"]:
            raise BackupError(f"{path.name} changed while it was being backed up")
        ref = backup_ref(slot)
        target = payload_path(state_dir, op_id, ref)
        fsops.ensure_private_dir(target.parent)
        atomic_write(target, data, sensitive=True)
        state["backup"] = ref
    elif state["state"] == "copy_tree":
        ref = backup_ref(slot)
        target = payload_path(state_dir, op_id, ref)
        fsops.ensure_private_dir(target.parent)
        fsops.copy_tree(path, target, private=True)
        if fsops.tree_hash(target) != state["tree_sha256"]:
            raise BackupError(f"{path.name} changed while it was being backed up")
        state["backup"] = ref
        state["modes"] = fsops.tree_modes(path)
    return state


def _remove_current(path: Path, current: dict) -> None:
    kind = current["state"]
    if kind == "file":
        os.unlink(path)
    elif kind in ("symlink", "dir_link"):
        fsops.remove_link(path)
    elif kind == "copy_tree":
        fsops.remove_tree(path)


def restore(state_dir: Path, op_id: str, path: Path, state: dict) -> None:
    """Put `path` back to the recorded `state`, then check that it really is.

    Whatever is at the path now is replaced, except a real folder without a marker (refused).
    A file is restored through the atomic write with its recorded permission bits.
    """
    path = Path(path)
    current = describe(path)
    if states_match(current, state):
        return
    wanted = state["state"]
    if not (wanted == "file" and current["state"] == "file"):
        _remove_current(path, current)
    if wanted == "absent":
        pass
    elif wanted == "file":
        data = payload_path(state_dir, op_id, state["backup"]).read_bytes()
        if _sha256(data) != state["sha256"]:
            raise BackupError(f"the backup of {path.name} is damaged; not restoring it")
        mode = state.get("mode")
        fsops.make_dirs(path.parent)
        atomic_write(path, data, sensitive=mode is not None and (mode & 0o077) == 0)
        if mode is not None and not fsops.IS_WINDOWS:
            os.chmod(path, mode)
    elif wanted == "symlink":
        fsops.make_dirs(path.parent)
        fsops.create_symlink(path, state["target"], directory=False)
    elif wanted == "dir_link":
        fsops.make_dirs(path.parent)
        if state["kind"] == "junction":
            if not fsops.IS_WINDOWS:
                raise BackupError(f"{path.name} was a junction; junctions exist on Windows only")
            fsops.create_junction(path, Path(state["target"]))
        else:
            fsops.create_symlink(path, state["target"], directory=True)
    elif wanted == "copy_tree":
        fsops.make_dirs(path.parent)
        payload = payload_path(state_dir, op_id, state["backup"])
        fsops.restore_tree(payload, path, state.get("modes", {}))
    else:
        raise BackupError(f"unknown state {wanted!r}")
    after = describe(path)
    if not states_match(after, state):
        raise BackupError(f"restoring {path.name} did not produce the recorded state")
