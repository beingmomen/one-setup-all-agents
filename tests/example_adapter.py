"""A tiny, neutral adapter for a made-up tool, used to prove the contract suite is satisfiable.

`ExampleAdapter` (id `example`) is NOT registered in `ADAPTERS`. It keeps everything under
`<home>/.example/`:

    INSTRUCTIONS.md      the instructions (one owned item; a size limit like Antigravity's)
    mcp.json             {"servers": {name: {...}}}; other top-level keys belong to the user
    skills/<name>        one link per skill

It deliberately cannot store `startup_timeout_sec`, which shows `normalize_mcp` at work.
`ExampleCliAdapter` keeps the same format but changes MCP only through a CLI (`RunCli` ops
with inverses), run in the tests by the in-process `FakeExampleCli`. Fake data only.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from contract_applier import is_link, link_target
from one_setup_all_agents.adapters.base import ToolAdapter
from one_setup_all_agents.model import (
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
    RunCli,
    Scope,
    SkillRef,
)

ROOT_NAME = ".example"
CLI_NAME = "example-cli"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(doc: object) -> str:
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


class ExampleAdapter(ToolAdapter):
    id = "example"
    label = "Example"
    capabilities = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})

    INSTRUCTIONS_LIMIT = 4000  # characters, like a tool with a size limit (ADR 0003)

    # -- discovery ----------------------------------------------------------------------

    def detect(self, env: Env) -> Detection:
        root = env.home / ROOT_NAME
        return Detection(installed=root.is_dir(), config_roots=(root,))

    def paths(self, env: Env, scope: Scope) -> Paths:
        root = env.home / ROOT_NAME
        return Paths(
            instructions=root / "INSTRUCTIONS.md",
            mcp=root / "mcp.json",
            skills_dir=root / "skills",
        )

    # -- reading ------------------------------------------------------------------------

    def read_instructions(self, env: Env, scope: Scope) -> Instructions | None:
        path = self.paths(env, scope).instructions
        if not path.is_file():
            return None
        return Instructions(path=path, text=path.read_bytes().decode("utf-8"))

    def read_mcp(self, env: Env, scope: Scope) -> dict[str, McpServer]:
        raw = self._raw_servers(self.paths(env, scope).mcp)
        return {name: self._decode(name, entry) for name, entry in raw.items()}

    def read_skills(self, env: Env, scope: Scope) -> list[SkillRef]:
        skills_dir = self.paths(env, scope).skills_dir
        if not skills_dir.is_dir():
            return []
        return [
            SkillRef(name=child.name, path=child, is_link=is_link(child))
            for child in sorted(skills_dir.iterdir())
            if child.is_dir()
        ]

    # -- planning -----------------------------------------------------------------------

    def plan_instructions(self, env: Env, scope: Scope, desired: str, owned: Owned) -> list[Op]:
        path = self.paths(env, scope).instructions
        ref = ItemRef(self.id, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY)
        if len(desired) > self.INSTRUCTIONS_LIMIT:
            reason = f"{len(desired)} characters; this tool accepts {self.INSTRUCTIONS_LIMIT}"
            return [Conflict(self.id, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY, reason, path)]
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
                claims=(ref,),
            )
        ]

    def plan_mcp(
        self, env: Env, scope: Scope, desired: Mapping[str, McpServer], owned: Owned
    ) -> list[Op]:
        path = self.paths(env, scope).mcp
        old = path.read_bytes() if path.is_file() else None
        current = self._raw_servers(path)
        ops: list[Op] = []
        upserts: dict[str, dict] = {}
        for name, server in desired.items():
            entry = self._encode(server)
            if current.get(name) == entry:
                continue  # already as wanted (an identical entry of the user's is also left alone)
            if name in current and name not in owned:
                reason = "an MCP server with this name exists and was not created by all-agents"
                ops.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            upserts[name] = entry
        removals = [n for n in sorted(owned - desired.keys()) if n in current]
        if upserts or removals:
            ops.append(self._mcp_edit(path, old, upserts, removals))
        return ops

    def plan_skills(
        self, env: Env, scope: Scope, desired: Sequence[SkillRef], owned: Owned
    ) -> list[Op]:
        skills_dir = self.paths(env, scope).skills_dir
        ops: list[Op] = []
        wanted = {skill.name: skill for skill in desired}
        for name, skill in wanted.items():
            dest = skills_dir / name
            ref = ItemRef(self.id, Kind.SKILLS, name)
            if not os.path.lexists(dest):
                ops.append(Link(dest, skill.path, f"link skill {name}", claims=(ref,)))
            elif is_link(dest) and link_target(dest) == Path(os.path.realpath(skill.path)):
                continue
            elif is_link(dest) and name in owned:
                ops.append(Link(dest, skill.path, f"repoint skill {name}", claims=(ref,)))
            else:
                reason = "a skill with this name exists and was not created by all-agents"
                ops.append(Conflict(self.id, Kind.SKILLS, name, reason, dest))
        for name in sorted(owned - wanted.keys()):
            dest = skills_dir / name
            ref = ItemRef(self.id, Kind.SKILLS, name)
            if not os.path.lexists(dest):
                continue
            if is_link(dest):
                ops.append(Link(dest, None, f"withdraw skill {name}", releases=(ref,)))
            else:
                reason = "the owned skill is no longer a link; left alone"
                ops.append(Conflict(self.id, Kind.SKILLS, name, reason, dest))
        return ops

    # -- normalization: this tool has no startup timeout --------------------------------

    def normalize_mcp(self, servers: Mapping[str, McpServer]) -> dict[str, McpServer]:
        return {n: replace(s, startup_timeout_sec=None) for n, s in servers.items()}

    # -- the file format ----------------------------------------------------------------

    @staticmethod
    def _raw_servers(path: Path) -> dict[str, dict]:
        if not path.is_file():
            return {}
        return dict(json.loads(path.read_bytes().decode("utf-8")).get("servers", {}))

    def _mcp_edit(self, path: Path, old: bytes | None, upserts: dict, removals: list) -> EditFile:
        def transform(current: bytes | None) -> bytes:
            doc = json.loads(current.decode("utf-8")) if current else {}
            servers = doc.setdefault("servers", {})
            servers.update(upserts)
            for name in removals:
                servers.pop(name, None)
            return (json.dumps(doc, indent=2) + "\n").encode("utf-8")

        return EditFile(
            path=path,
            transform=transform,
            expect_sha256=None if old is None else _sha(old),
            description="update the MCP servers",
            sensitive=True,
            claims=tuple(ItemRef(self.id, Kind.MCP, n) for n in upserts),
            releases=tuple(ItemRef(self.id, Kind.MCP, n) for n in removals),
        )

    @staticmethod
    def _encode(server: McpServer) -> dict:
        entry: dict = {}
        if server.url:
            entry["url"] = server.url
        if server.command:
            entry["command"] = server.command
        if server.args:
            entry["args"] = list(server.args)
        if server.cwd:
            entry["cwd"] = server.cwd
        if server.env_vars:
            entry["env"] = list(server.env_vars)
        if server.headers:
            entry["headers"] = [
                {"name": h.name, "env": h.env_var, **({"scheme": h.scheme} if h.scheme else {})}
                for h in server.headers
            ]
        if server.auth:
            entry["auth"] = server.auth
        if not server.enabled:
            entry["enabled"] = False
        return entry  # startup_timeout_sec has no place in this format

    @staticmethod
    def _decode(name: str, entry: Mapping) -> McpServer:
        return McpServer(
            name=name,
            command=entry.get("command"),
            args=tuple(entry.get("args", ())),
            url=entry.get("url"),
            env_vars=tuple(entry.get("env", ())),
            headers=tuple(
                HeaderRef(h["name"], h["env"], h.get("scheme")) for h in entry.get("headers", ())
            ),
            cwd=entry.get("cwd"),
            auth=entry.get("auth"),
            enabled=entry.get("enabled", True),
        )


class ExampleCliAdapter(ExampleAdapter):
    """Same tool, but its MCP file belongs to a CLI: changes go through `RunCli` with inverses."""

    id = "example-cli"
    label = "Example (CLI)"

    def paths(self, env: Env, scope: Scope) -> Paths:
        base = super().paths(env, scope)
        return replace(base, mcp=env.home / ROOT_NAME / "cli-mcp.json")

    def plan_mcp(
        self, env: Env, scope: Scope, desired: Mapping[str, McpServer], owned: Owned
    ) -> list[Op]:
        path = self.paths(env, scope).mcp
        current = self._raw_servers(path)
        ops: list[Op] = []
        for name, server in desired.items():
            entry = self._encode(server)
            existing = current.get(name)
            if existing == entry:
                continue
            if existing is not None and name not in owned:
                reason = "an MCP server with this name exists and was not created by all-agents"
                ops.append(Conflict(self.id, Kind.MCP, name, reason, path))
                continue
            ref = ItemRef(self.id, Kind.MCP, name)
            if existing is not None:  # not atomic: remove, then add (ADR 0005 open point)
                ops.append(self._remove(name, existing, f"replace MCP server {name} (remove)"))
            add = [CLI_NAME, "mcp", "add", name, json.dumps(entry, sort_keys=True)]
            ops.append(
                RunCli(
                    argv=add,
                    inverse_argv=[CLI_NAME, "mcp", "remove", name],
                    description=f"add MCP server {name}",
                    claims=(ref,),
                )
            )
        for name in sorted(owned - desired.keys()):
            if name in current:
                ops.append(
                    self._remove(
                        name,
                        current[name],
                        f"remove MCP server {name}",
                        releases=(ItemRef(self.id, Kind.MCP, name),),
                    )
                )
        return ops

    @staticmethod
    def _remove(name: str, existing: dict, description: str, releases=()) -> RunCli:
        return RunCli(
            argv=[CLI_NAME, "mcp", "remove", name],
            inverse_argv=[CLI_NAME, "mcp", "add", name, json.dumps(existing, sort_keys=True)],
            description=description,
            releases=releases,
        )


class FakeExampleCli:
    """In-process stand-in for `example-cli mcp add|remove`. It never starts a process.

    Keeps `<home>/.example/cli-mcp.json` in canonical form (sorted keys) and deletes the file when
    the last server is removed, so that an add followed by its inverse leaves the home as it was.
    """

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / ROOT_NAME / "cli-mcp.json"
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] != [CLI_NAME, "mcp"] or len(argv) < 4:
            return 2
        verb, name = argv[2], argv[3]
        doc = json.loads(self.path.read_text("utf-8")) if self.path.is_file() else {}
        servers = doc.setdefault("servers", {})
        if verb == "add" and len(argv) == 5:
            if name in servers:
                return 1
            servers[name] = json.loads(argv[4])
        elif verb == "remove" and len(argv) == 4:
            if name not in servers:
                return 1
            del servers[name]
        else:
            return 2
        if servers or len(doc) > 1:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(canonical_json(doc).encode("utf-8"))
        elif self.path.is_file():
            self.path.unlink()
        return 0


def seed_example_home(env: Env, *, cli: bool = False) -> None:
    """The user's own config before all-agents: one server, one skill folder, a theme key."""
    root = env.home / ROOT_NAME
    (root / "skills" / "user-skill").mkdir(parents=True)
    (root / "skills" / "user-skill" / "SKILL.md").write_text("user skill\n", encoding="utf-8")
    doc = {"theme": "dark", "servers": {"user-server": {"command": "user-cmd", "args": ["-x"]}}}
    if cli:
        (root / "cli-mcp.json").write_bytes(canonical_json(doc).encode("utf-8"))
    else:
        (root / "mcp.json").write_bytes((json.dumps(doc, indent=1) + "\n").encode("utf-8"))
