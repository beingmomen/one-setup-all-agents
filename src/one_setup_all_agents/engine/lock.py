"""One engine run at a time per state directory.

Planned: an exclusive lock file created with O_CREAT | O_EXCL that records the
owner's process id and start time. A lock whose process is gone is stale and is
replaced; a live one makes the run fail fast with a clear message.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def engine_lock(state_dir: Path) -> Iterator[None]:
    """Hold the engine lock for the duration of the `with` block."""
    raise NotImplementedError("engine.lock.engine_lock is not implemented yet (Phase 3).")
    yield  # pragma: no cover  (makes this a generator function)
