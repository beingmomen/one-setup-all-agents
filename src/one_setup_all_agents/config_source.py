"""Shared source layout loader, renderer, and validator.

Reads and writes the unified agent configuration from the source directory
(docs/decisions/0003-source-layout-on-disk.md and docs/decisions/0015-source-layout-v1.md).
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._core import jsonc
from ._core.mask import env_value_looks_secret, mask_args, mask_text
from .engine.plan import DesiredState
from .model import HeaderRef, McpServer, SkillRef

SOURCE_CONFIG_NAME = "config.toml"
SOURCE_VERSION = 1
INSTRUCTIONS_NAME = "instructions.md"
MCP_NAME = "mcp.jsonc"
SKILLS_DIRNAME = "skills"

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_VAR_RE = re.compile(r"^(?:([A-Za-z0-9_-]+)\s+)?\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_BIDI_RE = re.compile(r"[\u200E\u200F\u061C\u202A-\u202E\u2066-\u2069]")


class SourceError(Exception):
    """Raised when the source configuration is invalid or cannot be read."""


@dataclass(frozen=True)
class ToolSettings:
    """Per-tool settings declared in the shared config file."""

    enabled: bool = True
    skills_exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServerEntry:
    """An MCP server declaration with its target tool selector."""

    server: McpServer
    tools: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SourceConfig:
    """Complete in-memory model of the shared source configuration directory."""

    path: Path
    tools: Mapping[str, ToolSettings]
    instructions_core: str | None
    appendices: Mapping[str, str]
    servers: Mapping[str, ServerEntry]
    skills: tuple[SkillRef, ...]
    warnings: tuple[str, ...] = ()


def source_exists(config_dir: Path) -> bool:
    """Return True if the shared source config file exists."""
    return (config_dir / SOURCE_CONFIG_NAME).is_file()


def load_source(config_dir: Path, adapter_ids: Sequence[str]) -> SourceConfig:
    """Load and validate the shared source directory against known adapter IDs."""
    config_file = config_dir / SOURCE_CONFIG_NAME
    if not config_file.is_file():
        raise SourceError(f"source config file '{SOURCE_CONFIG_NAME}' not found in {config_dir}")

    try:
        raw_toml = config_file.read_bytes().decode("utf-8-sig")
        toml_data = tomllib.loads(raw_toml)
    except Exception as exc:
        raise SourceError(f"failed to parse '{SOURCE_CONFIG_NAME}': {exc}") from exc

    version = toml_data.get("version")
    if version != SOURCE_VERSION:
        raise SourceError(
            f"unsupported source version {version}; this release understands {SOURCE_VERSION}"
        )

    for key in toml_data:
        if key not in ("version", "tools", "projects"):
            raise SourceError(f"unknown top-level key '{key}' in '{SOURCE_CONFIG_NAME}'")

    valid_ids = set(adapter_ids)
    tools_map: dict[str, ToolSettings] = {}

    tools_table = toml_data.get("tools", {})
    if not isinstance(tools_table, dict):
        raise SourceError(f"'tools' must be a table in '{SOURCE_CONFIG_NAME}'")

    for tool_id, settings in tools_table.items():
        if tool_id not in valid_ids:
            raise SourceError(f"unknown tool id '{tool_id}' in '{SOURCE_CONFIG_NAME}'")
        if not isinstance(settings, dict):
            raise SourceError(f"settings for tool '{tool_id}' must be a table")
        for skey in settings:
            if skey not in ("enabled", "skills_exclude"):
                raise SourceError(f"unknown setting '{skey}' in [tools.{tool_id}]")
        enabled = settings.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SourceError(f"'enabled' in [tools.{tool_id}] must be a boolean")
        skills_ex = settings.get("skills_exclude", ())
        if not isinstance(skills_ex, (list, tuple)):
            raise SourceError(f"'skills_exclude' in [tools.{tool_id}] must be a list of strings")
        tools_map[tool_id] = ToolSettings(
            enabled=enabled, skills_exclude=tuple(str(s) for s in skills_ex)
        )

    servers_map: dict[str, ServerEntry] = {}
    mcp_file = config_dir / MCP_NAME
    if mcp_file.is_file():
        try:
            raw_mcp = mcp_file.read_bytes().decode("utf-8-sig")
            mcp_data = jsonc.loads(raw_mcp)
        except Exception as exc:
            raise SourceError(f"failed to parse '{MCP_NAME}': {exc}") from exc

        if not isinstance(mcp_data, dict):
            raise SourceError(f"root of '{MCP_NAME}' must be a JSON object")

        mcp_version = mcp_data.get("version")
        if mcp_version != SOURCE_VERSION:
            raise SourceError(
                f"unsupported source version {mcp_version}; "
                f"this release understands {SOURCE_VERSION}"
            )

        for key in mcp_data:
            if key not in ("version", "servers"):
                raise SourceError(f"unknown top-level key '{key}' in '{MCP_NAME}'")

        servers_obj = mcp_data.get("servers", {})
        if not isinstance(servers_obj, dict):
            raise SourceError(f"'servers' in '{MCP_NAME}' must be an object")

        for sname, sdata in servers_obj.items():
            if not isinstance(sdata, dict):
                raise SourceError(f"server '{sname}' must be an object")

            for legacy_key in ("retired", "unmanaged", "keep_live_env", "vars"):
                if legacy_key in sdata:
                    raise SourceError(
                        f"legacy key '{legacy_key}' in server '{sname}' is not supported; "
                        "see ADR 0015"
                    )

            # Check for {{VAR}} syntax anywhere in string values
            _check_no_double_curly(sname, sdata)

            allowed_fields = {
                "command",
                "args",
                "cwd",
                "url",
                "env",
                "env_vars",
                "headers",
                "startup_timeout_sec",
                "auth",
                "enabled",
                "tools",
                "description",
            }
            for field in sdata:
                if field not in allowed_fields:
                    raise SourceError(f"unknown field '{field}' in server '{sname}'")

            url = sdata.get("url")
            command = sdata.get("command")
            args = sdata.get("args", ())
            cwd = sdata.get("cwd")

            if url is not None:
                if not isinstance(url, str):
                    raise SourceError(f"'url' in server '{sname}' must be a string")
                if command is not None or args or cwd is not None:
                    raise SourceError(
                        f"http server '{sname}' cannot have 'command', 'args', or 'cwd'"
                    )
            else:
                if command is None or not isinstance(command, str) or not command.strip():
                    raise SourceError(f"stdio server '{sname}' requires a 'command'")

            # Validate against masking leaks
            for val in (command, url, cwd):
                if val and mask_text(str(val)) != str(val):
                    raise SourceError(
                        f"server '{sname}' field looks like a secret; use an environment variable"
                    )
            if mask_args(list(args)) != [str(a) for a in args]:
                raise SourceError(
                    f"server '{sname}' args look like a secret; use an environment variable"
                )

            raw_env = sdata.get("env", {})
            if not isinstance(raw_env, dict):
                raise SourceError(f"'env' in server '{sname}' must be an object")
            env_pairs = []
            for ek, ev in sorted(raw_env.items()):
                if not isinstance(ek, str) or not isinstance(ev, str):
                    raise SourceError(f"'env' keys and values in server '{sname}' must be strings")
                if env_value_looks_secret(ek, ev):
                    raise SourceError(
                        f"server '{sname}' env key '{ek}' looks like a secret; use env_vars"
                    )
                env_pairs.append((ek, ev))

            raw_env_vars = sdata.get("env_vars", ())
            if not isinstance(raw_env_vars, (list, tuple)):
                raise SourceError(f"'env_vars' in server '{sname}' must be a list of strings")
            env_vars_list = []
            for i, evar in enumerate(raw_env_vars, 1):
                if not isinstance(evar, str) or not _ENV_VAR_NAME_RE.match(evar):
                    raise SourceError(
                        f"env_vars entry #{i} in server '{sname}' "
                        "is not a valid environment variable name"
                    )
                env_vars_list.append(evar)

            raw_headers = sdata.get("headers", {})
            if not isinstance(raw_headers, dict):
                raise SourceError(f"'headers' in server '{sname}' must be an object")
            headers_list = []
            for hname, hval in sorted(raw_headers.items()):
                if not isinstance(hname, str) or not isinstance(hval, str):
                    raise SourceError(
                        f"'headers' keys and values in server '{sname}' must be strings"
                    )
                match = _HEADER_VAR_RE.match(hval.strip())
                if not match:
                    raise SourceError(
                        f"headers must reference an environment variable in server '{sname}'"
                    )
                scheme = match.group(1)
                var_name = match.group(2)
                headers_list.append(HeaderRef(name=hname, env_var=var_name, scheme=scheme))

            raw_tools = sdata.get("tools")
            tools_tuple = None
            if raw_tools is not None:
                if not isinstance(raw_tools, (list, tuple)):
                    raise SourceError(f"'tools' in server '{sname}' must be a list of strings")
                for tid in raw_tools:
                    if tid not in valid_ids:
                        raise SourceError(f"unknown tool id '{tid}' in server '{sname}' tools")
                tools_tuple = tuple(raw_tools)

            auth = sdata.get("auth")
            if auth is not None and not isinstance(auth, str):
                raise SourceError(f"'auth' in server '{sname}' must be a string")

            startup_timeout = sdata.get("startup_timeout_sec")
            if startup_timeout is not None and not isinstance(startup_timeout, int):
                raise SourceError(f"'startup_timeout_sec' in server '{sname}' must be an integer")

            enabled = sdata.get("enabled", True)
            if not isinstance(enabled, bool):
                raise SourceError(f"'enabled' in server '{sname}' must be a boolean")

            server = McpServer(
                name=sname,
                command=command,
                args=tuple(args),
                url=url,
                env_vars=tuple(env_vars_list),
                env=tuple(env_pairs),
                headers=tuple(headers_list),
                cwd=cwd,
                auth=auth,
                enabled=enabled,
                startup_timeout_sec=startup_timeout,
            )
            servers_map[sname] = ServerEntry(server=server, tools=tools_tuple)

    warnings: list[str] = []

    instructions_core: str | None = None
    core_file = config_dir / INSTRUCTIONS_NAME
    if core_file.is_file():
        core_text = core_file.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
        if _BIDI_RE.search(core_text):
            warnings.append(
                f"Unicode bidirectional control character found in '{INSTRUCTIONS_NAME}'"
            )
        instructions_core = core_text

    appendices: dict[str, str] = {}
    for item in sorted(config_dir.glob("instructions.*.md")):
        name = item.name
        if name == INSTRUCTIONS_NAME:
            continue
        tool_id = name[len("instructions.") : -len(".md")]
        if tool_id not in valid_ids:
            warnings.append(f"appendix file '{name}' references unknown tool id '{tool_id}'")
            continue
        app_text = item.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
        if _BIDI_RE.search(app_text):
            warnings.append(f"Unicode bidirectional control character found in '{name}'")
        appendices[tool_id] = app_text

    skills: list[SkillRef] = []
    skills_dir = config_dir / SKILLS_DIRNAME
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or "/" in name or "\\" in name:
                warnings.append(f"skill folder '{name}' skipped (invalid name)")
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                warnings.append(f"skill folder '{name}' skipped (missing SKILL.md)")
                continue
            skills.append(SkillRef(name=name, path=entry))

    return SourceConfig(
        path=config_dir,
        tools=tools_map,
        instructions_core=instructions_core,
        appendices=appendices,
        servers=servers_map,
        skills=tuple(skills),
        warnings=tuple(warnings),
    )


def _check_no_double_curly(server_name: str, val: object) -> None:
    if isinstance(val, str):
        if "{{" in val and "}}" in val:
            raise SourceError(
                f"legacy '{{{{VAR}}}}' syntax in server '{server_name}' is not supported; "
                "use '${{VAR}}'"
            )
    elif isinstance(val, dict):
        for sub_val in val.values():
            _check_no_double_curly(server_name, sub_val)
    elif isinstance(val, (list, tuple)):
        for item in val:
            _check_no_double_curly(server_name, item)


def desired_for(source: SourceConfig, adapter_id: str) -> DesiredState:
    """Build the DesiredState for a specific tool adapter from the shared source."""
    if source.instructions_core is None:
        inst = None
    else:
        rendered = source.instructions_core.rstrip("\n") + "\n"
        appendix = source.appendices.get(adapter_id)
        if appendix and appendix.strip():
            rendered += "\n" + appendix.rstrip("\n") + "\n"
        inst = rendered

    mcp: dict[str, McpServer] = {}
    for name, entry in source.servers.items():
        if entry.tools is None or adapter_id in entry.tools:
            mcp[name] = entry.server

    tsettings = source.tools.get(adapter_id, ToolSettings())
    excluded = set(tsettings.skills_exclude)
    skills = [s for s in source.skills if s.name not in excluded]

    return DesiredState(instructions=inst, mcp=mcp, skills=tuple(skills))


def desired_all(source: SourceConfig, adapter_ids: Sequence[str]) -> dict[str, DesiredState]:
    """Build DesiredState for all enabled adapters in adapter_ids."""
    result: dict[str, DesiredState] = {}
    for aid in adapter_ids:
        tsettings = source.tools.get(aid, ToolSettings())
        if tsettings.enabled:
            result[aid] = desired_for(source, aid)
    return result


def render_source_config(tools: Mapping[str, ToolSettings]) -> str:
    """Render config.toml content with LF line endings."""
    lines = [
        "# Shared agent configuration",
        "# Managed by one-setup-all-agents",
        f"version = {SOURCE_VERSION}",
        "",
    ]
    for tid in sorted(tools):
        t = tools[tid]
        lines.append(f"[tools.{tid}]")
        lines.append(f"enabled = {'true' if t.enabled else 'false'}")
        if t.skills_exclude:
            items = ", ".join(f'"{s}"' for s in t.skills_exclude)
            lines.append(f"skills_exclude = [{items}]")
        else:
            lines.append("skills_exclude = []")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_mcp_source(
    entries: Mapping[str, ServerEntry], notes: Mapping[str, Sequence[str]] | None = None
) -> str:
    """Render mcp.jsonc content with 2-space indentation and LF line endings."""
    notes = notes or {}
    servers_dict: dict[str, dict] = {}

    for sname in sorted(entries):
        entry = entries[sname]
        srv = entry.server
        obj: dict[str, object] = {}

        if srv.url is not None:
            obj["url"] = srv.url
        else:
            obj["command"] = srv.command or ""
            if srv.args:
                obj["args"] = list(srv.args)
            if srv.cwd:
                obj["cwd"] = srv.cwd

        if srv.env:
            obj["env"] = dict(srv.env)
        if srv.env_vars:
            obj["env_vars"] = list(srv.env_vars)
        if srv.headers:
            headers_dict = {}
            for h in srv.headers:
                if h.scheme:
                    headers_dict[h.name] = f"{h.scheme} ${{{h.env_var}}}"
                else:
                    headers_dict[h.name] = f"${{{h.env_var}}}"
            obj["headers"] = headers_dict

        if srv.startup_timeout_sec is not None:
            obj["startup_timeout_sec"] = srv.startup_timeout_sec
        if srv.auth is not None:
            obj["auth"] = srv.auth
        if not srv.enabled:
            obj["enabled"] = False
        if entry.tools is not None:
            obj["tools"] = list(entry.tools)

        servers_dict[sname] = obj

    data = {"version": SOURCE_VERSION, "servers": servers_dict}
    formatted = json.dumps(data, indent=2)

    # Prepend comment header
    header = "// Names only: no secrets. Reference environment variables with ${VAR_NAME}.\n"
    if notes:
        # If notes exist, add comments above corresponding servers
        lines = formatted.splitlines()
        annotated: list[str] = [header.rstrip("\n")]
        for line in lines:
            # Check if this line introduces a server: '    "server-name": {'
            for sname, note_list in notes.items():
                if f'"{sname}":' in line and note_list:
                    for note in note_list:
                        annotated.append(f"    // Note: {note}")
            annotated.append(line)
        return "\n".join(annotated) + "\n"

    return header + formatted + "\n"


def render_instructions_core(text: str) -> str:
    """Normalize instructions core text with trailing LF."""
    return text.rstrip("\n") + "\n"
