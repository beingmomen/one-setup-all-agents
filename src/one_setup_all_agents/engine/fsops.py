"""The non-atomic file-system operations of the engine, all in one place.

Every write of file CONTENT goes through `engine/atomic.py` (docs/decisions/0006). Three things
have no atomic-write equivalent and live here and nowhere else:

- directory creation and removal (including the private `0700` state directories of 0002)
- links: a Windows directory junction (no administrator rights), a symlink elsewhere, and their
  removal (docs/decisions/0007, 0010)
- copied trees: the copy-plus-marker fallback of 0007, with a tree hash and a marker file

Nothing in here decides policy (ownership, what may be replaced): `links.py` and `apply.py` do.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .._core.fsutil import is_junction
from .atomic import atomic_write
from .errors import BackupError

IS_WINDOWS = sys.platform.startswith("win")

#: The marker file inside a folder that all-agents copied (docs/decisions/0007, 0010).
MARKER_NAME = ".all-agents-copy.json"
MARKER_FORMAT = "one-setup-all-agents/copy-marker"
MARKER_VERSION = 1

#: Cost guard for copied trees (docs/decisions/0010): a tree is hashed in full and, as a
#: before-state, copied in full. A skill folder is small; anything above these is refused up
#: front instead of making apply slow or filling the disk.
MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_TREE_FILES = 5000

_TMP_PREFIX = ".all-agents-tmp-"


# -- directories -------------------------------------------------------------------------------


def ensure_private_dir(path: Path) -> None:
    """Create `path` (and parents) and, on POSIX, make it `0700` (docs/decisions/0002)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(path, 0o700)


def missing_ancestors(path: Path) -> list[Path]:
    """The folders above `path` that do not exist yet, deepest first."""
    missing = []
    for parent in Path(path).parents:
        if parent.exists():
            break
        missing.append(parent)
    return missing


def make_dirs(directory: Path) -> None:
    os.makedirs(directory, exist_ok=True)


def remove_empty_dirs(dirs: list[Path]) -> None:
    """Remove each folder if it is empty (deepest first); a folder with content is left alone."""
    for directory in dirs:
        try:
            os.rmdir(directory)
        except OSError:
            pass


# -- link inspection ---------------------------------------------------------------------------


def link_kind(path: Path) -> str:
    """'absent', 'symlink', 'junction', 'dir' (a real folder), 'file' or 'other'."""
    text = str(path)
    if os.path.islink(text):
        return "symlink"
    if is_junction(text):
        return "junction"
    if not os.path.lexists(text):
        return "absent"
    if os.path.isdir(text):
        return "dir"
    if os.path.isfile(text):
        return "file"
    return "other"


def _strip_win_prefix(text: str) -> str:
    for prefix in ("\\\\?\\UNC\\", "\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            rest = text[len(prefix) :]
            return ("\\\\" + rest) if prefix.endswith("UNC\\") else rest
    return text


def read_link(path: Path) -> str:
    """Where a symlink or junction points, as stored (the Windows `\\\\?\\` prefix is removed)."""
    return _strip_win_prefix(os.readlink(str(path)))


def same_target(a: str | Path, b: str | Path) -> bool:
    """True when two link targets name the same place (case, `..`, drive prefix, symlinks)."""
    a, b = _strip_win_prefix(str(a)), _strip_win_prefix(str(b))
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.abspath(p)))  # noqa: E731
    if norm(a) == norm(b):
        return True
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def native_link_kind() -> str:
    """The kind of directory link this OS gets: a junction on Windows, a symlink elsewhere."""
    return "junction" if IS_WINDOWS else "symlink"


# -- link creation and removal -----------------------------------------------------------------


def create_junction_mklink(link: Path, target: Path) -> None:
    """Windows only: `mklink /J` through cmd.exe (argv list, no shell string)."""
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    done = subprocess.run(
        [comspec, "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        raise OSError(f"mklink /J failed with exit code {done.returncode}")


def create_junction(link: Path, target: Path) -> None:
    """Create a Windows directory junction `link` -> `target`. No administrator rights needed.

    `_winapi.CreateJunction` is the method (docs/decisions/0010): in-process, no quoting, no
    subprocess. It is not documented in the standard library, so if this interpreter lacks it
    the same junction is made with `mklink /J`.
    """
    try:
        import _winapi

        create = _winapi.CreateJunction
    except (ImportError, AttributeError):
        create_junction_mklink(link, target)
        return
    create(str(target), str(link))


def create_dir_link(link: Path, target: Path) -> None:
    """Make `link` a directory link to the existing folder `target` (junction or symlink)."""
    link, target = Path(os.path.abspath(link)), Path(os.path.abspath(target))
    if IS_WINDOWS:
        create_junction(link, target)
    else:
        os.symlink(target, link, target_is_directory=True)


def create_symlink(link: Path, target: str, *, directory: bool) -> None:
    """Re-create a symlink exactly as it was recorded (`target` is used verbatim)."""
    os.symlink(target, str(link), target_is_directory=directory)


def remove_link(path: Path) -> None:
    """Remove a symlink or junction itself, never the folder it points at."""
    kind = link_kind(path)
    if kind == "junction":
        os.rmdir(path)
    elif kind == "symlink":
        if IS_WINDOWS and os.path.isdir(path):
            os.rmdir(path)  # a directory symlink is removed like a folder on Windows
        else:
            os.unlink(path)
    else:
        raise BackupError(f"{path} is not a link; refusing to remove it")


# -- copied trees ------------------------------------------------------------------------------


def _is_link_entry(entry: os.DirEntry) -> bool:
    return entry.is_symlink() or is_junction(entry.path)


def tree_hash(root: Path) -> str:
    """A hash of a folder's content: every folder and file name (sorted) and every file's bytes.

    The marker file at the top is not part of it. File modes are not part of it (a copied tree
    is compared by content). A link inside the tree is refused, and so is a tree above
    `MAX_TREE_FILES` or `MAX_TREE_BYTES`: the cost guard of docs/decisions/0010.
    """
    digest = hashlib.sha256()
    totals = {"files": 0, "bytes": 0}

    def walk(directory: str, prefix: str) -> None:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda e: e.name)
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            if rel == MARKER_NAME:
                continue
            if _is_link_entry(entry):
                raise BackupError(f"{entry.path} is a link inside a copied tree; not supported")
            if entry.is_dir(follow_symlinks=False):
                digest.update(f"D {rel}\n".encode())
                walk(entry.path, f"{rel}/")
                continue
            size, inner = 0, hashlib.sha256()
            with open(entry.path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    totals["bytes"] += len(chunk)
                    if totals["bytes"] > MAX_TREE_BYTES:
                        raise BackupError(f"{root} is larger than {MAX_TREE_BYTES} bytes")
                    inner.update(chunk)
            totals["files"] += 1
            if totals["files"] > MAX_TREE_FILES:
                raise BackupError(f"{root} has more than {MAX_TREE_FILES} files")
            digest.update(f"F {rel} {size} {inner.hexdigest()}\n".encode())

    walk(str(root), "")
    return digest.hexdigest()


def tree_modes(root: Path) -> dict[str, int]:
    """POSIX permission bits of every file in a tree, by relative path. Empty on Windows."""
    if IS_WINDOWS:
        return {}
    modes: dict[str, int] = {}
    base = Path(root)
    for current, _dirs, files in os.walk(base):
        for name in files:
            full = Path(current) / name
            modes[full.relative_to(base).as_posix()] = os.stat(full).st_mode & 0o7777
    return modes


def apply_tree_modes(root: Path, modes: dict[str, int]) -> None:
    for rel, mode in modes.items():
        target = Path(root) / rel
        if target.is_file():
            os.chmod(target, mode)


def copy_tree(src: Path, dest: Path, *, private: bool = False) -> None:
    """Copy a folder to a path that does not exist. `private`: folders 0700, files 0600 on POSIX.

    A link inside the source is refused (see `tree_hash`).
    """
    os.mkdir(dest, 0o700 if private else 0o777)
    with os.scandir(src) as scan:
        entries = sorted(scan, key=lambda e: e.name)
    for entry in entries:
        if _is_link_entry(entry):
            raise BackupError(f"{entry.path} is a link inside a copied tree; not supported")
        target = Path(dest) / entry.name
        if entry.is_dir(follow_symlinks=False):
            copy_tree(Path(entry.path), target, private=private)
            continue
        shutil.copyfile(entry.path, target)
        if private:
            if not IS_WINDOWS:
                os.chmod(target, 0o600)
        else:
            shutil.copymode(entry.path, target)


def _rmtree_onerror(func, path, _exc) -> None:
    os.chmod(path, 0o700)  # a read-only file blocks removal on Windows
    func(path)


def remove_tree(path: Path) -> None:
    """Remove a folder and everything in it."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda func, p, exc: _rmtree_onerror(func, p, exc))
    else:
        shutil.rmtree(path, onerror=_rmtree_onerror)


def rename_dir(src: Path, dest: Path) -> None:
    """Rename a folder to a path that does not exist, with the short Windows retry of 0006."""
    attempts = 5 if IS_WINDOWS else 1
    for attempt in range(1, attempts + 1):
        try:
            os.rename(src, dest)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(0.05 * attempt)


def temp_sibling(path: Path) -> Path:
    """A not-yet-existing path next to `path`, so a rename into place stays on one filesystem."""
    return Path(path).with_name(f"{_TMP_PREFIX}{Path(path).name}-{uuid.uuid4().hex[:8]}")


def marker_bytes(source: Path, digest: str) -> bytes:
    doc = {
        "format": MARKER_FORMAT,
        "version": MARKER_VERSION,
        "source": str(source),
        "tree_sha256": digest,
    }
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def read_marker(directory: Path) -> dict | None:
    """The marker of a copied folder, or None when the folder has no valid marker."""
    try:
        doc = json.loads((Path(directory) / MARKER_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != MARKER_FORMAT:
        return None
    if not isinstance(doc.get("tree_sha256"), str):
        return None
    return {"source": str(doc.get("source", "")), "tree_sha256": doc["tree_sha256"]}


def make_marked_copy(src: Path, dest: Path) -> str:
    """Copy folder `src` to the absent path `dest` with a marker; return the tree hash.

    The copy is built beside `dest` and renamed into place, so `dest` is either absent or whole.
    """
    digest = tree_hash(src)
    tmp = temp_sibling(dest)
    try:
        copy_tree(src, tmp)
        if tree_hash(tmp) != digest:
            raise BackupError(f"{src} changed while it was being copied")
        atomic_write(tmp / MARKER_NAME, marker_bytes(Path(src), digest))
        rename_dir(tmp, dest)
    except BaseException:
        if os.path.lexists(tmp):
            remove_tree(tmp)
        raise
    return digest


def restore_tree(payload: Path, dest: Path, modes: dict[str, int]) -> None:
    """Rebuild a copied folder at the absent path `dest` from a backup payload folder."""
    tmp = temp_sibling(dest)
    try:
        copy_tree(payload, tmp)
        apply_tree_modes(tmp, modes)
        rename_dir(tmp, dest)
    except BaseException:
        if os.path.lexists(tmp):
            remove_tree(tmp)
        raise
