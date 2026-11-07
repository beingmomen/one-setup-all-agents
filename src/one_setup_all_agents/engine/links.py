"""Execute a `Link` op: junction, symlink or copy plus marker, and the removal of an owned link.

Rules (docs/decisions/0007, 0009, 0010):

- A link is made as a directory junction on Windows (no administrator rights) and as a symlink
  on Linux and macOS. The target must be an existing folder.
- If linking fails and the op has `allow_copy_fallback`, the folder is copied and a marker file
  with the tree hash is written inside it (`fsops.make_marked_copy`). Without the flag a failure
  is an error.
- Something that already sits at the link path is replaced or removed only if it is a link,
  junction or marked copy AND the ledger records the op's item as owned. A regular file, a real
  folder and anything unowned are never touched. A marked copy that was edited after it was made
  is also refused (its content would be lost).
- `Link(target=None)` removes the link; it needs `releases` and every released item owned.

This module decides and acts; `apply.py` does the journal around it. Nothing here is called by
an adapter.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..model import ItemRef, Link
from . import backup, fsops
from .errors import LinkError


def _triple(ref: ItemRef) -> tuple[str, str, str]:
    return (ref.tool, ref.kind.value, ref.key)


def expected_after(op: Link) -> dict:
    """The state the link path should have once `op` ran (the fallback is decided at run time)."""
    if op.target is None:
        return {"state": "absent"}
    return {
        "state": "dir_link",
        "kind": fsops.native_link_kind(),
        "target": os.path.abspath(op.target),
    }


def _require_owned(op: Link, owned: set[tuple[str, str, str]], what: str) -> None:
    claims = {_triple(r) for r in op.claims}
    releases = {_triple(r) for r in op.releases}
    if op.target is None:
        if not releases:
            raise LinkError(
                f"refusing to remove {op.link}: the op does not say which owned item it releases"
            )
        if not releases <= owned:
            raise LinkError(f"refusing to remove {op.link}: the ledger does not record it as owned")
    elif not ((claims | releases) & owned):
        raise LinkError(f"refusing to {what} {op.link}: the ledger does not record it as owned")


def check(op: Link, current: dict, owned: set[tuple[str, str, str]]) -> None:
    """Refuse the op (LinkError) unless it may act on what is at the link path now."""
    state = current["state"]
    if state == "file":
        raise LinkError(f"{op.link} is a regular file; refusing to replace it with a link")
    if op.target is not None and not os.path.isdir(op.target):
        raise LinkError(f"link target {op.target} is not an existing folder")
    if state == "absent":
        return
    if op.target is not None and backup.states_match(current, expected_after(op)):
        return  # already as wanted
    _require_owned(op, owned, "remove" if op.target is None else "replace")
    if state == "copy_tree" and current["tree_sha256"] != current["marker"]["tree_sha256"]:
        raise LinkError(
            f"{op.link} is a copy that was edited after all-agents made it; refusing to discard it"
        )


def execute(op: Link, current: dict, on_expected: Callable[[dict], None]) -> None:
    """Make the link path match `op`. `check` has already passed for `current`.

    `on_expected(state)` is called before the copy fallback starts, with the state that the copy
    will have, so the journal knows what to expect (write-ahead).
    """
    link = Path(op.link)
    state = current["state"]
    if op.target is None:
        if state == "absent":
            return
        _remove(link, state)
        return
    if backup.states_match(current, expected_after(op)):
        return
    if state != "absent":
        _remove(link, state)
    fsops.make_dirs(link.parent)
    try:
        fsops.create_dir_link(link, Path(op.target))
    except OSError as exc:
        if not op.allow_copy_fallback:
            raise LinkError(f"cannot link {op.link}: {type(exc).__name__}: {exc}") from exc
        digest = fsops.tree_hash(Path(op.target))
        on_expected(
            {
                "state": "copy_tree",
                "tree_sha256": digest,
                "marker": {"source": str(op.target), "tree_sha256": digest},
            }
        )
        fsops.make_marked_copy(Path(op.target), link)


def _remove(link: Path, state: str) -> None:
    if state == "copy_tree":
        fsops.remove_tree(link)
    else:
        fsops.remove_link(link)
