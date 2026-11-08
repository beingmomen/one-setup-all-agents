"""Claude Code adapter: global scope, instructions + MCP servers + skills.

Adapters detect, read and plan; they never write (docs/decisions/0008). Everything here returns
`Op` objects; the engine runs them. Settled by docs/decisions/0009 (interface), 0010 (engine) and
0011 (this adapter).

Where Claude Code keeps things (global scope)
---------------------------------------------

    <config>/CLAUDE.md                instructions (owned as one item, `INSTRUCTIONS_KEY`)
    <config>/skills/<name>            skills (one link per skill, docs/decisions/0007)
    <claude.json>                     MCP servers: top-level "mcpServers" (the "user" scope)

`<config>` is `$CLAUDE_CONFIG_DIR` when it is set to an absolute path (read from `env.environ`,
never from the OS), else `<home>/.claude`. `<claude.json>` is `$CLAUDE_CONFIG_DIR/.claude.json`
when the variable is set and `<home>/.claude.json` otherwise: the official docs say that with the
variable set, Claude Code reads `.claude.json` from inside that directory instead of from the home
directory. [Windows: verified with the real tool for the default locations; the effect of
`CLAUDE_CONFIG_DIR` is from the docs and UNVERIFIED with a live tool; Linux and macOS UNVERIFIED.]
A relative `CLAUDE_CONFIG_DIR` is ignored (Claude would resolve it against its own working
directory, which an adapter cannot know) and `detect` warns about it.

MCP is written through Claude Code's own CLI, not by editing `.claude.json`
----------------------------------------------------------------------------

`.claude.json` is rewritten by every running Claude Code session (counters, per-project state,
caches), so an edit of the file would have the engine's content check (`expect_sha256`) refuse
whenever a session wrote in between, and a read-modify-write of the whole file could lose that
session's changes. The CLI does the write with Claude Code's own merge logic, so this adapter
plans `RunCli` ops (the proven behaviour of the prototype this project comes from):

    add:     claude mcp add-json <name> '<json>' -s user
    inverse: claude mcp remove <name> -s user
    remove:  claude mcp remove <name> -s user
    inverse: claude mcp add-json <name> '<old entry>' -s user

`-s user` is always passed, so a same-named server of a project (local or `.mcp.json` scope) is
never touched. Changing an owned server is a remove followed by an add (the CLI has no update):
two ops, so the engine's revert puts the old entry back if the add fails (ADR 0010). The file is
only READ here, to compare; a torn read (a session writing at that moment) is retried once.
Because the engine cannot re-read a file it does not know, "verified" means exit code 0 (ADR 0010).

The CLI is found through `ALL_AGENTS_CLAUDE_BIN` (a path to the binary; the prototype's variable)
or else `PATH`. Without it the MCP changes cannot run: each affected server becomes a `Conflict`
saying so, and nothing else in the plan is affected (the other kinds do not need the CLI).

MCP entry format
----------------

    {"type": "stdio", "command": "...", "args": [...],
     "env": {"KEY": "literal", "OTHER": "${OTHER}"}}
    {"type": "http", "url": "https://...", "headers": {"Authorization": "Bearer ${TOKEN}"}}

A secret is referenced by variable name only, in Claude Code's own `${NAME}` syntax (it expands
`${VAR}` and `${VAR:-default}` in command, args, env, url and headers). `type` is always written:
an entry with a `url` and no `type` is a configuration error in Claude Code. Keys of an owned
entry that this adapter does not own (`oauth`, `headersHelper`, ...) survive a change.

What Claude Code cannot store is reported as `Unsupported` (never dropped silently): `cwd` (not a
documented key), `startup_timeout_sec` (only the global `MCP_TIMEOUT`), and for an http server
`command`, `args`, `env`, `env_vars`, `cwd`. `auth` is documentation only (OAuth is on by default)
and is not written. `normalize_mcp` says exactly which fields do not survive a round trip. An
`sse` or `streamable-http` entry is read as http; a `ws` entry is not representable and is not
read.

A server with `enabled = false` is not written at all: the user scope has no per-server switch, and
writing it would turn on a server the source disables. It is treated as "should not be present":
absent means nothing is planned, an owned entry is removed (the normal remove path), an entry that
is not owned is left alone, and one `Unsupported` line names the server.

Windows launchers (`npx`, `npm`, `pnpm`, `yarn`): written as plain names, never `.cmd`. The
prototype did this against the live tool, and the Claude Code changelog removed its "requires a
cmd /c wrapper" warning as a false positive. Reading strips a `.cmd` suffix, so the neutral view
is the same on every platform. [UNVERIFIED in this repository with a live tool.]

Skills are linked straight from the source into `<config>/skills/<name>`. Claude Code does not read
`~/.agents/skills`. The folders `.trash` and `synced` (managed by Claude Code and claude.ai) are
never listed or touched.

Not verified: whether Claude Code follows skill symlinks on Linux and macOS (0007 open point; the
prototype saw junctions work on Windows), the behaviour of the Windows `claude.cmd` shim with
unusual characters (guarded, see below), and anything on macOS at all (CI only).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from .._core.mask import env_value_looks_secret, mask_args, mask_text
from ..model import (
    SOURCE_OVERRIDE,
    SOURCE_PATH,
    Conflict,
    Detection,
    Env,
    HeaderRef,
    ImportNote,
    Instructions,
    ItemRef,
    Kind,
    McpServer,
    Op,
    Owned,
    Paths,
    RunCli,
    Scope,
    SkillRef,
    Unsupported,
    VersionProbe,
)
from ._common import (
    find_binary,
    is_absolute,
    parse_version,
    plan_skill_links,
    plan_whole_file_instructions,
    read_skill_dir,
)
from .base import ConfigUnreadable, ToolAdapter

CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
BIN_ENV = "ALL_AGENTS_CLAUDE_BIN"
CLI_NAME = "claude"
#: What `--version` prints, as measured: `2.1.310 (Claude Code)`.
VERSION_BANNER = r"(\d[^\s()]*)\s+\(Claude Code\)"
DEFAULT_DIR = ".claude"
STATE_FILE = ".claude.json"
INSTRUCTIONS_FILE = "CLAUDE.md"
SKILLS_DIR = "skills"
MCP_SCOPE = "user"

#: Folders inside the skills directory that Claude Code and claude.ai manage: never skills of ours.
RESERVED_SKILL_DIRS = frozenset({".trash", "synced"})

#: Launchers that a `.cmd` suffix may be stripped from when reading (same set as the prototype).
WIN_SHIMS = frozenset({"npx", "npm", "pnpm", "yarn"})

#: Keys of an MCP entry that this adapter writes. Any other key of an owned entry is kept.
_OWNED_KEYS = frozenset({"type", "command", "args", "url", "env", "headers"})

#: Characters a Windows `.cmd` / `.bat` shim cannot be trusted to pass through unchanged.
_SHIM_UNSAFE = frozenset("%&|<>^!\r\n")

_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_HEADER_REF = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9._-]*) )?\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}$"
)
_HTTP_TYPES = frozenset({"http", "sse", "streamable-http"})


# -- paths -------------------------------------------------------------------------------------


def config_override(env: Env) -> Path | None:
    """`$CLAUDE_CONFIG_DIR` when it is an absolute path (in the style of `env.platform`)."""
    value = env.environ.get(CONFIG_DIR_ENV)
    if value and is_absolute(value, env.platform):
        return Path(value)
    return None


def config_dir(env: Env) -> Path:
    return config_override(env) or Path(env.home) / DEFAULT_DIR


def state_file(env: Env) -> Path:
    """`.claude.json`: inside `CLAUDE_CONFIG_DIR` when it is set, else directly in the home."""
    override = config_override(env)
    return (override or Path(env.home)) / STATE_FILE


def skills_dir(env: Env) -> Path:
    return config_dir(env) / SKILLS_DIR


def locate_cli(env: Env) -> tuple[Path | None, str | None, str | None]:
    """(binary, how it was found, warning). The first two are None when it is not found.

    `ALL_AGENTS_CLAUDE_BIN` wins and must name an existing file (a typo is reported, not replaced
    by a different install found on PATH). Otherwise the `PATH` of `env.environ` is searched, then
    the known install folders (`~/.claude/local`, `~/.local/bin`, ...). Nothing is started.
    """
    override = env.environ.get(BIN_ENV)
    if override:
        path = Path(override)
        if path.is_file():
            return path, SOURCE_OVERRIDE, None
        return None, None, f"{BIN_ENV} points to a file that does not exist"
    found = find_binary(env, CLI_NAME, extra_dirs=[Path(env.home) / DEFAULT_DIR / "local"])
    return (found.path, found.source, None) if found else (None, None, None)


def find_cli(env: Env) -> tuple[str | None, Path | None, str | None]:
    """(argv[0] to use, resolved binary, warning). The first two are None when it is not found.

    A binary found on `PATH` is called by its bare name, so the plan reads the same on every
    machine. One from the override or from a known install folder is called by its full path,
    because the bare name would not resolve.
    """
    binary, source, warning = locate_cli(env)
    if binary is None:
        return None, None, warning
    return (CLI_NAME if source == SOURCE_PATH else str(binary)), binary, warning


def parse_version_output(output: str) -> str | None:
    return parse_version(output, prefer=VERSION_BANNER)


# -- reading .claude.json ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Config:
    path: Path
    members: dict  # the "mcpServers" object, by server name (empty if there is none)


def _parse(raw: bytes) -> dict:
    if not raw.strip():
        return {}
    doc = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError("the top level is not an object")
    return doc


def load_config(path: Path) -> _Config:
    """Parse the file (read-only). Raises `ConfigUnreadable` with a reason that has no content.

    A running session rewrites this file, so one failed parse is retried once after a short wait.
    """
    path = Path(path)
    if not path.is_file():
        return _Config(path, {})
    try:
        try:
            doc = _parse(path.read_bytes())
        except ValueError:
            time.sleep(0.05)
            doc = _parse(path.read_bytes())
    except (OSError, ValueError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ConfigUnreadable(f"cannot parse {path.name} as JSON ({exc})") from exc
    members = doc.get("mcpServers")
    if members is None:
        return _Config(path, {})
    if not isinstance(members, dict):
        raise ConfigUnreadable('"mcpServers" is not an object')
    return _Config(path, dict(members))


# -- neutral model <-> Claude Code entry ---------------------------------------------------------


def _canon_command(command: object) -> object:
    """A command with the Windows `.cmd` suffix removed from a launcher shim."""
    if isinstance(command, str) and command.lower().endswith(".cmd"):
        if command[:-4].lower() in WIN_SHIMS:
            return command[:-4]
    return command


def _kind(entry: Mapping) -> str:
    """The transport of an entry: stdio is the default when `type` is absent."""
    kind = entry.get("type")
    if kind is None:
        return "stdio"
    return "http" if kind in _HTTP_TYPES else str(kind)


def _entry_shaped(value: object) -> bool:
    return isinstance(value, dict) and ("type" in value or "command" in value or "url" in value)


def _representable(entry: Mapping) -> bool:
    """Can the neutral model hold this entry (stdio or http)? A `ws` entry cannot."""
    kind = entry.get("type")
    return kind is None or kind == "stdio" or kind in _HTTP_TYPES


def _canon_server(server: McpServer) -> McpServer:
    if server.command:
        return replace(server, command=_canon_command(server.command))
    return server


def _name_problem(name: str) -> str | None:
    if not _NAME.match(name):
        return "claude mcp accepts only letters, digits, hyphens and underscores in a name"
    return None


def _fatal(server: McpServer) -> str | None:
    """Why this server cannot be written to Claude Code at all (None: it can)."""
    problem = _name_problem(server.name)
    if problem:
        return problem
    if not server.url and not server.command:
        return "it has neither a command nor a url"
    for name in (*server.env_vars, *(h.env_var for h in server.headers)):
        if not _VAR.match(name):
            return f"variable name {name!r} cannot be written as ${{NAME}}"
    for key, value in server.env:
        if "${" in value:
            return f"env {key} holds a ${{...}} that Claude Code would expand as a variable"
    for header in server.headers:
        if not _HEADER_NAME.match(header.name):
            return f"header name {header.name!r} is not a valid HTTP header name"
        if header.scheme is not None and not _SCHEME.match(header.scheme):
            return f"header scheme {header.scheme!r} cannot be written"
    return None


def _lost_fields(server: McpServer) -> list[str]:
    """Fields of a writable server that Claude Code's user-scope entry has no place for."""
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
    else:
        if server.cwd:
            lost.append("cwd")
        if server.headers:
            lost.append("headers")
    return lost


def encode_server(server: McpServer) -> dict:
    """The Claude Code entry for `server`. Holds variable NAMES only, never a secret value."""
    if server.url:
        entry: dict = {"type": "http", "url": server.url}
        if server.headers:
            entry["headers"] = {
                h.name: (f"{h.scheme} " if h.scheme else "") + "${" + h.env_var + "}"
                for h in server.headers
            }
        return entry
    entry = {"type": "stdio", "command": server.command or "", "args": list(server.args)}
    environment = dict(server.env)
    environment.update({name: "${" + name + "}" for name in server.env_vars})
    if environment:
        entry["env"] = environment
    return entry


def decode_server(name: str, entry: Mapping) -> McpServer:
    """The neutral view of a Claude Code entry (the user's included).

    Lossy on purpose where the model cannot carry a value: a header that is not a variable
    reference, an env value that is a secret-looking literal or a reference to a variable of
    another name (or with a default). Those are skipped here; they are never copied into the
    neutral model.
    """
    if _kind(entry) == "http" or (entry.get("type") is None and entry.get("url")):
        url = entry.get("url") if isinstance(entry.get("url"), str) else None
        headers = []
        raw_headers = entry.get("headers")
        for key, value in raw_headers.items() if isinstance(raw_headers, dict) else ():
            match = _HEADER_REF.match(value) if isinstance(value, str) else None
            if match:
                headers.append(HeaderRef(str(key), match["var"], match["scheme"]))
        return McpServer(name=name, url=url, headers=tuple(headers))
    literals: list[tuple[str, str]] = []
    passed: list[str] = []
    environment = entry.get("env")
    for key, value in environment.items() if isinstance(environment, dict) else ():
        if not isinstance(value, str):
            continue
        ref = _ENV_REF.match(value)
        if ref:
            if ref.group(1) == key:
                passed.append(str(key))
            continue  # a reference to a variable of another name: not representable
        if "${" in value or env_value_looks_secret(key, value):
            continue
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
        env_vars=tuple(passed),
        env=tuple(literals),
    )


# -- comparing and patching entries ---------------------------------------------------------------


def _owned_view(entry: Mapping) -> dict:
    """The part of an entry this adapter owns, in a form where "same meaning" is "equal".

    A missing `type` is stdio, `streamable-http` is `http`, a missing args/env/headers is empty,
    and a `.cmd` launcher shim equals the bare launcher. Keys the user added are not part of it.
    """
    return {
        "type": _kind(entry),
        "command": _canon_command(entry.get("command")),
        "args": entry.get("args") or [],
        "url": entry.get("url"),
        "env": entry.get("env") or {},
        "headers": entry.get("headers") or {},
    }


def _patched(old: Mapping, fresh: Mapping) -> dict:
    """`old` with its owned keys replaced by `fresh`'s; every other key stays, in place."""
    new: dict = {}
    for key, value in old.items():
        if key not in _OWNED_KEYS:
            new[key] = value
        elif key in fresh:
            new[key] = fresh[key]
    for key, value in fresh.items():
        new.setdefault(key, value)
    return new


_REFERENCE_ONLY = re.compile(r"(?:[A-Za-z][A-Za-z0-9._-]* )?\$\{[^}]+\}")
_CREDENTIAL_KEY = re.compile(r"(?i)authorization|cookie")


def _secret_field(node: object, path: tuple[str, ...] = ()) -> str | None:
    """The dotted path of the first string in `node` that looks like a secret (None: none does).

    Walks every nested string with the project's one set of detectors (`_core.mask`): an env value
    judged together with its key, a header such as `Authorization`, an `oauth` field, an argument
    after `--token`. A value that is only a `${NAME}` reference (optionally after a scheme word,
    `Bearer ${NAME}`) is a name, not a secret. Only key names are returned, never a value.
    """
    key = path[-1] if path else ""
    where = ".".join(path) or "(entry)"
    if isinstance(node, str):
        if _REFERENCE_ONLY.fullmatch(node):
            return None
        credential = _CREDENTIAL_KEY.search(key) and node.strip()
        if env_value_looks_secret(key, node) or credential or mask_text(node) != node:
            return where
        return None
    if isinstance(node, Mapping):
        for name, value in node.items():
            found = _secret_field(value, (*path, str(name)))
            if found:
                return found
        return None
    if isinstance(node, (list, tuple)):
        strings = [x for x in node if isinstance(x, str)]
        if strings and mask_args(strings) != strings:
            return where
        for item in node:
            found = _secret_field(item, path)
            if found:
                return found
    return None


def _secret_reason(field: str) -> str:
    return (
        f"the entry holds a secret-looking value ({field}) that would end up on a command line "
        "and in the journal; move it to an environment variable (${NAME}) or change the entry "
        "by hand; left alone"
    )


def _compact(entry: Mapping) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def _shim_problem(binary: Path | None, platform: str, argvs: Sequence[Sequence[str]]) -> str | None:
    """Windows `.cmd` / `.bat` shims re-parse their arguments in cmd.exe, which mangles some
    characters. Refuse instead of passing them (None: fine)."""
    if platform != "windows" or binary is None or binary.suffix.lower() not in (".cmd", ".bat"):
        return None
    for argv in argvs:
        if any(ch in _SHIM_UNSAFE for arg in argv for ch in arg):
            return (
                "the claude CLI is a Windows .cmd shim, which cannot pass the characters "
                "% & | < > ^ ! safely; use claude.exe or set " + BIN_ENV
            )
    return None


# -- the adapter ---------------------------------------------------------------------------------


class ClaudeCodeAdapter(ToolAdapter):
    id: ClassVar[str] = "claude"
    label: ClassVar[str] = "Claude Code"
    capabilities: ClassVar[frozenset[Kind]] = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})

    # -- discovery ---------------------------------------------------------------------------

    def detect(self, env: Env) -> Detection:
        root = config_dir(env)
        binary, source, bin_warning = locate_cli(env)
        warnings = []
        value = env.environ.get(CONFIG_DIR_ENV)
        if value and config_override(env) is None:
            warnings.append(f"{CONFIG_DIR_ENV} is not an absolute path and is ignored")
        if bin_warning:
            warnings.append(bin_warning)
        configured = root.is_dir() or state_file(env).is_file()
        if binary is None and configured:
            warnings.append(
                "the claude CLI was not found: MCP changes cannot be applied "
                f"(put it on PATH or set {BIN_ENV})"
            )
        return Detection(
            installed=binary is not None or configured,
            binary=binary,
            binary_source=source,
            version=None,  # reading it would start the program; detect() must stay read-only
            config_roots=(root,),
            warnings=tuple(warnings),
        )

    def version_probe(self, env: Env, binary: Path) -> VersionProbe | None:
        return VersionProbe((str(binary), "--version"), parse_version_output)

    def paths(self, env: Env, scope: Scope) -> Paths:
        if scope is not Scope.GLOBAL:
            return Paths()
        return Paths(
            instructions=config_dir(env) / INSTRUCTIONS_FILE,
            mcp=state_file(env),
            skills_dir=skills_dir(env),
        )

    # -- reading -----------------------------------------------------------------------------

    def read_instructions(self, env: Env, scope: Scope) -> Instructions | None:
        path = self.paths(env, scope).instructions
        if path is None or not path.is_file():
            return None
        return Instructions(path=path, text=path.read_bytes().decode("utf-8", errors="replace"))

    def read_mcp(self, env: Env, scope: Scope) -> dict[str, McpServer]:
        """The user-scope servers in `.claude.json`. Raises `ConfigUnreadable` if it cannot be
        parsed. Project (local) servers under `projects` are not read."""
        path = self.paths(env, scope).mcp
        if path is None:
            return {}
        config = load_config(path)
        return {
            name: decode_server(name, entry)
            for name, entry in config.members.items()
            if _entry_shaped(entry) and _representable(entry)
        }

    def import_notes(self, env: Env, scope: Scope = Scope.GLOBAL) -> list[ImportNote]:
        path = self.paths(env, scope).mcp
        if path is None or not path.is_file():
            return []
        try:
            config = load_config(path)
        except ConfigUnreadable:
            return []
        notes: list[ImportNote] = []
        for name, entry in sorted(config.members.items()):
            if not isinstance(entry, Mapping):
                continue
            raw_headers = entry.get("headers")
            if isinstance(raw_headers, Mapping):
                for key, value in sorted(raw_headers.items()):
                    if isinstance(value, str):
                        match = _HEADER_REF.match(value)
                        if not match:
                            notes.append(
                                ImportNote(
                                    tool=self.id,
                                    server=name,
                                    field="header",
                                    name=str(key),
                                )
                            )
            environment = entry.get("env")
            if isinstance(environment, Mapping):
                for key, value in sorted(environment.items()):
                    if isinstance(value, str):
                        ref = _ENV_REF.match(value)
                        if ref and ref.group(1) == key:
                            continue
                        if "${" in value:
                            continue
                        if env_value_looks_secret(str(key), value):
                            notes.append(
                                ImportNote(
                                    tool=self.id,
                                    server=name,
                                    field="env",
                                    name=str(key),
                                )
                            )
        return notes

    def read_skills(self, env: Env, scope: Scope) -> list[SkillRef]:
        return read_skill_dir(self.paths(env, scope).skills_dir, RESERVED_SKILL_DIRS)

    # -- planning ----------------------------------------------------------------------------

    def plan_instructions(self, env: Env, scope: Scope, desired: str, owned: Owned) -> list[Op]:
        if scope is not Scope.GLOBAL:
            return [self._project_unsupported(Kind.INSTRUCTIONS)]
        path = self.paths(env, scope).instructions
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
        argv0, binary, _warning = find_cli(env)
        cli = [argv0 or CLI_NAME, "mcp"]
        scope_args = ["-s", MCP_SCOPE]
        refused: list[Conflict] = []
        gaps: list[str] = []
        disabled: set[str] = set()
        steps: list[tuple[str, list[RunCli]]] = []  # (server name, its ops in order)

        def add_op(name: str, entry: dict) -> RunCli:
            return RunCli(
                argv=[*cli, "add-json", name, _compact(entry), *scope_args],
                inverse_argv=[*cli, "remove", name, *scope_args],
                description=f"add MCP server {name} to Claude Code",
                claims=(ItemRef(self.id, Kind.MCP, name),),
            )

        def remove_op(name: str, old: dict, *, release: bool, why: str) -> RunCli:
            return RunCli(
                argv=[*cli, "remove", name, *scope_args],
                inverse_argv=[*cli, "add-json", name, _compact(old), *scope_args],
                description=f"{why} MCP server {name} in Claude Code",
                releases=(ItemRef(self.id, Kind.MCP, name),) if release else (),
            )

        for name, server in desired.items():
            if not server.enabled:
                disabled.add(name)
                gaps.append(
                    f"{name}: disabled in the source; Claude Code has no per-server switch, "
                    "so it is not written"
                )
                continue
            fatal = _fatal(server)
            if fatal:
                gaps.append(f"{name}: not written, {fatal}")
                continue
            lost = _lost_fields(server)
            if lost:
                gaps.append(f"{name}: {', '.join(lost)} not stored")
            fresh = encode_server(server)
            if name not in config.members:
                steps.append((name, [add_op(name, fresh)]))
                continue
            old = config.members[name]
            if not isinstance(old, dict):
                reason = "the entry is not an object; left alone"
                refused.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            if _owned_view(old) == _owned_view(fresh):
                continue  # already as wanted (an identical entry of the user's is left alone too)
            if name not in owned:
                reason = "an MCP server with this name exists and was not created by all-agents"
                refused.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            leaked = _secret_field(old)
            if leaked:
                refused.append(Conflict(self.id, Kind.MCP, name, _secret_reason(leaked), path))
                continue
            steps.append(
                (
                    name,
                    [
                        remove_op(name, old, release=False, why="replace (remove old)"),
                        add_op(name, _patched(old, fresh)),
                    ],
                )
            )
        for name in sorted(owned - (desired.keys() - disabled)):
            if name not in config.members:
                continue
            old = config.members[name]
            if not isinstance(old, dict) or _name_problem(name):
                reason = "the owned entry cannot be removed safely through the CLI; left alone"
                refused.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            leaked = _secret_field(old)
            if leaked:
                refused.append(Conflict(self.id, Kind.MCP, name, _secret_reason(leaked), path))
                continue
            steps.append((name, [remove_op(name, old, release=True, why="remove")]))

        ops: list[Op] = list(refused)
        if steps:
            problem = None
            if binary is None:
                problem = (
                    "the claude CLI was not found, so the MCP server cannot be changed "
                    f"(put it on PATH or set {BIN_ENV})"
                )
            else:
                argvs = [list(op.argv) + list(op.inverse_argv) for _n, g in steps for op in g]
                problem = _shim_problem(binary, env.platform, argvs)
            if problem:
                ops += [Conflict(self.id, Kind.MCP, name, problem, path) for name, _g in steps]
            else:
                ops += [op for _name, group in steps for op in group]
        if gaps:
            ops.append(Unsupported(self.id, Kind.MCP, "; ".join(gaps)))
        return ops

    def plan_skills(
        self, env: Env, scope: Scope, desired: Sequence[SkillRef], owned: Owned
    ) -> list[Op]:
        if scope is not Scope.GLOBAL:
            return [self._project_unsupported(Kind.SKILLS)]
        directory = self.paths(env, scope).skills_dir
        return plan_skill_links(self.id, directory, desired, owned)

    # -- normalization -------------------------------------------------------------------------

    def normalize_mcp(self, servers: Mapping[str, McpServer]) -> dict[str, McpServer]:
        """What `read_mcp` returns after `servers` were written (for writable servers).

        A server with `enabled=False` is not written, so it is not in the result. Lost for the
        others: `startup_timeout_sec`, `cwd` and `auth` (OAuth is the default; nothing is written
        for it); for a remote server also command, args, env and env_vars; for a local one the
        headers. The `.cmd` suffix of a launcher is not part of the neutral view.
        """
        out = {}
        for name, server in servers.items():
            if not server.enabled:
                continue
            server = replace(_canon_server(server), startup_timeout_sec=None, auth=None, cwd=None)
            if server.url:
                server = replace(server, command=None, args=(), env=(), env_vars=())
            else:
                server = replace(server, headers=())
            out[name] = server
        return out

    def notes(self) -> list[str]:
        return [
            "global scope only; project scope arrives in v0.2",
            "MCP goes through `claude mcp add-json/remove -s user` because running sessions also "
            "write ~/.claude.json; the claude CLI must be on PATH (or set ALL_AGENTS_CLAUDE_BIN), "
            "otherwise the MCP changes are reported as conflicts and skipped",
            "the file is only read: a server added by Claude Code between the plan and the apply "
            "makes the add fail and the engine reverts; a change in between is not detected "
            "(a RunCli has no drift check)",
            "MCP: cwd and startup_timeout_sec cannot be stored; a remote server cannot carry "
            "args, env, env_vars or cwd; OAuth is Claude Code's default and is not written",
            "MCP: a server disabled in the source (enabled=false) is not written, because Claude "
            "Code has no per-server switch; an owned one is removed, one that is not owned is "
            "left alone",
            "MCP: a secret is referenced as ${NAME}; a literal secret in the user's own entry "
            "is never read into the neutral model, and an owned entry that holds one is not "
            "removed or replaced (it would reach a command line and the journal): a conflict",
            "CLAUDE_CONFIG_DIR (absolute) moves CLAUDE.md, skills and .claude.json; a relative "
            "value is ignored",
            "Windows: npx, npm, pnpm and yarn are written as plain names, not .cmd (as in the "
            "prototype; unverified with a live tool); a claude.cmd shim is refused for arguments "
            "with % & | < > ^ !",
            "skills are linked into <config>/skills (Claude Code does not read ~/.agents/skills); "
            ".trash and synced are never touched",
            "unverified: CLAUDE_CONFIG_DIR with a live tool, symlink-following for skills on "
            "Linux and macOS; everything on macOS",
        ]

    # -- helpers -------------------------------------------------------------------------------

    def _project_unsupported(self, kind: Kind) -> Unsupported:
        return Unsupported(self.id, kind, "project scope is not supported before v0.2")
