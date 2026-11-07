"""Antigravity adapter (the tool's command is `agy`; the adapter id is `agy`): global scope,
instructions + MCP servers + skills.

Adapters detect, read and plan; they never write (docs/decisions/0008). Everything here returns
`Op` objects; the engine runs them. Settled by docs/decisions/0009 (interface), 0010 (engine) and
0013 (this adapter). Facts marked [docs] come from the official Antigravity documentation
(antigravity.google/docs: rules, mcp, skills, customization-discovery), [prototype] from the legacy
implementation this project grew out of (ported in `_core/`, live-tested on Windows), and
[unverified] were not checked against a live tool.

Where Antigravity keeps things (global scope)
---------------------------------------------

    <home>/.gemini/GEMINI.md                     instructions (owned as one item)
    <home>/.gemini/config/mcp_config.json        MCP servers: top-level "mcpServers"
    <home>/.gemini/config/skills.json            skills manifest: "entries": [{"path": ...}]
    <home>/.gemini/config/skills/<name>          the folder agy reads skills from by itself

No environment variable moves any of these [docs name none; the adapter reads none].
`<home>/.gemini/GEMINI.md` is read by the App, the CLI and the IDE [docs]; the other standalone
files (`~/.gemini/AGENTS.md`, `~/.gemini/config/GEMINI.md`, `~/.gemini/config/AGENTS.md`) are read
too and are NOT managed here: a second copy there would be read in addition (reported by `notes`).

Instructions: one file, 24,000 bytes
------------------------------------

Antigravity truncates a global rules file above 24,000 bytes, after expanding `@[label](path)`
includes, WITHOUT a warning [docs]. A rendered text over the limit is a `Conflict` and nothing is
written (ADR 0003). The adapter checks the bytes it is given; it cannot expand includes.

MCP: `mcp_config.json` is edited as text (`_core/jsonc_edit.py`)
----------------------------------------------------------------

Comments, the user's other keys and servers, key order, the BOM and CRLF line endings stay as they
were. Entry format [docs]:

    "mcpServers": {"<name>": {
        "command": "npx", "args": ["-y", "pkg"], "env": {"MODE": "test"}, "cwd": "...",
        "disabled": true }}
    "mcpServers": {"<name>": {"serverUrl": "https://...", "headers": {...}}}

`serverUrl` is written (`url` is read as an alias and replaced when an owned entry changes);
`httpUrl` and the old SSE transport are not supported by Antigravity. `disabled` is the native
switch, written only when the server is disabled.

SECRETS. Antigravity documents no variable reference in `env` or `headers` [docs: the examples hold
literal values; the prototype concluded that agy does not expand `${NAME}` and wrapped such servers
in a runner]. So:

- a value that must be secret can never be written (the model forbids a literal and agy cannot
  reference a variable): an http server with `headers` is "not written" with an `Unsupported`
  line. The prototype's runner plus `mcp-remote` bridge is not ported (ADR 0013, open point).
- `env_vars` (names the tool passes on) is not written: the prototype found that agy hands its
  own environment to the server, so the variable reaches it without any entry. [prototype,
  unverified] The gap is still reported on every plan, and `normalize_mcp` resets the field,
  because the file cannot show it.

What Antigravity cannot store is reported as `Unsupported` (never dropped silently):
`startup_timeout_sec`, and for an http server `command`, `args`, `env`, `env_vars`, `cwd`. `auth`
is documentation only and is not written.

Windows launchers (`npx`, `npm`, `pnpm`, `yarn`) are written with the `.cmd` suffix (plan
decision, same as OpenCode and Codex); reading strips it again. [UNVERIFIED with a live tool.]

Skills: a manifest entry per skill, pointing at the source folder
-----------------------------------------------------------------

Antigravity does not follow directory junctions when it scans a skills folder [prototype, live
tests on Windows; symlinks on Linux and macOS: unverified]. But an entry of `skills.json` may name
ONE skill folder, by an absolute path, and that is read [docs; prototype: verified live]. So a
skill is made visible by one manifest entry that points straight at the source folder
(`<config-dir>/skills/<name>`): no link, no copy, nothing to refresh, the same on every OS.

    {"entries": [{"path": "/abs/source/skills/demo-skill-a"}, ...]}

(forward slashes on every OS; `~` is not expanded [docs].) Ownership: an entry is identified by
the NAME of its folder, because the ledger records names. A plain `{"path": ...}` entry whose folder
name is an owned skill name is ours; an entry with other keys (`include_only`, `exclude`), or
one for a name that is not owned, is the user's and is never changed. A skill that a user entry
already covers (the same folder, or the folder that contains it) is not added again. A user entry
or a real folder in `~/.gemini/config/skills/` with the same name is a `Conflict` (it would show
twice). The
`entries` array is replaced as a whole; if it holds comments or trailing commas the plan refuses
(`Conflict`) instead of losing them. Every other member of the file survives.

The skills folder that agy reads by itself is NOT linked into, so `wanted_links()` is empty:
nothing here is shared with the other tools' `~/.agents/skills` links (docs/decisions/0012).

Not verified: anything on Linux and macOS with a real install, the `.cmd` suffix, `env_vars`
inheritance, and agy's behaviour with two surfaces (App, CLI, IDE) reading the same manifest.
"""

from __future__ import annotations

import json
import os
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from .._core import jsonc, jsonc_edit
from .._core.mask import env_value_looks_secret
from ..model import (
    INSTRUCTIONS_KEY,
    Conflict,
    Detection,
    EditFile,
    Env,
    Instructions,
    ItemRef,
    Kind,
    McpServer,
    Op,
    Owned,
    Paths,
    Scope,
    SkillRef,
    Unsupported,
    VersionProbe,
    WantedLink,
)
from ._common import (
    decode_text,
    encode_text,
    find_binary,
    parse_version,
    plan_whole_file_instructions,
    read_skill_dir,
    sha256_hex,
    skill_problem,
)
from .base import ToolAdapter

HOME_DIR = ".gemini"
CONFIG_SUBDIR = "config"
INSTRUCTIONS_FILE = "GEMINI.md"
MCP_FILE = "mcp_config.json"
SKILLS_MANIFEST = "skills.json"
SKILLS_SUBDIR = "skills"
BINARY = "agy"
#: What `--version` prints, as measured: a bare `1.4.12`. A banner such as `agy 1.4.12` also reads.
VERSION_BANNER = r"\bagy\s+v?(\d\S*)"

#: Antigravity truncates a global rules file above this many bytes, silently [docs].
INSTRUCTIONS_LIMIT = 24_000

#: Launchers that Windows resolves only with a `.cmd` suffix (same set as the other adapters).
WIN_SHIMS = frozenset({"npx", "npm", "pnpm", "yarn"})

#: Keys of an MCP entry that this adapter writes. Any other key of the user's entry is kept.
#: `url` (an accepted alias) and `httpUrl` are ours so a stale one cannot contradict `serverUrl`.
_OWNED_KEYS = frozenset(
    {"command", "args", "serverUrl", "url", "httpUrl", "env", "cwd", "disabled"}
)


class ConfigUnreadable(ValueError):
    """`mcp_config.json` exists but cannot be understood. Never overwritten."""


class ManifestUnreadable(ValueError):
    """`skills.json` exists but cannot be understood or safely edited. Never overwritten."""


# -- paths -------------------------------------------------------------------------------------


def gemini_dir(env: Env) -> Path:
    return Path(env.home) / HOME_DIR


def config_dir(env: Env) -> Path:
    return gemini_dir(env) / CONFIG_SUBDIR


def instructions_file(env: Env) -> Path:
    return gemini_dir(env) / INSTRUCTIONS_FILE


def mcp_file(env: Env) -> Path:
    return config_dir(env) / MCP_FILE


def skills_manifest(env: Env) -> Path:
    return config_dir(env) / SKILLS_MANIFEST


def skills_dir(env: Env) -> Path:
    return config_dir(env) / SKILLS_SUBDIR


# -- reading mcp_config.json ---------------------------------------------------------------------


def _entry_shaped(value: object) -> bool:
    return isinstance(value, dict) and (
        "command" in value or "serverUrl" in value or "url" in value
    )


@dataclass(frozen=True)
class _Config:
    path: Path
    raw: bytes | None  # None: the file does not exist
    members: dict  # every member of "mcpServers", by name (empty if there is none)
    has_container: bool  # is there a "mcpServers" object

    @property
    def blank(self) -> bool:
        return self.raw is None or not self.raw.strip()


def load_config(path: Path) -> _Config:
    """Parse the file (read-only). Raises `ConfigUnreadable` with a reason that has no content."""
    path = Path(path)
    if not path.is_file():
        return _Config(path, None, {}, False)
    raw = path.read_bytes()
    if not raw.strip():
        return _Config(path, raw, {}, False)
    try:
        text, _bom, _crlf = decode_text(raw)
        doc = jsonc.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigUnreadable(f"cannot parse {path.name} as JSON with comments ({exc})") from exc
    if not isinstance(doc, dict):
        raise ConfigUnreadable(f"{path.name}: the top level is not an object")
    if "mcpServers" not in doc:
        return _Config(path, raw, {}, False)
    if not isinstance(doc["mcpServers"], dict):
        raise ConfigUnreadable('"mcpServers" is not an object')
    return _Config(path, raw, dict(doc["mcpServers"]), True)


# -- neutral model <-> Antigravity entry -----------------------------------------------------------


def _canon_command(command: object) -> object:
    """A command with the Windows `.cmd` suffix removed from a launcher shim."""
    if isinstance(command, str) and command.lower().endswith(".cmd"):
        if command[:-4].lower() in WIN_SHIMS:
            return command[:-4]
    return command


def _canon_server(server: McpServer) -> McpServer:
    if server.command:
        return replace(server, command=_canon_command(server.command))
    return server


def _fatal(server: McpServer) -> str | None:
    """Why this server cannot be written to Antigravity at all (None: it can)."""
    if not server.name:
        return "it has no name"
    if not server.url and not server.command:
        return "it has neither a command nor a url"
    if server.url and server.headers:
        return (
            "agy has no way to reference an environment variable in a header, and a header value "
            "must never be written as a literal"
        )
    return None


def _lost_fields(server: McpServer) -> list[str]:
    """Fields of a writable server that the Antigravity format has no place for."""
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


def encode_server(server: McpServer, *, windows: bool) -> dict:
    """The Antigravity entry for `server`. Holds no secret: env values are non-secret literals."""
    entry: dict = {}
    if server.url:
        entry["serverUrl"] = server.url
    else:
        command = server.command or ""
        if windows and command in WIN_SHIMS:
            command += ".cmd"
        entry["command"] = command
        entry["args"] = list(server.args)
        if server.env:
            entry["env"] = dict(server.env)
        if server.cwd:
            entry["cwd"] = server.cwd
    if not server.enabled:
        entry["disabled"] = True
    return entry


def decode_server(name: str, entry: Mapping) -> McpServer:
    """The neutral view of an Antigravity entry (the user's included).

    Lossy on purpose where the model cannot carry a value: headers (never a variable reference
    in agy), an env value that looks like a secret. Those are skipped; they are never copied into
    the neutral model.
    """
    enabled = entry.get("disabled") is not True
    url = entry.get("serverUrl") or entry.get("url")
    if isinstance(url, str) and url:
        return McpServer(name=name, url=url, enabled=enabled)
    literals: list[tuple[str, str]] = []
    environment = entry.get("env")
    for key, value in environment.items() if isinstance(environment, dict) else ():
        if isinstance(value, str) and not env_value_looks_secret(str(key), value):
            literals.append((str(key), value))
    raw_args = entry.get("args")
    args = (
        tuple(str(a) for a in raw_args if isinstance(a, (str, int, float)))
        if isinstance(raw_args, list)
        else ()
    )
    command = _canon_command(entry.get("command"))
    return McpServer(
        name=name,
        command=command if isinstance(command, str) else None,
        args=args,
        env=tuple(literals),
        cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
        enabled=enabled,
    )


def _owned_view(entry: Mapping) -> dict:
    """The part of an entry this adapter owns, where "same meaning" is "equal".

    `url` equals `serverUrl`, a missing args/env is empty, `disabled: false` is absent and a `.cmd`
    launcher shim equals the bare launcher.
    """
    return {
        "command": _canon_command(entry.get("command")),
        "args": entry.get("args") or [],
        "url": entry.get("serverUrl") or entry.get("url"),
        "env": entry.get("env") or {},
        "cwd": entry.get("cwd"),
        "disabled": entry.get("disabled") is True,
    }


def _entry_diff(old: Mapping, fresh: Mapping) -> tuple[tuple, dict, tuple]:
    """Key-level difference that turns `old` into `fresh`, touching only keys we own."""
    removes = tuple(sorted(k for k in _OWNED_KEYS if k in old and k not in fresh))
    replaces = {
        k: v for k, v in fresh.items() if k in old and _canon_command(old[k]) != _canon_command(v)
    }
    adds = tuple((k, v, None) for k, v in fresh.items() if k not in old)
    return removes, replaces, adds


# -- the edit of mcp_config.json -------------------------------------------------------------------


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


def _apply_mcp_edit(current: bytes | None, spec: _McpEdit) -> bytes:
    """The pure transform of the MCP file."""
    if current is None or not current.strip():
        doc = {"mcpServers": dict(spec.adds)}
        return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    text, bom, crlf = decode_text(current)
    doc = jsonc.loads(text)
    try:
        if "mcpServers" not in doc:
            text = jsonc_edit.edit_object(text, [], adds=[("mcpServers", dict(spec.adds), None)])
        else:
            path = ["mcpServers"]
            for name, removes, replaces, adds in spec.updates:
                if removes or replaces or adds:
                    text = jsonc_edit.edit_object(text, path + [name], removes, replaces, adds)
            whole = dict(spec.replace_whole)
            adds = [(name, entry, None) for name, entry in spec.adds]
            if spec.removes or whole or adds:
                text = jsonc_edit.edit_object(text, path, spec.removes, whole or None, adds)
    except jsonc_edit.EditError as exc:
        raise ValueError(f"cannot edit the Antigravity MCP config ({exc})") from exc
    node = jsonc.loads(text)["mcpServers"]
    for name, entry in (*spec.adds, *spec.replace_whole):
        if node.get(name) != entry:
            raise ValueError(f"the edited MCP config does not hold the entry {name!r}")
    for name in spec.removes:
        if name in node:
            raise ValueError(f"the edited MCP config still holds the entry {name!r}")
    return encode_text(text, bom, crlf)


# -- reading and editing skills.json -------------------------------------------------------------


def _fwd(path: object) -> str:
    return str(path).replace("\\", "/")


def _key(path: str, base: Path) -> str:
    """Comparison key of an entry path: absolute, normalised, forward slashes, case folded on
    Windows. A relative path is resolved against `base` (the folder of a personal manifest)."""
    text = _fwd(path)
    if not os.path.isabs(text):
        text = _fwd(os.path.join(str(base), text))
    return _fwd(os.path.normcase(os.path.normpath(text)))


def _folder_name(path: str) -> str:
    return posixpath.basename(_fwd(path).rstrip("/"))


def _entry_path(entry: object) -> str | None:
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        return entry["path"]
    return None


def _is_plain(entry: object) -> bool:
    """Only `path`: the shape this adapter writes. An entry with more keys is the user's."""
    return isinstance(entry, dict) and set(entry) == {"path"} and isinstance(entry["path"], str)


@dataclass(frozen=True)
class _Manifest:
    path: Path
    raw: bytes | None  # None: the file does not exist
    entries: list  # the "entries" array (empty if there is none)

    @property
    def blank(self) -> bool:
        return self.raw is None or not self.raw.strip()


def load_manifest(path: Path) -> _Manifest:
    """Parse the file (read-only). Raises `ManifestUnreadable` with a reason that has no content."""
    path = Path(path)
    if not path.is_file():
        return _Manifest(path, None, [])
    raw = path.read_bytes()
    if not raw.strip():
        return _Manifest(path, raw, [])
    try:
        text, _bom, _crlf = decode_text(raw)
        doc = jsonc.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestUnreadable(f"cannot parse {path.name} as JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise ManifestUnreadable(f"{path.name}: the top level is not an object")
    entries = doc.get("entries", [])
    if not isinstance(entries, list):
        raise ManifestUnreadable(f'{path.name}: "entries" is not an array')
    return _Manifest(path, raw, entries)


def _entries_are_strict_json(raw: bytes) -> bool:
    """Is the `entries` array plain JSON (no comments, no trailing commas)? It is replaced whole,
    so anything else would be lost."""
    text, _bom, _crlf = decode_text(raw)
    try:
        root = jsonc_edit.find_object(text, [])
        members, _close = jsonc_edit.members_of(text, root)
        for member in members:
            if member.key == "entries":
                json.loads(text[member.value_start : member.value_end])
        return True
    except (jsonc_edit.EditError, ValueError):
        return False


@dataclass(frozen=True)
class _ManifestEdit:
    """Positions are indexes into the `entries` array the plan was made from; the engine's content
    check guarantees the transform sees that same array."""

    removes: tuple[int, ...] = ()
    replaces: tuple[tuple[int, str], ...] = ()  # (index, new path)
    adds: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.removes or self.replaces or self.adds)


def _edited_entries(entries: Sequence, spec: _ManifestEdit) -> list:
    replaced = dict(spec.replaces)
    out: list = []
    for index, entry in enumerate(entries):
        if index in spec.removes:
            continue
        out.append({"path": replaced[index]} if index in replaced else entry)
    out.extend({"path": path} for path in spec.adds)
    return out


def _apply_manifest_edit(current: bytes | None, spec: _ManifestEdit) -> bytes:
    """The pure transform of the skills manifest."""
    if current is None or not current.strip():
        doc = {"entries": _edited_entries([], spec)}
        return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    text, bom, crlf = decode_text(current)
    doc = jsonc.loads(text)
    new = _edited_entries(doc.get("entries", []), spec)
    try:
        if "entries" in doc:
            text = jsonc_edit.edit_object(text, [], replaces={"entries": new})
        else:
            text = jsonc_edit.edit_object(text, [], adds=[("entries", new, None)])
    except jsonc_edit.EditError as exc:
        raise ValueError(f"cannot edit the Antigravity skills manifest ({exc})") from exc
    after = jsonc.loads(text)
    expected = {**doc, "entries": new}
    if after != expected:
        raise ValueError("the edited skills manifest does not read back as planned")
    return encode_text(text, bom, crlf)


def _listed_skills(manifest: _Manifest) -> list[SkillRef]:
    """The skills a manifest names: an entry that is a skill folder, or the real skill folders
    one level inside an entry that is a folder of skills."""
    base = manifest.path.parent
    out: list[SkillRef] = []
    for entry in manifest.entries:
        text = _entry_path(entry)
        if text is None:
            continue
        folder = Path(text) if os.path.isabs(text) else base / text
        if (folder / "SKILL.md").is_file():
            out.append(SkillRef(name=folder.name, path=folder))
        elif folder.is_dir():
            out.extend(
                SkillRef(name=child.name, path=child)
                for child in sorted(folder.iterdir())
                if child.is_dir() and not child.is_symlink() and (child / "SKILL.md").is_file()
            )
    return out


# -- the adapter ---------------------------------------------------------------------------------


def install_dirs(env: Env) -> list[Path]:
    """The default Windows install folder, `%LOCALAPPDATA%/agy/bin` [prototype], which the
    installer does not always put on `PATH`."""
    if env.platform != "windows":
        return []
    local = env.environ.get("LOCALAPPDATA")
    return [(Path(local) if local else Path(env.home) / "AppData" / "Local") / "agy" / "bin"]


def parse_version_output(output: str) -> str | None:
    return parse_version(output, prefer=VERSION_BANNER)


class AntigravityAdapter(ToolAdapter):
    id: ClassVar[str] = "agy"
    label: ClassVar[str] = "Antigravity"
    capabilities: ClassVar[frozenset[Kind]] = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})

    # -- discovery ---------------------------------------------------------------------------

    def detect(self, env: Env) -> Detection:
        root = gemini_dir(env)
        found = find_binary(env, BINARY, extra_dirs=install_dirs(env))
        binary = found.path if found else None
        return Detection(
            installed=binary is not None or root.is_dir(),
            binary=binary,
            binary_source=found.source if found else None,
            version=None,  # reading it would start the program; detect() must stay read-only
            config_roots=(root,),
        )

    def version_probe(self, env: Env, binary: Path) -> VersionProbe | None:
        return VersionProbe((str(binary), "--version"), parse_version_output)

    def paths(self, env: Env, scope: Scope) -> Paths:
        if scope is not Scope.GLOBAL:
            return Paths()
        return Paths(
            instructions=instructions_file(env),
            mcp=mcp_file(env),
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
        """Skills in the folder agy reads by itself, plus those the manifest names. Raises
        `ManifestUnreadable` if the manifest cannot be parsed."""
        if scope is not Scope.GLOBAL:
            return []
        seen = read_skill_dir(skills_dir(env))
        names = {s.name for s in seen}
        for skill in _listed_skills(load_manifest(skills_manifest(env))):
            if skill.name not in names:
                names.add(skill.name)
                seen.append(skill)
        return seen

    # -- planning ----------------------------------------------------------------------------

    def plan_instructions(self, env: Env, scope: Scope, desired: str, owned: Owned) -> list[Op]:
        if scope is not Scope.GLOBAL:
            return [self._project_unsupported(Kind.INSTRUCTIONS)]
        path = self.paths(env, scope).instructions
        size = len(desired.encode("utf-8"))
        if size > INSTRUCTIONS_LIMIT:
            reason = (
                f"the rendered instructions are {size} bytes; Antigravity truncates a global "
                f"rules file above {INSTRUCTIONS_LIMIT} bytes without a warning; shorten the "
                "source (nothing was written)"
            )
            return [Conflict(self.id, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY, reason, path)]
        return plan_whole_file_instructions(self.id, path, desired, owned)

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
            if server.env_vars and not server.url:
                gaps.append(
                    f"{name}: env_vars not stored (relies on agy passing its own environment "
                    "to the server; unverified)"
                )
            fresh = encode_server(server, windows=windows)
            if name not in config.members:
                adds.append((name, fresh))
                claimed.append(name)
                continue
            old = config.members[name]
            same = isinstance(old, dict) and _owned_view(old) == _owned_view(fresh)
            if same:
                continue  # already as wanted (an identical entry of the user's is left alone too)
            if name not in owned:
                reason = "an MCP server with this name exists and was not created by all-agents"
                ops.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            if isinstance(old, dict):
                updates.append((name, *_entry_diff(old, fresh)))
            else:
                whole.append((name, fresh))
            claimed.append(name)
        removals = tuple(n for n in sorted(owned - desired.keys()) if n in config.members)
        spec = _McpEdit(tuple(adds), tuple(updates), tuple(whole), removals)
        if not spec.empty:
            ops.append(
                EditFile(
                    path=path,
                    transform=lambda current, spec=spec: _apply_mcp_edit(current, spec),
                    expect_sha256=None if config.raw is None else sha256_hex(config.raw),
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
        manifest_path = skills_manifest(env)
        wanted = {skill.name: skill for skill in desired}
        every_name = sorted(set(wanted) | set(owned))
        try:
            manifest = load_manifest(manifest_path)
            if not manifest.blank and not _entries_are_strict_json(manifest.raw):
                raise ManifestUnreadable(
                    f"{manifest_path.name}: the entries array holds comments or trailing commas "
                    "that an edit would lose"
                )
        except ManifestUnreadable as exc:
            reason = f"{exc}; the file is left alone"
            return [Conflict(self.id, Kind.SKILLS, n, reason, manifest_path) for n in every_name]

        base = manifest_path.parent
        rows = []  # (index, entry, folder name or None, key or None, ours-if-owned)
        for index, entry in enumerate(manifest.entries):
            text = _entry_path(entry)
            rows.append(
                (
                    index,
                    entry,
                    _folder_name(text) if text is not None else None,
                    _key(text, base) if text is not None else None,
                    _is_plain(entry),
                )
            )

        def ours(name: str):
            return [r for r in rows if r[2] == name and r[4] and name in owned]

        conflicts: list[Op] = []
        removes: list[int] = []
        replaces: list[tuple[int, str]] = []
        adds: list[str] = []
        claimed: list[str] = []
        released: list[str] = []
        native = skills_dir(env)
        for name, skill in wanted.items():
            problem = skill_problem(name, skill)
            if problem is None and _folder_name(str(skill.path)) != name:
                problem = (
                    "the manifest names a skill by its folder path, so the folder name must be "
                    f"the skill name ({_folder_name(str(skill.path))!r} is not {name!r})"
                )
            if problem is None and not Path(skill.path).is_absolute():
                problem = "the source skill folder path is not absolute"
            if problem is None and os.path.lexists(native / name):
                problem = (
                    f"a skill with this name exists in {native}, which agy reads by itself; "
                    "it would be listed twice"
                )
            if problem:
                conflicts.append(Conflict(self.id, Kind.SKILLS, name, problem, manifest_path))
                continue
            target = _fwd(Path(skill.path).as_posix())
            tkey = _key(target, base)
            mine = ours(name)
            foreign = [r for r in rows if r not in mine]
            if any(r[3] == tkey for r in foreign + mine):
                continue  # listed as wanted already (an identical entry of the user's too)
            parent_key = _fwd(posixpath.dirname(tkey))
            if any(r[3] == parent_key for r in foreign):
                continue  # a folder entry of the user's already covers it
            if any(r[2] == name for r in foreign):
                reason = "a skill with this name is already listed from another path"
                conflicts.append(Conflict(self.id, Kind.SKILLS, name, reason, manifest_path))
                continue
            claimed.append(name)
            if mine:
                replaces.append((mine[0][0], target))
                removes.extend(r[0] for r in mine[1:])
            else:
                adds.append(target)
        for name in sorted(owned - wanted.keys()):
            listed = [r for r in rows if r[2] == name]
            if not listed:
                continue
            if all(r[4] for r in listed):
                removes.extend(r[0] for r in listed)
                released.append(name)
            else:
                reason = "the owned skill's manifest entry was customised; left alone"
                conflicts.append(Conflict(self.id, Kind.SKILLS, name, reason, manifest_path))
        spec = _ManifestEdit(tuple(sorted(removes)), tuple(replaces), tuple(adds))
        ops: list[Op] = list(conflicts)
        if not spec.empty:
            try:  # a dry run on the exact bytes the engine will hand over
                _apply_manifest_edit(manifest.raw, spec)
            except ValueError as exc:
                reason = f"{exc}; the file is left alone"
                return [
                    Conflict(self.id, Kind.SKILLS, n, reason, manifest_path) for n in every_name
                ]
            ops.append(
                EditFile(
                    path=manifest_path,
                    transform=lambda current, spec=spec: _apply_manifest_edit(current, spec),
                    expect_sha256=None if manifest.raw is None else sha256_hex(manifest.raw),
                    description="update the skills manifest",
                    claims=tuple(ItemRef(self.id, Kind.SKILLS, n) for n in claimed),
                    releases=tuple(ItemRef(self.id, Kind.SKILLS, n) for n in released),
                )
            )
        return ops

    def wanted_links(self, env: Env, scope: Scope, desired: Sequence[SkillRef]) -> list[WantedLink]:
        """None: skills are listed in a manifest, not linked, so nothing is shared with the other
        tools' `~/.agents/skills` links (docs/decisions/0012, 0013)."""
        return []

    # -- normalization -------------------------------------------------------------------------

    def normalize_mcp(self, servers: Mapping[str, McpServer]) -> dict[str, McpServer]:
        """What `read_mcp` returns after `servers` were written (for writable servers).

        A server that is not written (an http server with headers) is not in the result. Lost for
        the others: `startup_timeout_sec`, `auth` and `env_vars` (see the module docstring); for
        a remote server also command, args, env and cwd; for a local one the headers. The `.cmd`
        suffix of a launcher is not part of the neutral view.
        """
        out = {}
        for name, server in servers.items():
            if _fatal(server):
                continue
            server = replace(
                _canon_server(server), startup_timeout_sec=None, auth=None, env_vars=()
            )
            if server.url:
                server = replace(server, command=None, args=(), env=(), cwd=None)
            else:
                server = replace(server, headers=())
            out[name] = server
        return out

    def notes(self) -> list[str]:
        return [
            "global scope only; project scope arrives in v0.2",
            "instructions: ~/.gemini/GEMINI.md; Antigravity truncates a file above 24000 bytes "
            "without a warning, so a longer text is a conflict; ~/.gemini/AGENTS.md and "
            "~/.gemini/config/{AGENTS,GEMINI}.md are also read and are not managed",
            "MCP: ~/.gemini/config/mcp_config.json; startup_timeout_sec cannot be stored; a remote "
            "server cannot carry args, env, env_vars or cwd",
            "MCP: agy has no variable reference in env or headers, so an http server with "
            "headers is not written (the prototype's runner bridge is not ported); env_vars is "
            "not written and relies on agy passing its environment on (unverified)",
            "skills: one entry per skill in ~/.gemini/config/skills.json pointing at the source "
            "folder (agy does not follow Windows junctions); nothing is linked, and the "
            "~/.agents/skills links of the other tools are not used",
            "Windows: npx, npm, pnpm and yarn are written as .cmd (unverified with a live tool)",
            "unverified: everything on Linux and macOS with a real install",
        ]

    # -- helpers -------------------------------------------------------------------------------

    def _project_unsupported(self, kind: Kind) -> Unsupported:
        return Unsupported(self.id, kind, "project scope is not supported before v0.2")
