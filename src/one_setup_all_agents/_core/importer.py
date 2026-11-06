"""Draft the neutral MCP source (mcp.jsonc) from the live configs of the tools.

Never copies env values or header values. For each server it keeps the majority form across the tools,
lists the env variable NAMES it did not import in a comment, and records the decisions given on the command line.
"""

import json
from pathlib import Path

from .mask import MASK
from .parsers import TOOLS
from .report import reference_tools, server_names

HEADER = """// Neutral MCP source for one-setup-all-agents. Edit this file, then run `sync` (it shows a dry run first).
// Names only: no tokens, no env values. A secret belongs in an environment variable, never here.
// Fields: transport, command, args, url, env (non-secret literals), startup_timeout_sec (Codex only),
//   auth (oauth|none, documentation), enabled, tools (default: all four), keep_live_env (tools whose existing env
//   stays as it is in their own file), description.
"""


class ImportError_(Exception):
    pass


def draft(configs, exclude=(), retire=(), keep_live_env=(), auth=None):
    auth = auth or {}
    servers, notes, unmanaged, retired = {}, {}, {}, {}
    for tool, name in (r.split(":", 1) for r in retire):
        retired.setdefault(tool, []).append(name)
    skip = set(exclude) | {n for names in retired.values() for n in names}
    loaded = [t for t in TOOLS if not configs[t].error]
    for name in server_names(configs):
        internal_in = [
            t for t in TOOLS if name in configs[t].servers and configs[t].servers[name].internal
        ]
        for t in internal_in:
            unmanaged.setdefault(t, []).append(name)
        if internal_in or name in skip:
            continue
        refs = reference_tools(name, configs)
        ref = configs[refs[0]].servers[name]
        for field, val in (
            ("command", ref.command),
            ("url", ref.url),
            ("args", json.dumps(ref.args)),
        ):
            if val and MASK in str(val):
                raise ImportError_(
                    f"{name}: {field} contains a masked secret in {refs[0]}; fix it in the tool first"
                )
        spec = {"transport": ref.transport}
        if ref.transport == "stdio":
            spec["command"] = ref.command
            spec["args"] = list(ref.args)
        else:
            spec["url"] = ref.url
        if ref.cwd:
            spec["cwd"] = ref.cwd
        timeouts = [
            configs[t].servers[name].startup_timeout_sec
            for t in TOOLS
            if name in configs[t].servers
            and configs[t].servers[name].startup_timeout_sec is not None
        ]
        if timeouts:
            spec["startup_timeout_sec"] = timeouts[0]
        if name in auth:
            spec["auth"] = auth[name]
        if not ref.enabled:
            spec["enabled"] = False
        present = [t for t in loaded if name in configs[t].servers]
        if len(present) != len(loaded):
            spec["tools"] = present
        keep = [
            t
            for t in keep_live_env
            if name in configs[t].servers and configs[t].servers[name].env_keys
        ]
        if keep:
            spec["keep_live_env"] = keep
        env_names = sorted(
            {
                k
                for t in TOOLS
                if name in configs[t].servers
                for k in configs[t].servers[name].env_keys
            }
        )
        if env_names:
            notes[name] = env_names
        servers[name] = spec
    return servers, retired, unmanaged, notes


def render(servers, retired, unmanaged, notes, vars_=None):
    out = [HEADER, "{", '  "version": 1,']
    if vars_:
        out.append(f'  "vars": {json.dumps(vars_)},')
    out.append('  "servers": {')
    items = list(servers.items())
    for i, (name, spec) in enumerate(items):
        if name in notes:
            out.append(
                f"    // live env variable names NOT imported (values are never copied): {', '.join(notes[name])}"
            )
        body = json.dumps(spec, indent=2, ensure_ascii=False).replace("\n", "\n    ")
        out.append(f"    {json.dumps(name)}: {body}" + ("," if i < len(items) - 1 else ""))
    out.append("  },")
    out.append(f'  "retired": {json.dumps(retired, ensure_ascii=False)},')
    out.append(f'  "unmanaged": {json.dumps(unmanaged, ensure_ascii=False)}')
    out.append("}")
    return "\n".join(out) + "\n"


def write_new(path, text, force=False):
    path = Path(path)
    if path.exists() and not force:
        raise ImportError_(f"{path} already exists (use --force to overwrite it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
