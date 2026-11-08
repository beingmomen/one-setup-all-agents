"""Crash-safe file writes. Rules are in docs/decisions/0006-atomic-writes.md.

The rules this module implements:

- write to a temporary file in the same directory as the real target, flush and fsync,
  then os.replace; the temporary file is removed on any failure
- if the target is a symlink, write through it to the real file; the link survives
  (a dangling symlink is an error)
- preserve the existing file's permission bits; a new file gets the umask default,
  or 0600 when it is marked sensitive
- optionally refuse to write when the file changed since it was planned (expect_sha256)
- on Windows, retry os.replace briefly on PermissionError (antivirus, indexers)
- on POSIX, fsync the directory after the replace

Line endings and a UTF-8 BOM are part of the bytes: the caller builds `data` from the
existing file's bytes (see `_core.apply.FileText`), so they are preserved there.
Not handled (documented limits): hard links are broken by the replace; ownership,
extended attributes and ACLs are not preserved.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
import uuid
from pathlib import Path

REPLACE_RETRIES = 5
REPLACE_BACKOFF = 0.05  # seconds, multiplied by the attempt number


class AtomicWriteError(OSError):
    """The write was refused or failed; the target is left as it was."""


class _Unset:
    def __repr__(self):
        return "UNSET"


UNSET = _Unset()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_target(path: Path) -> Path:
    """The real file a write to `path` replaces. Follows symlinks; a dangling link is an error."""
    path = Path(path)
    if path.is_symlink():
        real = Path(os.path.realpath(path))
        if not real.exists():
            raise AtomicWriteError(
                f"{path} is a symlink to a missing target; not writing through it"
            )
        if real.is_dir():
            raise AtomicWriteError(f"{path} is a symlink to a directory")
        return real
    return path


def _replace(tmp: Path, target: Path) -> None:
    attempts = REPLACE_RETRIES if sys.platform.startswith("win") else 1
    for attempt in range(1, attempts + 1):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(REPLACE_BACKOFF * attempt)


def _fsync_dir(directory: Path) -> None:
    if sys.platform.startswith("win"):
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(
    path: Path,
    data: bytes,
    *,
    sensitive: bool = False,
    mode: int | None = None,
    expect_sha256: str | None | _Unset = UNSET,
) -> None:
    """Write `data` to `path` atomically, following the rules in the module docstring.

    `expect_sha256`: when given, the target must currently hash to this value (None means
    "must not exist"); otherwise AtomicWriteError is raised and nothing is written.
    """
    target = resolve_target(Path(path))
    target.parent.mkdir(parents=True, exist_ok=True)

    existing_mode = None
    current = None
    if target.exists():
        existing_mode = stat.S_IMODE(target.stat().st_mode)
        if expect_sha256 is not UNSET:
            current = _sha256(target.read_bytes())
    if expect_sha256 is not UNSET and current != expect_sha256:
        raise AtomicWriteError(f"{target.name} changed since it was planned; nothing written")

    new_mode = (
        existing_mode
        if existing_mode is not None
        else (mode if mode is not None else (0o600 if sensitive else 0o666))
    )
    tmp = target.with_name(f".{target.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), new_mode
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not sys.platform.startswith("win"):
            if existing_mode is not None:
                os.chmod(tmp, existing_mode)
            elif mode is not None:
                os.chmod(tmp, mode)
        _replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(target.parent)
