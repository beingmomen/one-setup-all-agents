"""A made-up tool, written the way a contributor would add a real one: ONE module.

`DemoToolAdapter` (id `demo-tool`) is the whole cost of adding a tool (docs/decisions/0008,
"Consequences"): this file, a seed for its fake home, and one registry line. Nothing in `engine/`,
`model.py`, `cli.py` or another adapter is edited. `tests/test_drop_in_tool.py` proves that.

It is NOT in `ADAPTERS`; the test registers it by patching the tuple. Like a real adapter it uses
the shared planners in `adapters/_common.py`. Everything lives under `<home>/.demo-tool/`:

    DEMO.md            the instructions (one owned item; the whole file)
    demo-tool.json     {"mcp_servers": {name: {...}}}; other top-level keys belong to the user
    skills/<name>      one link per skill

It cannot store `startup_timeout_sec`, which `normalize_mcp` says. Fake data only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters._common import (
    decode_text,
    encode_text,
    plan_skill_links,
    plan_whole_file_instructions,
    read_skill_dir,
    sha256_hex,
)
from one_setup_all_agents.adapters.base import ToolAdapter
from one_setup_all_agents.model import (
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
)

ROOT_NAME = ".demo-tool"


class DemoToolAdapter(ToolAdapter):
    id = "demo-tool"
    label = "Demo Tool"
    capabilities = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})

    # -- discovery ----------------------------------------------------------------------

    def detect(self, env: Env) -> Detection:
        root = env.home / ROOT_NAME
        return Detection(installed=root.is_dir(), config_roots=(root,))

    def paths(self, env: Env, scope: Scope) -> Paths:
        root = env.home / ROOT_NAME
        return Paths(
            instructions=root / "DEMO.md", mcp=root / "demo-tool.json", skills_dir=root / "skills"
        )

    # -- reading ------------------------------------------------------------------------

    def read_instructions(self, env: Env, scope: Scope) -> Instructions | None:
        path = self.paths(env, scope).instructions
        if not path.is_file():
            return None
        return Instructions(path=path, text=path.read_bytes().decode("utf-8"))

    def read_mcp(self, env: Env, scope: Scope) -> dict[str, McpServer]:
        raw = _raw_servers(self.paths(env, scope).mcp)
        return {name: _decode(name, entry) for name, entry in raw.items()}

    def read_skills(self, env: Env, scope: Scope) -> list[SkillRef]:
        return read_skill_dir(self.paths(env, scope).skills_dir)

    # -- planning -----------------------------------------------------------------------

    def plan_instructions(self, env: Env, scope: Scope, desired: str, owned: Owned) -> list[Op]:
        path = self.paths(env, scope).instructions
        return plan_whole_file_instructions(self.id, path, desired, owned)

    def plan_mcp(
        self, env: Env, scope: Scope, desired: Mapping[str, McpServer], owned: Owned
    ) -> list[Op]:
        path = self.paths(env, scope).mcp
        old = path.read_bytes() if path.is_file() else None
        current = _raw_servers(path)
        ops: list[Op] = []
        upserts: dict[str, dict] = {}
        for name, server in desired.items():
            entry = _encode(server)
            if current.get(name) == entry:
                continue
            if name in current and name not in owned:
                reason = "an MCP server with this name exists and was not created by all-agents"
                ops.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            upserts[name] = entry
        removals = [n for n in sorted(owned - desired.keys()) if n in current]
        if upserts or removals:
            ops.append(self._edit(path, old, upserts, removals))
        return ops

    def plan_skills(
        self, env: Env, scope: Scope, desired: Sequence[SkillRef], owned: Owned
    ) -> list[Op]:
        return plan_skill_links(self.id, self.paths(env, scope).skills_dir, desired, owned)

    def normalize_mcp(self, servers: Mapping[str, McpServer]) -> dict[str, McpServer]:
        return {n: replace(s, startup_timeout_sec=None) for n, s in servers.items()}

    def _edit(self, path: Path, old: bytes | None, upserts: dict, removals: list) -> EditFile:
        def transform(current: bytes | None) -> bytes:
            text, bom, crlf = decode_text(current) if current else ("{}", False, False)
            doc = json.loads(text)
            servers = doc.setdefault("mcp_servers", {})
            servers.update(upserts)
            for name in removals:
                servers.pop(name, None)
            return encode_text(json.dumps(doc, indent=2) + "\n", bom, crlf)

        return EditFile(
            path=path,
            transform=transform,
            expect_sha256=None if old is None else sha256_hex(old),
            description="update the MCP servers",
            sensitive=True,
            claims=tuple(ItemRef(self.id, Kind.MCP, n) for n in upserts),
            releases=tuple(ItemRef(self.id, Kind.MCP, n) for n in removals),
        )


# -- the file format --------------------------------------------------------------------------


def _raw_servers(path: Path | None) -> dict[str, dict]:
    if path is None or not path.is_file():
        return {}
    text, _bom, _crlf = decode_text(path.read_bytes())
    return dict(json.loads(text).get("mcp_servers", {}))


def _encode(server: McpServer) -> dict:
    entry: dict = {}
    for key, value in (
        ("url", server.url),
        ("command", server.command),
        ("cwd", server.cwd),
        ("auth", server.auth),
    ):
        if value:
            entry[key] = value
    if server.args:
        entry["args"] = list(server.args)
    if server.env_vars:
        entry["env_pass"] = list(server.env_vars)
    if server.env:
        entry["env"] = dict(server.env)
    if server.headers:
        entry["headers"] = [
            {"name": h.name, "env": h.env_var, **({"scheme": h.scheme} if h.scheme else {})}
            for h in server.headers
        ]
    if not server.enabled:
        entry["disabled"] = True
    return entry  # startup_timeout_sec has no place in this format


def _decode(name: str, entry: Mapping) -> McpServer:
    return McpServer(
        name=name,
        command=entry.get("command"),
        args=tuple(entry.get("args", ())),
        url=entry.get("url"),
        env_vars=tuple(entry.get("env_pass", ())),
        env=tuple(entry.get("env", {}).items()),
        headers=tuple(
            HeaderRef(h["name"], h["env"], h.get("scheme")) for h in entry.get("headers", ())
        ),
        cwd=entry.get("cwd"),
        auth=entry.get("auth"),
        enabled=not entry.get("disabled", False),
    )


def seed_demo_tool_home(env: Env) -> None:
    """The user's own config before all-agents: a server, a skill folder and a theme key."""
    root = env.home / ROOT_NAME
    (root / "skills" / "user-skill").mkdir(parents=True)
    (root / "skills" / "user-skill" / "SKILL.md").write_text("user skill\n", encoding="utf-8")
    doc = {"theme": "dark", "mcp_servers": {"user-server": {"command": "user-cmd", "args": ["-x"]}}}
    (root / "demo-tool.json").write_bytes((json.dumps(doc, indent=1) + "\n").encode("utf-8"))
