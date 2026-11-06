"""Crash-safe file writes. Rules are in docs/decisions/0006-atomic-writes.md.

Summary of the rules the implementation must follow:

- write to a temporary file in the same directory, flush and fsync, then os.replace
- if the target is a symlink, write through it to the real file; the link survives
- preserve the existing file's permission bits; new files use the umask default,
  or 0600 when the op is marked sensitive
- preserve the line-ending style and any UTF-8 BOM of the existing file
- on Windows, retry os.replace briefly on PermissionError (antivirus, indexers)
"""

from __future__ import annotations

from pathlib import Path


def atomic_write(path: Path, data: bytes, *, sensitive: bool = False) -> None:
    """Write `data` to `path` atomically, following the rules above."""
    raise NotImplementedError("engine.atomic.atomic_write is not implemented yet (Phase 3).")
