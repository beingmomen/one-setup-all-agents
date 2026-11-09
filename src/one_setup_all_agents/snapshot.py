"""Safety snapshot of original files before first write (ADR 0017)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from .adapters.base import ToolAdapter
from .engine.atomic import resolve_target
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
                is_symlink = path.is_symlink()
                if is_symlink:
                    target = resolve_target(path)
                    if not target.exists():
                        messages.append(f"snapshot skipped broken symlink: {path}")
                        continue
                    if not target.is_file():
                        messages.append(f"snapshot skipped path that is not a regular file: {path}")
                        continue
                    data = target.read_bytes()
                else:
                    if not path.exists():
                        # A missing file is skipped silently without warning
                        continue
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
