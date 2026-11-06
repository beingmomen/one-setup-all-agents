"""Read-only parsers for the four MCP config formats. Each returns {name: Server}.

claude    ~/.claude.json                       top-level "mcpServers" (project entries are ignored)
codex     ~/.codex/config.toml                 [mcp_servers.<name>]
opencode  ~/.config/opencode/opencode.jsonc    "mcp"."servers" (v2), JSONC
agy       ~/.gemini/config/mcp_config.json     "mcpServers"
"""

import json
import time
import tomllib
from pathlib import Path

from . import jsonc
from .model import Server, names_of, set_env, set_headers, set_http, set_stdio

# Entries the tool itself writes and rewrites; all-agents must never manage them (plan section 2.4 "unmanaged").
INTERNAL = {"codex": {"node_repl"}}

TOOLS = ("claude", "codex", "opencode", "agy")
LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "agy": "Antigravity (agy)",
}
PATHS = {
    "claude": (".claude.json",),
    "codex": (".codex", "config.toml"),
    "opencode": (".config", "opencode", "opencode.jsonc"),
    "agy": (".gemini", "config", "mcp_config.json"),
}
OPENCODE_FALLBACK = (".config", "opencode", "opencode.json")


class ToolConfig:
    def __init__(self, tool, path):
        self.tool = tool
        self.path = path
        self.exists = False
        self.error = None
        self.servers = {}
        self.raw = {}  # name -> native entry (may hold env values: kept in memory for editing, never printed)

    @property
    def count(self):
        return len(self.servers)


def _read_json(path, loader):
    return loader(path.read_bytes().decode("utf-8-sig"))


def _enabled(entry):
    if entry.get("disabled") is True or entry.get("enabled") is False:
        return False
    return True


def _as_list(v):
    return list(v) if isinstance(v, (list, tuple)) else []


# ---------------------------------------------------------------- per-format normalizers
def parse_claude(data):
    out = {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    for name, e in (servers or {}).items():
        if not isinstance(e, dict):
            continue
        s = Server(name, "claude")
        url = e.get("url")
        kind = e.get("type")
        if url or kind in ("http", "sse"):
            set_http(s, url, "sse" if kind == "sse" else "http")
        else:
            set_stdio(s, e.get("command"), _as_list(e.get("args")))
        set_env(s, e.get("env"))
        set_headers(s, e.get("headers"))
        s.enabled = _enabled(e)
        s.cwd = e.get("cwd")
        out[name] = s
    return out


def parse_codex(data):
    out = {}
    servers = data.get("mcp_servers") if isinstance(data, dict) else None
    for name, e in (servers or {}).items():
        if not isinstance(e, dict):
            continue
        s = Server(name, "codex")
        if e.get("url"):
            set_http(s, e.get("url"))
        else:
            set_stdio(s, e.get("command"), _as_list(e.get("args")))
        set_env(s, e.get("env"))
        s.env_pass = names_of(e.get("env_vars"))
        set_headers(
            s, e.get("http_headers"), e.get("env_http_headers"), e.get("bearer_token_env_var")
        )
        s.enabled = _enabled(e)
        s.cwd = e.get("cwd")
        s.startup_timeout_sec = e.get("startup_timeout_sec")
        s.internal = name in INTERNAL["codex"]
        out[name] = s
    return out


def parse_opencode(data):
    out = {}
    mcp = data.get("mcp") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        return out
    # v2: mcp.servers.<name>; v1 fallback: mcp.<name> directly
    servers = mcp["servers"] if isinstance(mcp.get("servers"), dict) else mcp
    for name, e in servers.items():
        if not isinstance(e, dict):
            continue
        s = Server(name, "opencode")
        kind = e.get("type")
        if kind == "remote" or e.get("url"):
            set_http(s, e.get("url"))
            oauth = e.get("oauth")
            set_headers(s, e.get("headers"))
            if s.auth is None and isinstance(oauth, dict):
                s.auth = "oauth"
            elif s.auth is None and oauth is False:
                s.auth = "none"
        else:
            cmd = _as_list(e.get("command"))
            set_stdio(s, cmd[0] if cmd else None, cmd[1:])
        set_env(s, e.get("environment") or e.get("env"))
        s.enabled = _enabled(e)
        s.cwd = e.get("cwd")
        out[name] = s
    return out


def parse_agy(data):
    out = {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    for name, e in (servers or {}).items():
        if not isinstance(e, dict):
            continue
        s = Server(name, "agy")
        url = e.get("serverUrl") or e.get("url") or e.get("httpUrl")
        if url:
            set_http(s, url)
        else:
            set_stdio(s, e.get("command"), _as_list(e.get("args")))
        set_env(s, e.get("env"))
        set_headers(s, e.get("headers"))
        s.enabled = _enabled(e)
        s.cwd = e.get("cwd")
        out[name] = s
    return out


def raw_servers(tool, data):
    """The dict of native entries (name -> dict) inside a parsed config tree."""
    if tool == "claude":
        node = data.get("mcpServers") if isinstance(data, dict) else None
    elif tool == "codex":
        node = data.get("mcp_servers") if isinstance(data, dict) else None
    elif tool == "agy":
        node = data.get("mcpServers") if isinstance(data, dict) else None
    else:
        mcp = data.get("mcp") if isinstance(data, dict) else None
        node = (
            mcp["servers"]
            if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict)
            else mcp
        )
    return (
        {n: e for n, e in (node or {}).items() if isinstance(e, dict)}
        if isinstance(node, dict)
        else {}
    )


NORMALIZERS = {
    "claude": parse_claude,
    "codex": parse_codex,
    "opencode": parse_opencode,
    "agy": parse_agy,
}


def finish_servers(servers):
    """Stdio servers with no auth info have no transport-level auth."""
    for s in servers.values():
        if s.transport == "stdio" and s.auth is None:
            s.auth = "none"
    return servers


def parse_text(tool, text):
    """(servers, raw) from the text of a tool's config. Raises ValueError / TOMLDecodeError on bad input."""
    if tool == "codex":
        data = tomllib.loads(text)
    elif tool == "opencode":
        data = jsonc.loads(text)
    else:
        data = json.loads(text)
    servers = finish_servers(NORMALIZERS[tool](data))
    return servers, raw_servers(tool, data)


def config_path(tool, home):
    path = Path(home).joinpath(*PATHS[tool])
    if tool == "opencode" and not path.exists():
        alt = Path(home).joinpath(*OPENCODE_FALLBACK)
        if alt.exists():
            path = alt
    return path


def load_tool(tool, home):
    path = config_path(tool, home)
    cfg = ToolConfig(tool, path)
    if not path.exists():
        cfg.error = "file not found"
        return cfg
    cfg.exists = True
    try:
        if tool in ("claude", "agy", "opencode"):
            text = _read_json(
                path, lambda t: t
            )  # plain read; parsed below with retry for torn reads of live files
        else:
            text = path.read_bytes().decode("utf-8-sig")
        try:
            cfg.servers, cfg.raw = parse_text(tool, text)
        except ValueError:
            time.sleep(
                0.2
            )  # the live Claude file is rewritten by running sessions; retry a torn read once
            cfg.servers, cfg.raw = parse_text(tool, path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        # json/toml errors report line and column only, never the content
        cfg.error = f"cannot parse ({type(e).__name__}: {e})"
        cfg.servers = {}
        cfg.raw = {}
    return cfg


def load_all(home):
    return {t: load_tool(t, home) for t in TOOLS}
