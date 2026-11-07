"""Codex adapter: global scope, instructions + MCP servers + skills.

Adapters detect, read and plan; they never write (docs/decisions/0008). Everything here returns
`Op` objects; the engine runs them. Settled by docs/decisions/0009 (interface), 0010 (engine) and
0012 (this adapter, and the shared skill links).

Where Codex keeps things (global scope)
---------------------------------------

    <codex home>/AGENTS.md        instructions (owned as one item, `INSTRUCTIONS_KEY`)
    <codex home>/config.toml      MCP servers: `[mcp_servers.<name>]` tables
    <home>/.agents/skills/<name>  skills (one link per skill, docs/decisions/0007)

`<codex home>` is `$CODEX_HOME` when it is set to an absolute path (read from `env.environ`, never
from the OS), else `<home>/.codex`. [docs: Codex's `find_codex_home` reads `CODEX_HOME`, ignores an
empty value, and refuses to start when the folder does not exist; an adapter cannot resolve a
relative value against Codex's working directory, so a relative one is ignored and `detect` warns.]
Skills are NOT under the Codex home: Codex reads `<home>/.agents/skills` (the folder OpenCode reads
too, so a skill is linked once for both tools) and the deprecated `$CODEX_HOME/skills`, which this
adapter does not manage. [docs: `ext/skills/src/host_roots.rs` in the Codex repository.]
`AGENTS.override.md` in the Codex home, when it is not empty, is read INSTEAD of `AGENTS.md`
[docs]; that makes the instructions this adapter writes ineffective, so `plan_instructions` adds a
`Conflict` for it (the override file is never touched).

MCP: `config.toml` is edited as text (`_core/toml_edit.py`)
------------------------------------------------------------

Only `[mcp_servers.<name>]` tables are touched. A table that is new or must change is written
inside a managed region (`# >>> one-setup-all-agents >>>` ... `# <<< ... <<<`) at the end of the
file; everything else (other tables, comments, key order, the BOM and CRLF line endings) stays as
it was. A table that changes loses the comments that sat inside it. Before an op is returned, the
edit is tried on the bytes it will receive and the result is parsed back and compared (`tomllib`):
if the file cannot be edited safely (it does not parse, a bare `[mcp_servers]` table, a server
defined as an inline table, nested tables below the server) each affected server is reported as a
`Conflict` and nothing is written.

Entry format (all keys verified in the Codex configuration source, `mcp_types.rs`):

    [mcp_servers.<name>]                       stdio
    command = "npx"   args = ["-y", "pkg"]   cwd = "..."
    enabled = false                             (only when disabled)
    startup_timeout_sec = 60
    env_vars = ["TOKEN"]                        names of variables Codex passes on
    [mcp_servers.<name>.env]                    literal NON-secret values
    MODE = "test"

    [mcp_servers.<name>]                       http
    url = "https://..."
    bearer_token_env_var = "TOKEN"              Authorization: Bearer <value of TOKEN>
    [mcp_servers.<name>.env_http_headers]       header name -> variable name, whole value
    X-Demo = "DEMO_VAR"

A secret is referenced by variable NAME only, in Codex's own way: `env_vars`, `bearer_token_env_var`
and `env_http_headers`. Codex expands nothing inside `env` or `args`, so a literal is only a
literal. A header can be written only as the whole value of a variable or as `Authorization:
Bearer <variable>`; any other header form makes the server "not written" (reported, never faked).
A literal `http_headers` of the user's is not part of the neutral model and is left alone.

Ownership of an entry's keys: `command`, `args`, `url`, `env`, `env_vars`, `cwd`, `enabled`,
`bearer_token_env_var`, `env_http_headers`, and `startup_timeout_sec` only when the source sets one
(a timeout the user added by hand to an owned server survives; the source saying nothing is "no
opinion"). Every other key of the user's entry (`tool_timeout_sec`, `required`, `http_headers`,
...) is kept. "Equal" means equal on those keys with Codex's defaults filled in, so an equal entry
of the user's is left alone.

What Codex cannot store is reported as `Unsupported` (never dropped silently): for an http server
`command`, `args`, `env`, `env_vars`, `cwd`; for a stdio server `headers`. `auth` is documentation
only and is not written. `enabled` and `startup_timeout_sec` ARE stored natively.

Windows launchers (`npx`, `npm`, `pnpm`, `yarn`) are written with the `.cmd` suffix (plan
decision, same as OpenCode); reading strips it again, so the neutral view is the same on every
platform. Codex resolves the program through `PATHEXT` itself [docs: `program_resolver`], so the
plain name would also work; the suffix is kept for a consistent global config. [UNVERIFIED with a
live tool in this repository.]

Not verified: that Codex follows skill symlinks on Linux and macOS (0007 open point), `CODEX_HOME`
on Linux and macOS with a live tool, and anything on macOS at all (CI only).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from .._core import toml_edit
from .._core.mask import env_value_looks_secret
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
    McpServer,
    Op,
    Owned,
    Paths,
    Scope,
    SkillRef,
    Unsupported,
    VersionProbe,
)
from ._common import (
    decode_text,
    encode_text,
    find_binary,
    is_absolute,
    parse_version,
    plan_skill_links,
    plan_whole_file_instructions,
    read_skill_dir,
    sha256_hex,
)
from .base import ToolAdapter

CONFIG_HOME_ENV = "CODEX_HOME"
BINARY = "codex"
#: What `--version` prints, as measured: `codex-cli 0.146.0`.
VERSION_BANNER = r"\bcodex-cli\s+v?(\d\S*)"
DEFAULT_DIR = ".codex"
CONFIG_FILE = "config.toml"
INSTRUCTIONS_FILE = "AGENTS.md"
OVERRIDE_FILE = "AGENTS.override.md"

#: Launchers that Windows resolves only with a `.cmd` suffix (same set as OpenCode).
WIN_SHIMS = frozenset({"npx", "npm", "pnpm", "yarn"})

#: Keys of an MCP table that this adapter writes. Any other key of the user's table is kept.
#: `startup_timeout_sec` is owned only when the source sets one (see `_patched`).
_OWNED_KEYS = frozenset(
    {
        "command",
        "args",
        "url",
        "env",
        "env_vars",
        "cwd",
        "enabled",
        "bearer_token_env_var",
        "env_http_headers",
        "startup_timeout_sec",
    }
)

_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
#: What `codex mcp add` accepts as a server name [docs: `validate_server_name`].
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_:@/.-]+$")


class ConfigUnreadable(ValueError):
    """The Codex config file exists but cannot be understood. Never overwritten."""


# -- paths -------------------------------------------------------------------------------------


def codex_home_warning(env: Env) -> str | None:
    """Why `CODEX_HOME` is ignored (None when it is unset, empty or usable)."""
    value = env.environ.get(CONFIG_HOME_ENV)
    if value and not is_absolute(value, env.platform):
        return (
            f"{CONFIG_HOME_ENV} is not an absolute path; it is ignored here, "
            "because Codex would resolve it against its own working directory"
        )
    return None


def codex_home(env: Env) -> Path:
    """`$CODEX_HOME` (absolute only) or `<home>/.codex`, on every platform."""
    value = env.environ.get(CONFIG_HOME_ENV)
    if value and is_absolute(value, env.platform):
        return Path(value)
    return Path(env.home) / DEFAULT_DIR


def config_file(env: Env) -> Path:
    return codex_home(env) / CONFIG_FILE


def skills_dir(env: Env) -> Path:
    return Path(env.home) / ".agents" / "skills"


# -- reading the config file -------------------------------------------------------------------


@dataclass(frozen=True)
class _Config:
    path: Path
    raw: bytes | None  # None: the file does not exist
    doc: dict  # the whole parsed document ({} when there is none)
    servers: dict  # every member of `mcp_servers`, by name

    @property
    def blank(self) -> bool:
        return self.raw is None or not self.raw.strip()


def _parse(raw: bytes, name: str) -> dict:
    try:
        text, _bom, _crlf = decode_text(raw)
        doc = tomllib.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:  # TOMLDecodeError is a ValueError
        raise ConfigUnreadable(f"cannot parse {name} as TOML ({exc})") from exc
    return doc


def load_config(path: Path) -> _Config:
    """Parse the file (read-only). Raises `ConfigUnreadable` with a reason that has no content."""
    path = Path(path)
    if not path.is_file():
        return _Config(path, None, {}, {})
    raw = path.read_bytes()
    if not raw.strip():
        return _Config(path, raw, {}, {})
    doc = _parse(raw, path.name)
    servers = doc.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ConfigUnreadable('"mcp_servers" is not a table')
    return _Config(path, raw, doc, dict(servers))


# -- neutral model <-> Codex table ---------------------------------------------------------------


def _canon_command(command: object) -> object:
    """A command with the Windows `.cmd` suffix removed from a launcher shim."""
    if isinstance(command, str) and command.lower().endswith(".cmd"):
        if command[:-4].lower() in WIN_SHIMS:
            return command[:-4]
    return command


def _bearer(scheme: str | None) -> bool:
    return scheme is not None and scheme.lower() == "bearer"


def _canon_headers(headers: Sequence[HeaderRef]) -> tuple[HeaderRef, ...]:
    """Headers as Codex stores them: one spelling of `Authorization: Bearer`, sorted by name."""
    out = []
    for h in headers:
        if h.name.lower() == "authorization" and _bearer(h.scheme):
            h = HeaderRef("Authorization", h.env_var, "Bearer")
        out.append(h)
    return tuple(sorted(out, key=lambda h: (h.name, h.env_var)))


def _canon_server(server: McpServer) -> McpServer:
    server = replace(server, headers=_canon_headers(server.headers))
    if server.command:
        server = replace(server, command=_canon_command(server.command))
    return server


def _fatal(name: str, server: McpServer) -> str | None:
    """Why this server cannot be written to Codex at all (None: it can)."""
    if not _SERVER_NAME.match(name):
        return f"{name!r} is not a server name Codex accepts"
    if not server.url and not server.command:
        return "it has neither a command nor a url"
    for var in server.env_vars:
        if not _VAR.match(var):
            return f"variable name {var!r} is not a valid environment variable name"
    seen: set[str] = set()
    for header in server.headers:
        if not _HEADER_NAME.match(header.name):
            return f"header name {header.name!r} is not a valid HTTP header name"
        if not _VAR.match(header.env_var):
            return f"variable name {header.env_var!r} is not a valid environment variable name"
        if header.name.lower() in seen:
            return f"header {header.name} appears twice"
        seen.add(header.name.lower())
        authorization = header.name.lower() == "authorization"
        if header.scheme is not None and not (authorization and _bearer(header.scheme)):
            return (
                "Codex reads a header from a variable only as the whole value or as "
                f"`Authorization: Bearer <variable>` (header {header.name})"
            )
    return None


def _lost_fields(server: McpServer) -> list[str]:
    """Fields of a writable server that the Codex format has no place for."""
    if server.url:
        return [
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
    return ["headers"] if server.headers else []


def encode_server(server: McpServer, *, windows: bool) -> dict:
    """The Codex table for `server`. Holds variable NAMES only, never a secret value."""
    entry: dict = {}
    if server.url:
        entry["url"] = server.url
        for header in server.headers:
            if header.name.lower() == "authorization" and _bearer(header.scheme):
                entry["bearer_token_env_var"] = header.env_var
            else:
                entry.setdefault("env_http_headers", {})[header.name] = header.env_var
    else:
        command = server.command or ""
        if windows and command in WIN_SHIMS:
            command += ".cmd"
        entry["command"] = command
        entry["args"] = list(server.args)
        if server.cwd:
            entry["cwd"] = server.cwd
        if server.env_vars:
            entry["env_vars"] = list(server.env_vars)
        if server.env:
            entry["env"] = dict(server.env)
    if not server.enabled:
        entry["enabled"] = False
    if server.startup_timeout_sec is not None:
        entry["startup_timeout_sec"] = server.startup_timeout_sec
    return entry


def _seconds(value: object) -> int | None:
    """A timeout as the neutral model holds it: whole seconds (Codex also takes fractions)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _names(value: object) -> list[str]:
    """Variable names of an `env_vars` list: plain names and `{name, source = "local"}` tables."""
    out = []
    for item in value if isinstance(value, list) else ():
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            if item.get("source") in (None, "local"):
                out.append(item["name"])
    return out


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def decode_server(name: str, entry: Mapping) -> McpServer:
    """The neutral view of a Codex table (the user's included).

    Lossy on purpose where the model cannot carry a value: a literal `http_headers`, a secret-
    looking literal in `env`, an `env_vars` entry for the remote side. Those are skipped here;
    they are never copied into the neutral model.
    """
    enabled = entry.get("enabled") is not False
    timeout = _seconds(entry.get("startup_timeout_sec"))
    url = entry.get("url")
    if isinstance(url, str) and url:
        headers = []
        bearer = entry.get("bearer_token_env_var")
        if isinstance(bearer, str) and bearer:
            headers.append(HeaderRef("Authorization", bearer, "Bearer"))
        refs = entry.get("env_http_headers")
        for key, var in refs.items() if isinstance(refs, dict) else ():
            if isinstance(var, str) and var:
                headers.append(HeaderRef(str(key), var, None))
        return McpServer(
            name=name,
            url=url,
            headers=_canon_headers(headers),
            enabled=enabled,
            startup_timeout_sec=timeout,
        )
    passed = _names(entry.get("env_vars"))
    literals: list[tuple[str, str]] = []
    env = entry.get("env")
    for key, value in env.items() if isinstance(env, dict) else ():
        if not isinstance(value, str) or key in passed:
            continue
        if env_value_looks_secret(str(key), value):
            continue
        literals.append((str(key), value))
    command = entry.get("command")
    return McpServer(
        name=name,
        command=_canon_command(command) if isinstance(command, str) and command else None,
        args=tuple(_strings(entry.get("args"))),
        env_vars=tuple(passed),
        env=tuple(literals),
        cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
        enabled=enabled,
        startup_timeout_sec=timeout,
    )


def _entry_shaped(value: object) -> bool:
    return isinstance(value, dict) and ("command" in value or "url" in value)


# -- comparing and patching a table ------------------------------------------------------------


def _canon_entry(entry: Mapping, *, timeout: bool) -> dict:
    """The owned keys of a table with Codex's defaults filled in: what 'equal' is measured on."""

    def text(key: str) -> object:
        value = entry.get(key)
        return value if isinstance(value, str) and value else None

    def table(key: str) -> dict:
        value = entry.get(key)
        return dict(value) if isinstance(value, dict) else {}

    out = {
        "command": _canon_command(text("command")),
        "args": _strings(entry.get("args")),
        "url": text("url"),
        "cwd": text("cwd"),
        "env": table("env"),
        "env_vars": _names(entry.get("env_vars")),
        "enabled": entry.get("enabled") is not False,
        "bearer_token_env_var": text("bearer_token_env_var"),
        "env_http_headers": table("env_http_headers"),
    }
    if timeout:
        out["startup_timeout_sec"] = entry.get("startup_timeout_sec")
    return out


def _same(old: Mapping, fresh: Mapping) -> bool:
    timeout = "startup_timeout_sec" in fresh
    return _canon_entry(old, timeout=timeout) == _canon_entry(fresh, timeout=timeout)


def _patched(old: Mapping, fresh: Mapping) -> dict:
    """`old` with the keys this adapter owns replaced by `fresh`; the user's other keys stay."""
    owned = _OWNED_KEYS - (set() if "startup_timeout_sec" in fresh else {"startup_timeout_sec"})
    new = {k: v for k, v in old.items() if k not in owned}
    new.update(fresh)
    return new


# -- the edit of the config file ---------------------------------------------------------------


@dataclass(frozen=True)
class _McpEdit:
    """Everything the transform needs; computed at plan time from the bytes it will receive."""

    upserts: tuple[tuple[str, dict], ...] = ()  # new tables and replaced tables, whole
    removes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.upserts or self.removes)

    @property
    def names(self) -> list[str]:
        return [*(n for n, _ in self.upserts), *self.removes]


def _apply_edit(current: bytes | None, spec: _McpEdit) -> bytes:
    """The pure transform of the config file. Raises `ValueError` if the result is not as meant."""
    if current is None or not current.strip():
        text, bom, crlf, old_doc = "", False, False, {}
    else:
        text, bom, crlf = decode_text(current)
        old_doc = _parse(current, "the file")
    try:
        new_text = toml_edit.edit(text, list(spec.removes), [(n, e, None) for n, e in spec.upserts])
    except toml_edit.TomlEditError as exc:
        raise ValueError(f"cannot edit the Codex config ({exc})") from exc
    _check_result(old_doc, new_text, spec)
    return encode_text(new_text, bom, crlf)


def _without(doc: dict, names: Sequence[str]) -> dict:
    out = dict(doc)
    servers = {k: v for k, v in out.get("mcp_servers", {}).items() if k not in names}
    if servers:
        out["mcp_servers"] = servers
    else:
        out.pop("mcp_servers", None)
    return out


def _check_result(old_doc: dict, new_text: str, spec: _McpEdit) -> None:
    """Fail (so nothing is written) if the edited text is not what the plan meant."""
    try:
        new_doc = tomllib.loads(new_text)
    except ValueError as exc:
        raise ValueError(f"the edited Codex config does not parse ({exc})") from exc
    if _without(new_doc, spec.names) != _without(old_doc, spec.names):
        raise ValueError("the edit would change something other than the planned servers")
    servers = new_doc.get("mcp_servers", {})
    for name, entry in spec.upserts:
        if servers.get(name) != entry:
            raise ValueError(f"the edited Codex config does not hold the server {name!r}")
    for name in spec.removes:
        if name in servers:
            raise ValueError(f"the edited Codex config still holds the server {name!r}")


# -- the adapter ---------------------------------------------------------------------------------


def parse_version_output(output: str) -> str | None:
    return parse_version(output, prefer=VERSION_BANNER)


class CodexAdapter(ToolAdapter):
    id: ClassVar[str] = "codex"
    label: ClassVar[str] = "Codex"
    capabilities: ClassVar[frozenset[Kind]] = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})

    # -- discovery ---------------------------------------------------------------------------

    def detect(self, env: Env) -> Detection:
        root = codex_home(env)
        found = find_binary(env, BINARY)
        binary = found.path if found else None
        warnings = []
        bad = codex_home_warning(env)
        if bad:
            warnings.append(bad)
        elif env.environ.get(CONFIG_HOME_ENV) and not root.is_dir():
            warnings.append(
                f"{CONFIG_HOME_ENV} points to a folder that does not exist; "
                "Codex refuses to start until it does"
            )
        if self._override_active(env):
            warnings.append(
                f"{OVERRIDE_FILE} exists in the Codex home and is read instead of "
                f"{INSTRUCTIONS_FILE}"
            )
        return Detection(
            installed=binary is not None or root.is_dir(),
            binary=binary,
            binary_source=found.source if found else None,
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
            instructions=codex_home(env) / INSTRUCTIONS_FILE,
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
        """The servers in `config.toml`. Raises `ConfigUnreadable` if it cannot be parsed."""
        path = self.paths(env, scope).mcp
        if path is None:
            return {}
        config = load_config(path)
        return {
            name: decode_server(name, entry)
            for name, entry in config.servers.items()
            if _entry_shaped(entry)
        }

    def read_skills(self, env: Env, scope: Scope) -> list[SkillRef]:
        return read_skill_dir(self.paths(env, scope).skills_dir)

    # -- planning ----------------------------------------------------------------------------

    def plan_instructions(self, env: Env, scope: Scope, desired: str, owned: Owned) -> list[Op]:
        if scope is not Scope.GLOBAL:
            return [self._project_unsupported(Kind.INSTRUCTIONS)]
        path = self.paths(env, scope).instructions
        ops = plan_whole_file_instructions(self.id, path, desired, owned)
        if self._override_active(env):
            reason = (
                f"{OVERRIDE_FILE} exists and is read instead of {INSTRUCTIONS_FILE}, so these "
                "instructions are not in effect; the override file is left alone"
            )
            ops.append(
                Conflict(
                    self.id,
                    Kind.INSTRUCTIONS,
                    INSTRUCTIONS_KEY,
                    reason,
                    codex_home(env) / OVERRIDE_FILE,
                )
            )
        return ops

    def plan_mcp(
        self, env: Env, scope: Scope, desired: Mapping[str, McpServer], owned: Owned
    ) -> list[Op]:
        if scope is not Scope.GLOBAL:
            return [self._project_unsupported(Kind.MCP)]
        path = self.paths(env, scope).mcp
        try:
            config = load_config(path)
        except ConfigUnreadable as exc:
            return self._refuse(path, desired, owned, str(exc))
        windows = env.platform == "windows"
        ops: list[Op] = []
        gaps: list[str] = []
        upserts: list[tuple[str, dict]] = []
        claimed: list[str] = []
        for name, server in desired.items():
            fatal = _fatal(name, server)
            if fatal:
                gaps.append(f"{name}: not written, {fatal}")
                continue
            lost = _lost_fields(server)
            if lost:
                gaps.append(f"{name}: {', '.join(lost)} not stored")
            fresh = encode_server(server, windows=windows)
            if name not in config.servers:
                upserts.append((name, fresh))
                claimed.append(name)
                continue
            old = config.servers[name]
            if isinstance(old, dict) and _same(old, fresh):
                continue  # already as wanted (an identical entry of the user's is left alone too)
            if name not in owned:
                reason = "an MCP server with this name exists and was not created by all-agents"
                ops.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            upserts.append((name, _patched(old, fresh) if isinstance(old, dict) else fresh))
            claimed.append(name)
        removals = tuple(n for n in sorted(owned - desired.keys()) if n in config.servers)
        spec = _McpEdit(tuple(upserts), removals)
        if not spec.empty:
            try:
                _apply_edit(config.raw, spec)  # a dry run on the very bytes the engine will pass
            except ValueError as exc:
                ops.extend(self._refuse(path, spec.names, (), str(exc), keep=False))
            else:
                ops.append(
                    EditFile(
                        path=path,
                        transform=lambda current, spec=spec: _apply_edit(current, spec),
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
        directory = self.paths(env, scope).skills_dir
        return plan_skill_links(self.id, directory, desired, owned)

    # -- normalization -------------------------------------------------------------------------

    def normalize_mcp(self, servers: Mapping[str, McpServer]) -> dict[str, McpServer]:
        """What `read_mcp` returns after `servers` were written (for writable servers).

        Lost: `auth` (documentation only; nothing is written for it); for an http server also
        command, args, env, env_vars and cwd; for a stdio one the headers. `Authorization: Bearer`
        has one spelling and headers are sorted by name. The Windows `.cmd` suffix of a launcher
        is not part of the neutral view. `enabled` and `startup_timeout_sec` are stored.
        """
        out = {}
        for name, server in servers.items():
            server = replace(_canon_server(server), auth=None)
            if server.url:
                server = replace(server, command=None, args=(), env=(), env_vars=(), cwd=None)
            else:
                server = replace(server, headers=())
            out[name] = server
        return out

    def notes(self) -> list[str]:
        return [
            "global scope only; project scope arrives in v0.2",
            "skills are linked into ~/.agents/skills, which Codex reads (OpenCode reads it too); "
            "$CODEX_HOME/skills is deprecated and not managed",
            "instructions: AGENTS.override.md, when present and not empty, is read instead of "
            "AGENTS.md and makes these instructions ineffective (reported as a conflict)",
            "MCP: config.toml is edited as text inside a managed region; comments inside a "
            "changed server table are lost, everything else is kept",
            "MCP: a remote server cannot carry command, args, env, env_vars or cwd; a local one "
            "cannot carry headers; a header is stored only as a whole variable or "
            "Authorization: Bearer <variable>",
            "MCP: enabled and startup_timeout_sec are stored natively",
            "Windows: npx, npm, pnpm and yarn are written as .cmd (unverified with a live tool)",
            "unverified: symlink-following for skills on Linux and macOS; everything on macOS",
        ]

    # -- helpers -------------------------------------------------------------------------------

    def _override_active(self, env: Env) -> bool:
        override = codex_home(env) / OVERRIDE_FILE
        try:
            return override.is_file() and bool(override.read_bytes().strip())
        except OSError:
            return False

    def _refuse(self, path: Path, desired, owned, why: str, *, keep: bool = True) -> list[Op]:
        """One `Conflict` per item that would have been written; the file is left alone."""
        names = sorted(set(desired) | set(owned)) if keep else list(desired)
        reason = f"{why}; the file is left alone"
        return [Conflict(self.id, Kind.MCP, name, reason, path) for name in names]

    def _project_unsupported(self, kind: Kind) -> Unsupported:
        return Unsupported(self.id, kind, "project scope is not supported before v0.2")
