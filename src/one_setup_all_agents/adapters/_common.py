"""Logic that two or more adapters share, identical for every tool that uses it.

Everything here is a pure planner or reader: nothing writes (docs/decisions/0008). What is shared:

- the whole-file ownership rule for a tool's global instructions file;
- the per-skill link planner and the skills-directory reader (docs/decisions/0007);
- two small helpers (`sha256_hex`, `is_absolute`).

Anything that depends on a tool's own format stays in that tool's module.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..engine import fsops
from ..engine.errors import BackupError
from ..model import (
    INSTRUCTIONS_KEY,
    Conflict,
    EditFile,
    ItemRef,
    Kind,
    Link,
    Op,
    Owned,
    SkillRef,
)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_absolute(value: str, platform: str) -> bool:
    """Is `value` an absolute path in the style of `platform` (not of the machine running this)?"""
    pure = PureWindowsPath if platform == "windows" else PurePosixPath
    return pure(value).is_absolute()


# -- instructions: one file, owned as one item ---------------------------------------------------


def plan_whole_file_instructions(tool: str, path: Path, desired: str, owned: Owned) -> list[Op]:
    """Make `path` hold exactly `desired`. A file that exists, is not owned and differs is a
    `Conflict`; an equal file is left alone, owned or not."""
    new = desired.encode("utf-8")
    old = path.read_bytes() if path.is_file() else None
    if old == new:
        return []
    if old is not None and INSTRUCTIONS_KEY not in owned:
        reason = "the file exists and was not created by all-agents; left alone"
        return [Conflict(tool, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY, reason, path)]
    return [
        EditFile(
            path=path,
            transform=lambda _old: new,
            expect_sha256=None if old is None else sha256_hex(old),
            description="write the shared instructions",
            claims=(ItemRef(tool, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY),),
        )
    ]


# -- skills: one link per skill ------------------------------------------------------------------


@dataclass(frozen=True)
class SkillState:
    kind: str  # absent | symlink | junction | copy | dir | file | other
    dest: Path
    marker: dict | None = None
    edited: bool = False  # a marked copy whose content no longer matches its marker

    def matches(self, source: Path) -> bool:
        """Is this exactly what a link / fresh copy of `source` would give?"""
        if self.kind in ("symlink", "junction"):
            return fsops.same_target(self.dest, source)
        if self.kind == "copy" and self.marker is not None and not self.edited:
            try:
                return fsops.tree_hash(source) == self.marker["tree_sha256"] and fsops.same_target(
                    self.marker["source"], source
                )
            except (BackupError, OSError):
                return False
        return False


def inspect_skill_dest(dest: Path) -> SkillState:
    kind = fsops.link_kind(dest)
    if kind != "dir":
        return SkillState(kind, dest)
    marker = fsops.read_marker(dest)
    if marker is None:
        return SkillState("dir", dest)
    try:
        edited = fsops.tree_hash(dest) != marker["tree_sha256"]
    except (BackupError, OSError):
        edited = True
    return SkillState("copy", dest, marker, edited)


def skill_problem(name: str, skill: SkillRef) -> str | None:
    if not name or name in (".", "..") or "/" in name or "\\" in name or os.sep in name:
        return f"{name!r} is not a usable skill folder name"
    if not Path(skill.path).is_dir():
        return "the source skill folder does not exist"
    return None


def plan_skill_links(
    tool: str, directory: Path, desired: Sequence[SkillRef], owned: Owned
) -> list[Op]:
    """One `Link` per desired skill into `directory`; only owned links change (ADR 0007)."""
    ops: list[Op] = []
    wanted = {skill.name: skill for skill in desired}
    for name, skill in wanted.items():
        dest = directory / name
        ref = ItemRef(tool, Kind.SKILLS, name)
        problem = skill_problem(name, skill)
        if problem:
            ops.append(Conflict(tool, Kind.SKILLS, name, problem, dest))
            continue
        state = inspect_skill_dest(dest)
        if state.kind == "absent":
            ops.append(Link(dest, skill.path, f"link skill {name}", True, claims=(ref,)))
        elif state.matches(skill.path):
            continue  # already as wanted, owned or not
        elif state.kind in ("symlink", "junction", "copy") and name in owned and not state.edited:
            ops.append(Link(dest, skill.path, f"relink skill {name}", True, claims=(ref,)))
        else:
            reason = (
                "this copy was edited after all-agents made it; left alone"
                if state.edited
                else "a skill with this name exists and was not created by all-agents"
            )
            ops.append(Conflict(tool, Kind.SKILLS, name, reason, dest))
    for name in sorted(owned - wanted.keys()):
        dest = directory / name
        ref = ItemRef(tool, Kind.SKILLS, name)
        state = inspect_skill_dest(dest)
        if state.kind == "absent":
            continue
        if state.kind in ("symlink", "junction", "copy") and not state.edited:
            ops.append(Link(dest, None, f"withdraw skill {name}", releases=(ref,)))
        else:
            reason = "the owned skill is no longer an unedited link or copy; left alone"
            ops.append(Conflict(tool, Kind.SKILLS, name, reason, dest))
    return ops


def read_skill_dir(directory: Path | None, ignore: Collection[str] = ()) -> list[SkillRef]:
    """The folders directly inside `directory` (links marked), minus the names in `ignore`."""
    if directory is None or not directory.is_dir():
        return []
    return [
        SkillRef(
            name=child.name,
            path=child,
            is_link=fsops.link_kind(child) in ("symlink", "junction"),
        )
        for child in sorted(directory.iterdir())
        if child.is_dir() and child.name not in ignore
    ]
