"""Safety snapshot of original files before first write (ADR 0017)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from .adapters.base import ToolAdapter
from .model import Detection, EditFile, Env, Scope


def snapshot_ops(
    env: Env,
    adapters: Mapping[str, ToolAdapter],
    detections: Sequence[Detection],
) -> tuple[list[EditFile], list[str]]:
    """Build snapshot no-op EditFile operations for installed tools' existing config files.

    Returns (ops, messages). Ops are no-op EditFile entries with expect_sha256 set to
    the current file hash, so the engine's write-ahead backup captures the original files.
    """
    ops: list[EditFile] = []
    messages: list[str] = []
    seen_paths: set[Path] = set()

    for detection in detections:
        if not detection.installed:
            continue
        adapter = adapters.get(detection.tool)
        if adapter is None:
            continue
        for path in adapter.snapshot_files(env, Scope.GLOBAL):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                if not os.path.lexists(path):
                    continue
                if os.path.islink(path):
                    target_str = os.path.realpath(path)
                    target_path = Path(target_str)
                    if not target_path.exists():
                        messages.append(f"snapshot skipped broken symlink: {path}")
                        continue
                    if not target_path.is_file():
                        messages.append(f"snapshot skipped path that is not a regular file: {path}")
                        continue
                    data = target_path.read_bytes()
                else:
                    if not path.is_file():
                        messages.append(f"snapshot skipped path that is not a regular file: {path}")
                        continue
                    data = path.read_bytes()

                h = hashlib.sha256(data).hexdigest()
                ops.append(
                    EditFile(
                        path=path,
                        transform=lambda old: old if old is not None else b"",
                        expect_sha256=h,
                        description="snapshot (no change)",
                        sensitive=False,
                    )
                )
            except Exception as exc:
                messages.append(f"snapshot skipped unreadable path {path}: {exc}")

    return ops, messages
