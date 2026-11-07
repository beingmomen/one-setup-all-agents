"""The neutral MCP source (mcp/servers.jsonc) and its translation into each tool's native entry.

Source shape (names only; no secrets):
    {"version": 1,
     "vars": {"NAME": "text"},                 // optional, {NAME} is expanded in command/args/url
     "servers": {"<name>": {"transport": "stdio"|"http",
                            "command", "args", "cwd", "url",
                            "env": {"K": "non-secret literal"},
                            "env_pass": ["K"],            // variable NAMES the tool passes on from its own environment
                            "startup_timeout_sec": 120,   // used by Codex only
                            "auth": "oauth"|"none",       // documentation; no tool needs a key for it
                            "enabled": true,
                            "tools": ["claude", ...],     // default: all four
                            "keep_live_env": ["codex"],   // keep the env already in that tool's file (values stay there)
                            "description": "..."}},
     "retired":   {"opencode": ["playwright"]},    // entries to remove from a tool
     "unmanaged": {"codex": ["node_repl"]}}        // entries all-agents never touches
"""

import os
import re
import shutil
import sys
from pathlib import Path

from . import jsonc
from .mask import env_value_looks_secret, mask_text
from .parsers import TOOLS

SERVER_FIELDS = {
    "transport",
    "command",
    "args",
    "cwd",
    "url",
    "env",
    "env_pass",
    "headers",
    "startup_timeout_sec",
    "auth",
    "enabled",
    "tools",
    "keep_live_env",
    "description",
}
_HDR_REF = re.compile(r"\{\{\$?([A-Za-z_][A-Za-z0-9_]*)\}\}")
_HDR_VALUE = re.compile(r"(?:([A-Za-z][A-Za-z-]*)\s+)?\{\{\$?([A-Za-z_][A-Za-z0-9_]*)\}\}")
_HDR_BEARER = re.compile(r"(?i)bearer\s+\{\{\$?([A-Za-z_][A-Za-z0-9_]*)\}\}")
_HDR_SECRET_NAME = re.compile(r"(?i)(authorization|token|secret|key|cookie|auth|passw)")
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CLI_NAME = "all-agents"  # the console script (ADR 0001)
RUNNER_SUBCOMMAND = "_run"  # hidden subcommand: starts a command after expanding {{VAR}} placeholders (see runner.py)
_PLACEHOLDER = re.compile(
    r"\{\{[^{}]*\}\}"
)  # {{VAR}} / {{$VAR}}: only the runner expands these, and only in its child's arguments
MCP_REMOTE = "mcp-remote@0.15.2"  # pinned: the one bridge from stdio to a remote http server (GitHub preset and header wrappers)
_HDR_LITERAL_BAD = re.compile(r"[{}$]")
_HDR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class SourceError(Exception):
    pass


class Spec:
    """One neutral server entry, validated and with {vars} expanded."""

    def __init__(self, name, raw, variables):
        self.name = name
        unknown = set(raw) - SERVER_FIELDS
        if unknown:
            raise SourceError(f"{name}: unknown field(s) {sorted(unknown)} (not supported yet)")
        self.transport = raw.get("transport", "http" if raw.get("url") else "stdio")
        if self.transport not in ("stdio", "http"):
            raise SourceError(f"{name}: transport must be stdio or http")
        expand = lambda v: _expand(v, variables)  # noqa: E731
        self.command = expand(raw.get("command"))
        self.args = [expand(a) for a in raw.get("args", [])]
        self.url = expand(raw.get("url"))
        self.cwd = expand(raw.get("cwd"))
        self.env = dict(raw.get("env") or {})
        self.env_pass = list(raw.get("env_pass") or [])
        self.headers = dict(raw.get("headers") or {})
        self.startup_timeout_sec = raw.get("startup_timeout_sec")
        self.auth = raw.get("auth")
        self.enabled = raw.get("enabled", True)
        self.tools = list(raw.get("tools") or TOOLS)
        self.keep_live_env = list(raw.get("keep_live_env") or [])
        self.description = raw.get("description")
        self._validate()

    def _validate(self):
        n = self.name
        if self.transport == "stdio" and not self.command:
            raise SourceError(f"{n}: stdio server needs a command")
        if self.transport == "http" and not self.url:
            raise SourceError(f"{n}: http server needs a url")
        if self.transport == "http" and (self.command or self.args):
            raise SourceError(f"{n}: http server must not have command/args")
        if self.auth not in (None, "oauth", "none"):
            raise SourceError(
                f"{n}: auth must be oauth or none (bearer/header secrets are not supported yet)"
            )
        for t in self.tools + self.keep_live_env:
            if t not in TOOLS:
                raise SourceError(f"{n}: unknown tool '{t}'")
        for field, value in (
            ("command", self.command),
            ("url", self.url),
            *[("args", a) for a in self.args],
        ):
            if value is not None and mask_text(value) != value:
                raise SourceError(
                    f"{n}: {field} contains something that looks like a secret; use an env variable name instead"
                )
        if self.headers and self.transport != "http":
            raise SourceError(f"{n}: headers belong to http servers only")
        for k, v in self.headers.items():
            v = "" if v is None else str(v).strip()
            if _HDR_REF.search(v):
                if not _HDR_VALUE.fullmatch(v):
                    raise SourceError(
                        f"{n}: header {k}: use one {{{{VAR}}}} reference, alone or after a scheme word (Bearer, Basic...)"
                    )
            elif v and (_HDR_SECRET_NAME.search(k) or mask_text(v) != v):
                raise SourceError(
                    f"{n}: header {k} looks like a secret; write it as a {{{{VAR}}}} reference"
                )
        for k in self.env_pass:
            if not isinstance(k, str) or not _VAR_NAME.match(k):
                raise SourceError(
                    f"{n}: env_pass holds variable NAMES only (got something that is not a name)"
                )
        for k, v in self.env.items():
            if env_value_looks_secret(k, v):
                raise SourceError(f"{n}: env {k} looks like a secret; the source holds names only")


def _expand(value, variables):
    if not isinstance(value, str):
        return value
    for k, v in variables.items():
        value = value.replace("{" + k + "}", v)
    return value


class Source:
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = jsonc.loads(self.path.read_bytes().decode("utf-8-sig"))
        except FileNotFoundError:
            raise SourceError(f"source file not found: {self.path}")
        except ValueError as e:
            raise SourceError(f"cannot parse {self.path.name}: {e}")
        if not isinstance(data, dict) or data.get("version") != 1:
            raise SourceError('source must be an object with "version": 1')
        extra = set(data) - {"version", "vars", "servers", "retired", "unmanaged"}
        if extra:
            raise SourceError(f"unknown top-level key(s): {sorted(extra)}")
        variables = {str(k): str(v) for k, v in (data.get("vars") or {}).items()}
        self.servers = {}
        for name, raw in (data.get("servers") or {}).items():
            if not isinstance(raw, dict):
                raise SourceError(f"{name}: entry must be an object")
            self.servers[name] = Spec(name, raw, variables)
        self.retired = {t: list(v) for t, v in (data.get("retired") or {}).items()}
        self.unmanaged = {t: list(v) for t, v in (data.get("unmanaged") or {}).items()}
        for label, table in (("retired", self.retired), ("unmanaged", self.unmanaged)):
            for t, names in table.items():
                if t not in TOOLS:
                    raise SourceError(f"{label}: unknown tool '{t}'")
                for n in names:
                    if n in self.servers and (label == "unmanaged" or t in self.servers[n].tools):
                        raise SourceError(f"{label}.{t}: '{n}' is also a managed server")
        for t, names in self.unmanaged.items():
            for n in names:
                if n in self.retired.get(t, []):
                    raise SourceError(f"{n} is both retired and unmanaged in {t}")

    def unsupported_for(self, tool):
        """{server: reason} for the servers of this source that `tool` cannot carry (reported and skipped, never faked)."""
        out = {}
        for n, s in self.servers.items():
            why = unsupported(tool, s)
            if why and tool in s.tools:
                out[n] = why
        return out

    def managed_names(self, tool):
        bad = self.unsupported_for(tool)
        return [n for n, s in self.servers.items() if tool in s.tools and n not in bad]


# ---------------------------------------------------------------- native rendering
CONTAINER = {
    "claude": ("mcpServers",),
    "codex": ("mcp_servers",),
    "agy": ("mcpServers",),
    "opencode": ("mcp", "servers"),
}


def wrap(tool, name, entry):
    """A minimal config tree holding one entry, in the shape the tool's parser expects."""
    node = {name: entry}
    for key in reversed(CONTAINER[tool]):
        node = {key: node}
    return node


def header_refs(spec):
    """True when any header value carries a {{VAR}} reference."""
    return any(_HDR_REF.search(str(v)) for v in spec.headers.values())


def launcher_invocation():
    """(command, leading args) that start the runner (`all-agents _run`), the same on every OS.

    The bare console-script name is used when it is on PATH: it stays valid when the package is upgraded or the
    virtual environment moves (an absolute path would break). Without it on PATH, the current interpreter runs the
    module instead. The word `python` is never written into a config file."""
    if shutil.which(CLI_NAME):
        return CLI_NAME, [RUNNER_SUBCOMMAND]
    return sys.executable, ["-I", "-m", "one_setup_all_agents", RUNNER_SUBCOMMAND]


def wrapper_args(spec):
    """Arguments of the runner command for an http server with headers, run as a stdio runner + mcp-remote bridge.

    agy does not expand variables in mcp_config.json, so a header with a secret reference cannot be written there. Instead the
    entry runs `all-agents _run --derive NAME=<value> -- npx mcp-remote <url> --header "Key:{{$NAME}}"`: the runner computes each
    header value from the user variable into the CHILD'S ENVIRONMENT, and `{{$NAME}}` reaches mcp-remote as the literal ${NAME}, which
    mcp-remote expands itself. The config holds variable NAMES only; the value is in neither a file nor an argv. Every header goes
    through a derived variable, so a value with a space (`Bearer <token>`) never travels as an argument (Windows .cmd shims split it)."""
    derives, headers = [], []
    for k, v in spec.headers.items():
        v = "" if v is None else str(v).strip()
        if not v:
            headers += ["--header", f"{k}:"]
            continue
        name = f"MCP_HDR_{len(derives) // 2 + 1}"  # numbered, not named after the header: mask.py hides `...AUTHORIZATION=<value>` in args
        m = _HDR_VALUE.fullmatch(v)
        tmpl = (
            (f"{m.group(1)}{{sp}}" if m.group(1) else "") + "{{" + m.group(2) + "}}"
            if m
            else v.replace(" ", "{sp}")
        )
        derives += ["--derive", f"{name}={tmpl}"]
        headers += ["--header", f"{k}:{{{{${name}}}}}"]
    allow = ["--allow-http"] if str(spec.url).lower().startswith("http://") else []
    _, lead = launcher_invocation()
    return [*lead, *derives, "--", "npx", "-y", MCP_REMOTE, spec.url, *allow, *headers]


def _wrapper_blocker(spec):
    """Why the agy wrapper cannot carry these headers (None = it can)."""
    for k, v in spec.headers.items():
        v = "" if v is None else str(v).strip()
        if not _HDR_REF.search(v) and _HDR_LITERAL_BAD.search(v):
            return f"header {k} holds braces or $ that the agy wrapper cannot pass on"
        if ":" in str(k) or not _HDR_NAME.match(str(k)):
            return f"header name {k!r} cannot be passed to mcp-remote"
    return None


def header_vars(spec):
    """Names of the variables the headers of `spec` reference (sorted, names only)."""
    return sorted({m for v in spec.headers.values() for m in _HDR_REF.findall(str(v))})


def unsupported(tool, spec):
    """Why `spec` cannot be written for `tool` (None when it can). Reported, never faked ."""
    if spec.headers and header_refs(spec):
        if tool == "agy":
            why = _wrapper_blocker(
                spec
            )  # agy gets the runner + mcp-remote wrapper instead (render_native)
            if why:
                return why
        if tool == "codex":
            for k, v in spec.headers.items():
                v = str(v).strip()
                if (
                    _HDR_REF.search(v)
                    and not _HDR_REF.fullmatch(v)
                    and not (_HDR_BEARER.fullmatch(v) and k.lower() == "authorization")
                ):
                    return f"codex reads a header from a variable only as the whole value or as `Authorization: Bearer <variable>` (header {k})"
    if placeholder_leak(
        render_native(tool, spec)
    ):  # guard: never write a {{...}} that the tool cannot expand
        return f"the generated {tool} entry would hold an unexpanded {{{{...}}}} placeholder that {tool} cannot expand"
    return None


# keys the generator writes per tool; a {{...}} there is only legitimate inside the ARGUMENTS of a runner command
_ARG_KEYS = {"claude": ("args",), "codex": ("args",), "agy": ("args",), "opencode": ("command",)}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def placeholder_leak(entry, tool=None):
    """The first unexpanded `{{...}}` in a native entry that its tool cannot expand (None = clean).

    The runner expands {{VAR}} / {{$VAR}} in the arguments of the command it starts, so those are fine in `args` (opencode: `command`)
    of an entry that runs the runner. Anywhere else (a plain server's args, url, env, headers) no tool expands it and the literal text
    would reach the server. `tool` limits the check to the keys the generator owns (an entry's other keys are the user's)."""
    if not isinstance(entry, dict):
        return None
    owned = None
    if tool:
        owned = OWNED_KEYS[tool] | HEADER_KEYS[tool] | {"command", "args"}
    runs_launcher = RUNNER_SUBCOMMAND in set(_strings([entry.get("command"), entry.get("args")]))
    for k, v in entry.items():
        if owned is not None and k not in owned:
            continue
        if runs_launcher and k in ("args", "command"):
            continue
        for s in _strings(v):
            m = _PLACEHOLDER.search(s)
            if m:
                return m.group(0)
    return None


def _headers_for(tool, spec):
    """The native header keys of an http server, with {{VAR}} turned into each tool's own reference ({} when none)."""
    if not spec.headers:
        return {}
    if tool == "claude":
        return {
            k: _HDR_REF.sub(lambda m: "${" + m.group(1) + "}", str(v))
            for k, v in spec.headers.items()
        }
    if tool == "opencode":
        return {
            k: _HDR_REF.sub(lambda m: "{env:" + m.group(1) + "}", str(v))
            for k, v in spec.headers.items()
        }
    if tool == "codex":
        out = {}
        for k, v in spec.headers.items():
            v = str(v).strip()
            whole, bearer = _HDR_REF.fullmatch(v), _HDR_BEARER.fullmatch(v)
            if whole:
                out.setdefault("env_http_headers", {})[k] = whole.group(1)
            elif bearer and k.lower() == "authorization":
                out["bearer_token_env_var"] = bearer.group(1)
            else:
                out.setdefault("http_headers", {})[k] = v
        return out
    return {
        "headers": dict(spec.headers)
    }  # agy: literal, non-secret headers only (references are unsupported above)


WIN_SHIMS = {
    "npx",
    "npm",
    "pnpm",
    "yarn",
}  # Windows resolves these only as .cmd outside Claude Code


def is_windows():
    return os.name == "nt"


def _command_for(tool, spec):
    cmd = spec.command
    # Only on Windows: on Linux and macOS `npx.cmd` does not exist. Project sources only (the user-level source writes .cmd itself).
    if is_windows() and getattr(spec, "project", False) and tool != "claude" and cmd in WIN_SHIMS:
        return cmd + ".cmd"
    return cmd


def render_native(tool, spec, live_env=None):
    """The native entry for `spec` in `tool`'s format. `live_env` (name -> value) is carried over when spec.keep_live_env says so."""
    env = {}
    if live_env and tool in spec.keep_live_env:
        env.update(live_env)
    env.update(spec.env)
    # env_pass: the tool hands its own variable to the server (agy inherits the whole environment, so it needs nothing)
    if tool == "claude":
        env.update({k: "${" + k + "}" for k in spec.env_pass})
    elif tool == "opencode":
        env.update({k: "{env:" + k + "}" for k in spec.env_pass})
    e = {}
    if tool == "claude":
        if spec.transport == "http":
            e = {"type": "http", "url": spec.url}
            e.update({"headers": _headers_for(tool, spec)} if spec.headers else {})
        else:
            e = {"type": "stdio", "command": spec.command, "args": list(spec.args)}
        if env:
            e["env"] = env
        if spec.cwd:
            e["cwd"] = spec.cwd
        if not spec.enabled:
            e["disabled"] = True
    elif tool == "codex":
        if spec.transport == "http":
            e = {"url": spec.url}
            e.update(_headers_for(tool, spec))
        else:
            e = {"command": _command_for(tool, spec), "args": list(spec.args)}
        if spec.startup_timeout_sec is not None:
            e["startup_timeout_sec"] = spec.startup_timeout_sec
        if env:
            e["env"] = env
        if spec.env_pass:
            e["env_vars"] = list(spec.env_pass)
        if spec.cwd:
            e["cwd"] = spec.cwd
        if not spec.enabled:
            e["enabled"] = False
    elif tool == "opencode":
        if spec.transport == "http":
            e = {"type": "remote", "url": spec.url}
            e.update({"headers": _headers_for(tool, spec)} if spec.headers else {})
        else:
            e = {"type": "local", "command": [_command_for(tool, spec), *spec.args]}
        if env:
            e["environment"] = env
        if spec.cwd:
            e["cwd"] = spec.cwd
        if not spec.enabled:
            e["disabled"] = True
    else:  # agy
        if spec.transport == "http" and header_refs(spec):
            command, _ = launcher_invocation()
            e = {
                "command": command,
                "args": wrapper_args(spec),
            }  # a stdio bridge: agy cannot expand a header variable itself
        elif spec.transport == "http":
            e = {"serverUrl": spec.url}
            e.update(_headers_for(tool, spec))
        else:
            e = {"command": _command_for(tool, spec), "args": list(spec.args)}
        if env:
            e["env"] = env
        if spec.cwd:
            e["cwd"] = spec.cwd
        if not spec.enabled:
            e["disabled"] = True
    return e


# keys the generator owns per tool; when it rewrites an entry these are replaced or dropped, everything else is kept
OWNED_KEYS = {
    "claude": {"type", "command", "args", "url", "env", "cwd", "disabled"},
    "codex": {"command", "args", "url", "env", "cwd", "enabled", "startup_timeout_sec"},
    "opencode": {"type", "command", "url", "environment", "env", "cwd", "disabled"},
    "agy": {"command", "args", "serverUrl", "url", "httpUrl", "env", "cwd", "disabled"},
}
KEEP_IF_ABSENT = {
    "startup_timeout_sec",
    "type",
}  # not dropped when the fresh entry does not mention them
HEADER_KEYS = {
    "claude": {"headers"},
    "opencode": {"headers"},
    "agy": {"headers"},
    "codex": {"http_headers", "env_http_headers", "bearer_token_env_var"},
}


def patch_native(tool, old, fresh, own_headers=False):
    """Update an existing native entry so it matches `fresh`, keeping keys all-agents does not own.

    Header keys are owned only for project sources (own_headers); user-level entries keep whatever headers they have."""
    new = dict(old)
    for k in HEADER_KEYS[tool]:
        if k in fresh:
            new[k] = fresh[k]
        elif own_headers and k in new:
            del new[k]
    for k in OWNED_KEYS[tool]:
        if k in fresh:
            new[k] = fresh[k]
        elif k in new and k not in KEEP_IF_ABSENT:
            del new[k]
    if tool == "opencode" and "environment" in fresh:
        new.pop("env", None)
    if tool in ("claude", "agy", "opencode") and "disabled" in new and "disabled" not in fresh:
        del new["disabled"]
    if tool == "agy" and "serverUrl" in fresh:
        new.pop("url", None)
        new.pop("httpUrl", None)
    if tool == "codex" and "enabled" in new and "enabled" not in fresh:
        new["enabled"] = True
    if (
        tool == "codex" and "env_vars" in fresh
    ):  # owned only when the source passes variables on; global entries keep theirs
        new["env_vars"] = fresh["env_vars"]
    return new
