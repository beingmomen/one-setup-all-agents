"""OpenCode adapter: global scope, instructions + MCP servers + skills.

Adapters detect, read and plan; they never write (docs/decisions/0008). Everything here returns
`Op` objects; the engine runs them. Settled by docs/decisions/0009 (interface) and 0010 (engine).

Where OpenCode keeps things (global scope)
------------------------------------------

    <config>/opencode/AGENTS.md          instructions (owned as one item, `INSTRUCTIONS_KEY`)
    <config>/opencode/opencode.jsonc     MCP servers; `opencode.json` is used when only it exists
    <home>/.agents/skills/<name>         skills (one link per skill, docs/decisions/0007)

`<config>` is `$XDG_CONFIG_HOME` when it is set to an absolute path (read from `env.environ`, never
from the OS), else `<home>/.config`, on every platform. [verified on Windows with the real tool and
by the legacy prototype; the XDG rule follows the `xdg-basedir` convention OpenCode uses and is
UNVERIFIED on Linux and macOS with a real install.]

MCP entry format (the "v2" layout the prototype verified against OpenCode 2.x; the older "v1"
layout `mcp.<name>` is still read and edited in place):

    "mcp": {"servers": {"<name>": {
        "type": "local",  "command": ["npx", "-y", "pkg"], "cwd": "...",
        "environment": {"KEY": "literal", "OTHER": "{env:OTHER}"}, "disabled": true }}}
    "mcp": {"servers": {"<name>": {
        "type": "remote", "url": "https://...",
        "headers": {"Authorization": "Bearer {env:TOKEN}"} }}}

A secret is referenced by variable name only, with OpenCode's own `{env:NAME}` syntax. `disabled`
is the v2 switch (`enabled: false` in the v1 layout). The file is edited as text so comments, the
user's other keys, the BOM and CRLF line endings survive (`_core/jsonc_edit.py`).

What OpenCode cannot store is reported as `Unsupported` (never dropped silently) and left out of
the entry: `startup_timeout_sec`, and for a remote server `args`, `env`, `env_vars`, `cwd`. `auth`
is documentation only (OAuth is on by default) and is not written; `normalize_mcp` says exactly
which fields do not survive a round trip.

On Windows the launchers `npx`, `npm`, `pnpm` and `yarn` are written with the `.cmd` suffix
(plan decision; the prototype did this for project files only). Reading strips it again, so the
neutral view is the same on every platform. [`.cmd` on Windows: UNVERIFIED with a live tool.]

Not verified: whether OpenCode follows skill symlinks on Linux and macOS (0007 open point), and
anything on macOS at all (CI only).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import ClassVar

from .._core import jsonc, jsonc_edit
from .._core.mask import env_value_looks_secret
from ..engine import fsops
from ..engine.errors import BackupError
from ..model import (
    INSTRUCTIONS_KEY,
    Conflict,
    Detection,
    EditFile,
    Env,
    HeaderRef,
    Instructions,
    ItemRef,
    Kind,
    Link,
    McpServer,
    Op,
    Owned,
    Paths,
    Scope,
    SkillRef,
    Unsupported,
)
from .base import ToolAdapter

APP_DIR = "opencode"
INSTRUCTIONS_FILE = "AGENTS.md"
CONFIG_FILES = ("opencode.jsonc", "opencode.json")  # first existing wins; the first is created
SCHEMA_URL = "https://opencode.ai/config.json"

#: Launchers that Windows resolves only with a `.cmd` suffix (same set as the prototype).
WIN_SHIMS = frozenset({"npx", "npm", "pnpm", "yarn"})

#: Keys of an MCP entry that this adapter writes. Any other key of the user's entry is kept.
#: `enabled`/`env` are the v1 switch and an alias the prototype also read; they are ours so a
#: stale one cannot contradict what we write.
_OWNED_KEYS = frozenset(
    {"type", "command", "url", "environment", "env", "cwd", "disabled", "enabled"}
)

_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_ENV_REF = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
_HEADER_REF = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9._-]*) )?\{env:(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}$"
)


class ConfigUnreadable(ValueError):
    """The OpenCode config file exists but cannot be understood. Never overwritten."""


# -- paths -------------------------------------------------------------------------------------


def _is_absolute(value: str, platform: str) -> bool:
    pure = PureWindowsPath if platform == "windows" else PurePosixPath
    return pure(value).is_absolute()


def config_home(env: Env) -> Path:
    """`$XDG_CONFIG_HOME` (absolute only, like the XDG spec) or `<home>/.config`, any platform."""
    value = env.environ.get("XDG_CONFIG_HOME")
    if value and _is_absolute(value, env.platform):
        return Path(value)
    return Path(env.home) / ".config"


def config_dir(env: Env) -> Path:
    return config_home(env) / APP_DIR


def config_file(env: Env) -> Path:
    """The MCP config file: an existing `opencode.jsonc`, else an existing `opencode.json`, else
    the (not yet existing) `opencode.jsonc`."""
    base = config_dir(env)
    for name in CONFIG_FILES:
        if (base / name).is_file():
            return base / name
    return base / CONFIG_FILES[0]


def skills_dir(env: Env) -> Path:
    return Path(env.home) / ".agents" / "skills"


# -- reading the config file -------------------------------------------------------------------


def _decode_text(raw: bytes) -> tuple[str, bool, bool]:
    """(text with LF newlines, had a BOM, used CRLF)."""
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    return body.decode("utf-8").replace("\r\n", "\n"), bom, b"\r\n" in body


def _encode_text(text: str, bom: bool, crlf: bool) -> bytes:
    out = text.replace("\n", "\r\n") if crlf else text
    return (b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8")


def _entry_shaped(value: object) -> bool:
    return isinstance(value, dict) and (
        value.get("type") in ("local", "remote") or "command" in value or "url" in value
    )


def _locate(doc: dict) -> tuple[tuple[str, ...] | None, bool]:
    """(path of the object that holds the servers, is it the v1 layout). None = to be created."""
    if "mcp" not in doc:
        return None, False
    mcp = doc["mcp"]
    if not isinstance(mcp, dict):
        raise ConfigUnreadable('"mcp" is not an object')
    if "servers" in mcp:
        if not isinstance(mcp["servers"], dict):
            raise ConfigUnreadable('"mcp.servers" is not an object')
        return ("mcp", "servers"), False
    if any(_entry_shaped(v) for v in mcp.values()):
        return ("mcp",), True
    return None, False


@dataclass(frozen=True)
class _Config:
    path: Path
    raw: bytes | None  # None: the file does not exist
    members: dict  # every member of the servers object, by name (empty if there is none)
    container: tuple[str, ...] | None  # where the servers live; None: still to be created
    v1: bool

    @property
    def blank(self) -> bool:
        return self.raw is None or not self.raw.strip()


def load_config(path: Path) -> _Config:
    """Parse the file (read-only). Raises `ConfigUnreadable` with a reason that has no content."""
    path = Path(path)
    if not path.is_file():
        return _Config(path, None, {}, None, False)
    raw = path.read_bytes()
    if not raw.strip():
        return _Config(path, raw, {}, None, False)
    try:
        text, _bom, _crlf = _decode_text(raw)
        doc = jsonc.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigUnreadable(f"cannot parse {path.name} as JSON with comments ({exc})") from exc
    if not isinstance(doc, dict):
        raise ConfigUnreadable(f"{path.name}: the top level is not an object")
    container, v1 = _locate(doc)
    members: dict = {}
    if container is not None:
        node = doc
        for key in container:
            node = node[key]
        members = dict(node)
    return _Config(path, raw, members, container, v1)


# -- neutral model <-> OpenCode entry ----------------------------------------------------------


def _canon_command(command: object) -> object:
    """A command list with the Windows `.cmd` suffix removed from a launcher shim."""
    if isinstance(command, list) and command and isinstance(command[0], str):
        first = command[0]
        if first.lower().endswith(".cmd") and first[:-4].lower() in WIN_SHIMS:
            return [first[:-4], *command[1:]]
    return command


def _canon_server(server: McpServer) -> McpServer:
    if server.command:
        return replace(server, command=_canon_command([server.command])[0])
    return server


def _fatal(server: McpServer) -> str | None:
    """Why this server cannot be written to OpenCode at all (None: it can)."""
    if not server.url and not server.command:
        return "it has neither a command nor a url"
    for name in (*server.env_vars, *(h.env_var for h in server.headers)):
        if not _VAR.match(name):
            return f"variable name {name!r} cannot be written as {{env:NAME}}"
    for header in server.headers:
        if not _HEADER_NAME.match(header.name):
            return f"header name {header.name!r} is not a valid HTTP header name"
        if header.scheme is not None and not _SCHEME.match(header.scheme):
            return f"header scheme {header.scheme!r} cannot be written"
    return None


def _lost_fields(server: McpServer) -> list[str]:
    """Fields of a writable server that the OpenCode format has no place for."""
    lost = []
    if server.startup_timeout_sec is not None:
        lost.append("startup_timeout_sec")
    if server.url:
        lost += [
            f
            for f, present in (
                ("command", server.command),
                ("args", server.args),
                ("env", server.env),
                ("env_vars", server.env_vars),
                ("cwd", server.cwd),
            )
            if present
        ]
    elif server.headers:
        lost.append("headers")
    return lost


def encode_server(server: McpServer, *, windows: bool, v1: bool = False) -> dict:
    """The OpenCode entry for `server`. Holds variable NAMES only, never a secret value."""
    entry: dict = {}
    if server.url:
        entry["type"] = "remote"
        entry["url"] = server.url
        if server.headers:
            entry["headers"] = {
                h.name: (f"{h.scheme} " if h.scheme else "") + "{env:" + h.env_var + "}"
                for h in server.headers
            }
    else:
        command = server.command or ""
        if windows and command in WIN_SHIMS:
            command += ".cmd"
        entry["type"] = "local"
        entry["command"] = [command, *server.args]
        environment = dict(server.env)
        environment.update({name: "{env:" + name + "}" for name in server.env_vars})
        if environment:
            entry["environment"] = environment
        if server.cwd:
            entry["cwd"] = server.cwd
    if not server.enabled:
        if v1:
            entry["enabled"] = False
        else:
            entry["disabled"] = True
    return entry


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    return []


def decode_server(name: str, entry: Mapping) -> McpServer:
    """The neutral view of an OpenCode entry (the user's included).

    Lossy on purpose where the model cannot carry a value: a header that is not a variable
    reference, an environment value that is a secret-looking literal or a reference to a variable
    of another name. Those are skipped here; they are never copied into the neutral model.
    """
    enabled = not (entry.get("disabled") is True or entry.get("enabled") is False)
    if entry.get("type") == "remote" or entry.get("url"):
        url = entry.get("url") if isinstance(entry.get("url"), str) else None
        headers = []
        raw_headers = entry.get("headers")
        for key, value in raw_headers.items() if isinstance(raw_headers, dict) else ():
            match = _HEADER_REF.match(value) if isinstance(value, str) else None
            if match:
                headers.append(HeaderRef(str(key), match["var"], match["scheme"]))
        oauth = entry.get("oauth")
        auth = "oauth" if isinstance(oauth, dict) else "none" if oauth is False else None
        return McpServer(name=name, url=url, headers=tuple(headers), auth=auth, enabled=enabled)
    command = _canon_command(_strings(entry.get("command")))
    literals: list[tuple[str, str]] = []
    passed: list[str] = []
    environment = entry.get("environment")
    for key, value in environment.items() if isinstance(environment, dict) else ():
        if not isinstance(value, str):
            continue
        ref = _ENV_REF.match(value)
        if ref:
            if ref.group(1) == key:
                passed.append(str(key))
            continue  # a reference to a variable of another name: not representable
        if "{env:" in value or env_value_looks_secret(key, value):
            continue
        literals.append((str(key), value))
    return McpServer(
        name=name,
        command=command[0] if command else None,
        args=tuple(command[1:]),
        env_vars=tuple(passed),
        env=tuple(literals),
        cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
        enabled=enabled,
    )


# -- the edit of the config file ---------------------------------------------------------------


@dataclass(frozen=True)
class _McpEdit:
    """Everything the transform needs; computed at plan time from the bytes it will receive."""

    adds: tuple[tuple[str, dict], ...] = ()  # new entries
    updates: tuple[tuple[str, tuple, dict, tuple], ...] = ()  # (name, removes, replaces, adds)
    replace_whole: tuple[tuple[str, dict], ...] = ()  # existing non-object value, owned
    removes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.adds or self.updates or self.replace_whole or self.removes)


def _entry_diff(old: Mapping, fresh: Mapping) -> tuple[tuple, dict, tuple]:
    """Key-level difference that turns `old` into `fresh`, touching only keys we own."""
    old = {
        k: v
        for k, v in old.items()
        if not ((k == "disabled" and v is False) or (k == "enabled" and v is True))
    }
    owned = _OWNED_KEYS | ({"headers"} if "headers" in fresh else set())
    removes = tuple(sorted(k for k in owned if k in old and k not in fresh))
    replaces = {
        k: v
        for k, v in fresh.items()
        if k in owned and k in old and _canon_command(old[k]) != _canon_command(v)
    }
    adds = tuple((k, v, None) for k, v in fresh.items() if k in owned and k not in old)
    return removes, replaces, adds


def _apply_edit(current: bytes | None, spec: _McpEdit) -> bytes:
    """The pure transform of the MCP file."""
    if current is None or not current.strip():
        doc = {"$schema": SCHEMA_URL, "mcp": {"servers": dict(spec.adds)}}
        return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    text, bom, crlf = _decode_text(current)
    doc = jsonc.loads(text)
    container, _v1 = _locate(doc)
    try:
        if container is None:
            new_servers = dict(spec.adds)
            if "mcp" not in doc:
                text = jsonc_edit.edit_object(
                    text, [], adds=[("mcp", {"servers": new_servers}, None)]
                )
            else:
                text = jsonc_edit.edit_object(text, ["mcp"], adds=[("servers", new_servers, None)])
        else:
            path = list(container)
            for name, removes, replaces, adds in spec.updates:
                if removes or replaces or adds:
                    text = jsonc_edit.edit_object(text, path + [name], removes, replaces, adds)
            whole = dict(spec.replace_whole)
            adds = [(name, entry, None) for name, entry in spec.adds]
            if spec.removes or whole or adds:
                text = jsonc_edit.edit_object(text, path, spec.removes, whole or None, adds)
    except jsonc_edit.EditError as exc:
        raise ValueError(f"cannot edit the OpenCode config ({exc})") from exc
    _check_result(text, spec)
    return _encode_text(text, bom, crlf)


def _check_result(text: str, spec: _McpEdit) -> None:
    """Fail (so the engine reverts) if the edited text is not what the plan meant."""
    doc = jsonc.loads(text)
    container, _ = _locate(doc)
    node = doc
    for key in container or ():
        node = node[key]
    for name, entry in (*spec.adds, *spec.replace_whole):
        if node.get(name) != entry:
            raise ValueError(f"the edited OpenCode config does not hold the entry {name!r}")
    for name in spec.removes:
        if name in node:
            raise ValueError(f"the edited OpenCode config still holds the entry {name!r}")


# -- the adapter ---------------------------------------------------------------------------------


class OpenCodeAdapter(ToolAdapter):
    id: ClassVar[str] = "opencode"
    label: ClassVar[str] = "OpenCode"
    capabilities: ClassVar[frozenset[Kind]] = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})

    # -- discovery ---------------------------------------------------------------------------

    def detect(self, env: Env) -> Detection:
        root = config_dir(env)
        found = shutil.which("opencode", path=env.environ.get("PATH", ""))
        binary = Path(found) if found else None
        return Detection(
            installed=binary is not None or root.is_dir(),
            binary=binary,
            version=None,  # reading it would start the program; detect() must stay read-only
            config_roots=(root,),
        )

    def paths(self, env: Env, scope: Scope) -> Paths:
        if scope is not Scope.GLOBAL:
            return Paths()
        return Paths(
            instructions=config_dir(env) / INSTRUCTIONS_FILE,
            mcp=config_file(env),
            skills_dir=skills_dir(env),
        )

    # -- reading -----------------------------------------------------------------------------

    def read_instructions(self, env: Env, scope: Scope) -> Instructions | None:
        path = self.paths(env, scope).instructions
        if path is None or not path.is_file():
            return None
        return Instructions(path=path, text=path.read_bytes().decode("utf-8", errors="replace"))

    def read_mcp(self, env: Env, scope: Scope) -> dict[str, McpServer]:
        """The servers in the config file. Raises `ConfigUnreadable` if it cannot be parsed."""
        path = self.paths(env, scope).mcp
        if path is None:
            return {}
        config = load_config(path)
        return {
            name: decode_server(name, entry)
            for name, entry in config.members.items()
            if _entry_shaped(entry)
        }

    def read_skills(self, env: Env, scope: Scope) -> list[SkillRef]:
        directory = self.paths(env, scope).skills_dir
        if directory is None or not directory.is_dir():
            return []
        return [
            SkillRef(
                name=child.name,
                path=child,
                is_link=fsops.link_kind(child) in ("symlink", "junction"),
            )
            for child in sorted(directory.iterdir())
            if child.is_dir()
        ]

    # -- planning ----------------------------------------------------------------------------

    def plan_instructions(self, env: Env, scope: Scope, desired: str, owned: Owned) -> list[Op]:
        if scope is not Scope.GLOBAL:
            return [self._project_unsupported(Kind.INSTRUCTIONS)]
        path = self.paths(env, scope).instructions
        new = desired.encode("utf-8")
        old = path.read_bytes() if path.is_file() else None
        if old == new:
            return []
        if old is not None and INSTRUCTIONS_KEY not in owned:
            reason = "the file exists and was not created by all-agents; left alone"
            return [Conflict(self.id, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY, reason, path)]
        return [
            EditFile(
                path=path,
                transform=lambda _old: new,
                expect_sha256=None if old is None else _sha(old),
                description="write the shared instructions",
                claims=(ItemRef(self.id, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY),),
            )
        ]

    def plan_mcp(
        self, env: Env, scope: Scope, desired: Mapping[str, McpServer], owned: Owned
    ) -> list[Op]:
        if scope is not Scope.GLOBAL:
            return [self._project_unsupported(Kind.MCP)]
        path = self.paths(env, scope).mcp
        try:
            config = load_config(path)
        except ConfigUnreadable as exc:
            reason = f"{exc}; the file is left alone"
            return [
                Conflict(self.id, Kind.MCP, name, reason, path)
                for name in sorted(set(desired) | set(owned))
            ]
        windows = env.platform == "windows"
        ops: list[Op] = []
        gaps: list[str] = []
        adds: list[tuple[str, dict]] = []
        updates: list[tuple[str, tuple, dict, tuple]] = []
        whole: list[tuple[str, dict]] = []
        claimed: list[str] = []
        for name, server in desired.items():
            fatal = _fatal(server)
            if fatal:
                gaps.append(f"{name}: not written, {fatal}")
                continue
            lost = _lost_fields(server)
            if lost:
                gaps.append(f"{name}: {', '.join(lost)} not stored")
            fresh = encode_server(server, windows=windows, v1=config.v1)
            if name not in config.members:
                adds.append((name, fresh))
                claimed.append(name)
                continue
            old = config.members[name]
            diff = _entry_diff(old, fresh) if isinstance(old, dict) else None
            if diff is not None and not any(diff):
                continue  # already as wanted (an identical entry of the user's is left alone too)
            if name not in owned:
                reason = "an MCP server with this name exists and was not created by all-agents"
                ops.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            if diff is None:
                whole.append((name, fresh))
            else:
                updates.append((name, *diff))
            claimed.append(name)
        removals = tuple(n for n in sorted(owned - desired.keys()) if n in config.members)
        spec = _McpEdit(tuple(adds), tuple(updates), tuple(whole), removals)
        if not spec.empty:
            ops.append(
                EditFile(
                    path=path,
                    transform=lambda current, spec=spec: _apply_edit(current, spec),
                    expect_sha256=None if config.raw is None else _sha(config.raw),
                    description="update the MCP servers",
                    sensitive=True,
                    claims=tuple(ItemRef(self.id, Kind.MCP, n) for n in claimed),
                    releases=tuple(ItemRef(self.id, Kind.MCP, n) for n in removals),
                )
            )
        if gaps:
            ops.append(Unsupported(self.id, Kind.MCP, "; ".join(gaps)))
        return ops

    def plan_skills(
        self, env: Env, scope: Scope, desired: Sequence[SkillRef], owned: Owned
    ) -> list[Op]:
        if scope is not Scope.GLOBAL:
            return [self._project_unsupported(Kind.SKILLS)]
        directory = self.paths(env, scope).skills_dir
        ops: list[Op] = []
        wanted = {skill.name: skill for skill in desired}
        for name, skill in wanted.items():
            dest = directory / name
            ref = ItemRef(self.id, Kind.SKILLS, name)
            problem = _skill_problem(name, skill)
            if problem:
                ops.append(Conflict(self.id, Kind.SKILLS, name, problem, dest))
                continue
            state = _inspect(dest)
            if state.kind == "absent":
                ops.append(Link(dest, skill.path, f"link skill {name}", True, claims=(ref,)))
            elif state.matches(skill.path):
                continue  # already as wanted, owned or not
            elif (
                state.kind in ("symlink", "junction", "copy") and name in owned and not state.edited
            ):
                ops.append(Link(dest, skill.path, f"relink skill {name}", True, claims=(ref,)))
            else:
                reason = (
                    "this copy was edited after all-agents made it; left alone"
                    if state.edited
                    else "a skill with this name exists and was not created by all-agents"
                )
                ops.append(Conflict(self.id, Kind.SKILLS, name, reason, dest))
        for name in sorted(owned - wanted.keys()):
            dest = directory / name
            ref = ItemRef(self.id, Kind.SKILLS, name)
            state = _inspect(dest)
            if state.kind == "absent":
                continue
            if state.kind in ("symlink", "junction", "copy") and not state.edited:
                ops.append(Link(dest, None, f"withdraw skill {name}", releases=(ref,)))
            else:
                reason = "the owned skill is no longer an unedited link or copy; left alone"
                ops.append(Conflict(self.id, Kind.SKILLS, name, reason, dest))
        return ops

    # -- normalization -------------------------------------------------------------------------

    def normalize_mcp(self, servers: Mapping[str, McpServer]) -> dict[str, McpServer]:
        """What `read_mcp` returns after `servers` were written (for writable servers).

        Lost: `startup_timeout_sec` and `auth` (OAuth is the default; nothing is written for it);
        for a remote server also command, args, env, env_vars and cwd; for a local one the
        headers. The Windows `.cmd` suffix of a launcher is not part of the neutral view.
        """
        out = {}
        for name, server in servers.items():
            server = replace(_canon_server(server), startup_timeout_sec=None, auth=None)
            if server.url:
                server = replace(server, command=None, args=(), env=(), env_vars=(), cwd=None)
            else:
                server = replace(server, headers=())
            out[name] = server
        return out

    def notes(self) -> list[str]:
        return [
            "global scope only; project scope arrives in v0.2",
            "skills are linked into ~/.agents/skills, which OpenCode reads; "
            "~/.config/opencode/skills is not managed",
            "MCP: startup_timeout_sec cannot be stored; a remote server cannot carry "
            "args, env, env_vars or cwd; OAuth is OpenCode's default and is not written",
            "MCP: a secret is referenced as {env:NAME}; a literal secret in the user's own "
            "entry is never read into the neutral model",
            "Windows: npx, npm, pnpm and yarn are written as .cmd (unverified with a live tool)",
            "unverified: symlink-following for skills on Linux and macOS; everything on macOS",
        ]

    # -- helpers -------------------------------------------------------------------------------

    def _project_unsupported(self, kind: Kind) -> Unsupported:
        return Unsupported(self.id, kind, "project scope is not supported before v0.2")


# -- skills helpers ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _SkillState:
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


def _inspect(dest: Path) -> _SkillState:
    kind = fsops.link_kind(dest)
    if kind != "dir":
        return _SkillState(kind, dest)
    marker = fsops.read_marker(dest)
    if marker is None:
        return _SkillState("dir", dest)
    try:
        edited = fsops.tree_hash(dest) != marker["tree_sha256"]
    except (BackupError, OSError):
        edited = True
    return _SkillState("copy", dest, marker, edited)


def _skill_problem(name: str, skill: SkillRef) -> str | None:
    if not name or name in (".", "..") or "/" in name or "\\" in name or os.sep in name:
        return f"{name!r} is not a usable skill folder name"
    if not Path(skill.path).is_dir():
        return "the source skill folder does not exist"
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
