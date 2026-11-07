"""One engine run at a time per state directory.

The lock is an operating-system lock on the file `<state-dir>/lock`, stdlib only:

- POSIX (Linux, macOS): `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on the whole file.
- Windows: `msvcrt.locking(fd, LK_NBLCK, 1)` on the first byte.

It is never waited for forever. `engine_lock` tries once (or for `timeout` seconds, default 0)
and raises `EngineLockedError` naming the holder, so a second concurrent apply fails with a
clear message instead of hanging.

A stale lock cannot exist. The operating system drops the lock when the holding process ends,
however it ends (crash, kill, power loss and restart), so the file left behind means nothing:
there is no pid-liveness probing and nothing to delete by hand. The file holds an informational
line (pid and start time) after byte 0, which the next holder overwrites; it is only read to
make the error message helpful. A crashed run is not a lock problem but an `open` operation in
the journal, which `apply.recover_operation` handles (docs/decisions/0010).

Unverified: advisory locks on a network filesystem (NFS, SMB). The state directory is
machine-local by design (docs/decisions/0002).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import fsops
from .errors import EngineLockedError

LOCK_NAME = "lock"
_INFO_OFFSET = 16  # holder info lives after the locked first byte, readable by other processes

if sys.platform.startswith("win"):
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _read_holder(path: Path) -> str:
    """A short description of the current holder, from its informational line."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return "another process"
    try:
        os.lseek(fd, _INFO_OFFSET, os.SEEK_SET)
        info = json.loads(os.read(fd, 512).decode("utf-8", "replace").strip("\x00 \n") or "{}")
        return f"pid {int(info['pid'])}, started {info['started']}"
    except (OSError, ValueError, KeyError, TypeError):
        return "another process"
    finally:
        os.close(fd)


def _write_holder(fd: int) -> None:
    info = {"pid": os.getpid(), "started": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, b" ".ljust(_INFO_OFFSET))
    os.write(fd, json.dumps(info).encode("utf-8"))
    os.ftruncate(fd, _INFO_OFFSET + len(json.dumps(info)))


@contextmanager
def engine_lock(state_dir: Path, *, timeout: float = 0.0) -> Iterator[None]:
    """Hold the engine lock for the duration of the `with` block.

    Raises `EngineLockedError` if another run holds it and it does not free up within `timeout`
    seconds (default: do not wait). Not re-entrant: take it once, at the top of a run.
    """
    state_dir = Path(state_dir)
    fsops.ensure_private_dir(state_dir)
    path = state_dir / LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise EngineLockedError(
                    f"another all-agents run holds the lock of {state_dir} "
                    f"({_read_holder(path)}). Wait for it to finish, then run again. "
                    "The lock is released automatically when that process ends, so a crashed "
                    "run never leaves a stale lock."
                )
            time.sleep(0.05)
    except BaseException:
        os.close(fd)
        raise
    try:
        _write_holder(fd)
        yield
    finally:
        _unlock(fd)
        os.close(fd)
