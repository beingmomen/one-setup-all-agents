"""Logic that two or more adapters share, identical for every tool that uses it.

Everything here is a pure planner or reader: nothing writes (docs/decisions/0008). What is shared:

- the whole-file ownership rule for a tool's global instructions file;
- the per-skill link planner and the skills-directory reader (docs/decisions/0007);
- small helpers (`sha256_hex`, `is_absolute`, `decode_text` / `encode_text` for a text config);
- finding a tool's binary without starting it, and reading a version from command output
  (docs/decisions/0014).

Anything that depends on a tool's own format stays in that tool's module.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..engine import fsops
from ..engine.errors import BackupError
from ..model import (
    INSTRUCTIONS_KEY,
    SOURCE_KNOWN_LOCATION,
    SOURCE_PATH,
    Conflict,
    EditFile,
    Env,
    ItemRef,
    Kind,
    Link,
    Op,
    Owned,
    SkillRef,
)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_text(raw: bytes) -> tuple[str, bool, bool]:
    """(text with LF newlines, had a BOM, used CRLF). For editing a text config file in place."""
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    return body.decode("utf-8").replace("\r\n", "\n"), bom, b"\r\n" in body


def encode_text(text: str, bom: bool, crlf: bool) -> bytes:
    """The inverse of `decode_text`: restores the BOM and the CRLF line endings."""
    out = text.replace("\n", "\r\n") if crlf else text
    return (b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8")


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


# -- binaries and versions (docs/decisions/0014) -------------------------------------------------

DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


@dataclass(frozen=True)
class BinaryMatch:
    """A binary that exists, and how it was found (`SOURCE_PATH` or `SOURCE_KNOWN_LOCATION`)."""

    path: Path
    source: str


def _environ_get(environ: Mapping[str, str], name: str, platform: str) -> str:
    """One variable of `environ`. Windows names are case-insensitive (`Path` is `PATH`)."""
    if name in environ:
        return environ[name]
    if platform == "windows":
        wanted = name.casefold()
        for key, value in environ.items():
            if key.casefold() == wanted:
                return value
    return ""


def path_dirs(env: Env) -> list[Path]:
    """The folders of `PATH` in `env.environ`, in order. Empty and relative entries are skipped
    (a relative one means the current folder, which an adapter must not depend on)."""
    separator = ";" if env.platform == "windows" else ":"
    out: list[Path] = []
    for raw in _environ_get(env.environ, "PATH", env.platform).split(separator):
        entry = raw.strip().strip('"') if env.platform == "windows" else raw
        # absolute in the style of `env.platform`, or of the machine running this code (a test
        # may label an Env "windows" while its temporary folders are POSIX paths)
        absolute = bool(entry) and (is_absolute(entry, env.platform) or Path(entry).is_absolute())
        if absolute and Path(entry) not in out:
            out.append(Path(entry))
    return out


def known_bin_dirs(env: Env) -> list[Path]:
    """Folders where a command line tool commonly lands but that may not be on `PATH`.

    Derived from `env` only. Windows: the user's `.local/bin`, the npm global folder (`APPDATA/npm`)
    and the app execution aliases (`LOCALAPPDATA/Microsoft/WindowsApps`). Linux and macOS:
    `~/.local/bin`, `~/.npm-global/bin`, `~/.bun/bin`, `/usr/local/bin` and, on macOS, the Homebrew
    folder. An adapter adds the folders of its own installer through `extra_dirs`.
    """
    home = Path(env.home)
    if env.platform == "windows":
        appdata = _environ_get(env.environ, "APPDATA", env.platform)
        local = _environ_get(env.environ, "LOCALAPPDATA", env.platform)
        roaming = Path(appdata) if appdata else home / "AppData" / "Roaming"
        local_dir = Path(local) if local else home / "AppData" / "Local"
        return [
            home / ".local" / "bin",
            roaming / "npm",
            local_dir / "Microsoft" / "WindowsApps",
        ]
    dirs = [home / ".local" / "bin", home / ".npm-global" / "bin", home / ".bun" / "bin"]
    dirs.append(Path("/usr/local/bin"))
    if env.platform == "macos":
        dirs.append(Path("/opt/homebrew/bin"))
    return dirs


def _is_launchable(path: Path, platform: str) -> bool:
    try:
        if not path.is_file():
            return False
        return platform == "windows" or os.access(path, os.X_OK)
    except OSError:  # for example an unreadable app execution alias
        return False


def _candidates(directory: Path, name: str, env: Env) -> list[Path]:
    if env.platform != "windows":
        return [directory / name]
    pathext = _environ_get(env.environ, "PATHEXT", env.platform) or DEFAULT_PATHEXT
    exts = [e if e.startswith(".") else "." + e for e in pathext.split(";") if e.strip()]
    out = []
    for ext in exts:
        for variant in (ext.lower(), ext):
            candidate = directory / (name + variant)
            if candidate not in out:
                out.append(candidate)
    return out


def find_binary(env: Env, name: str, *, extra_dirs: Sequence[Path] = ()) -> BinaryMatch | None:
    """Find `name` without starting anything: first on `PATH` of `env.environ`, then in the known
    install folders (`known_bin_dirs` and `extra_dirs`). The OS and `os.environ` are not consulted.

    On Windows the extensions come from `PATHEXT` of `env.environ` (default `.COM;.EXE;.BAT;.CMD`),
    so a `.cmd` shim is found; the bare name never matches there (the extensionless script an npm
    install leaves beside its shim cannot be run). On Linux and macOS the file must be executable.
    """
    on_path = path_dirs(env)
    searches = [(directory, SOURCE_PATH) for directory in on_path]
    for directory in (*known_bin_dirs(env), *extra_dirs):
        if Path(directory) not in on_path:
            searches.append((Path(directory), SOURCE_KNOWN_LOCATION))
    for directory, source in searches:
        for candidate in _candidates(directory, name, env):
            if _is_launchable(candidate, env.platform):
                return BinaryMatch(candidate, source)
    return None


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_VERSION = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+)+(?:[-+][0-9A-Za-z][0-9A-Za-z.+-]*)?)")


def parse_version(output: str, *, prefer: str | None = None) -> str | None:
    """The first version-looking token of `output`, without a leading `v` (`demo v2.2.6` is
    `2.2.6`, `2.1.3 (Some Tool)` is `2.1.3`). A version has at least two numeric parts.

    `prefer` is a regex with one group, tried first, for a tool whose banner also holds other
    numbers; it falls back to the first token when the preferred shape is absent.
    """
    text = _ANSI.sub("", output)
    if prefer:
        match = re.search(prefer, text)
        if match and match.group(1):
            return parse_version(match.group(1))
    match = _VERSION.search(text)
    return match.group(1) if match else None
