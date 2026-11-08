"""Read tool configurations safely, collecting unreadable config problems."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._core.mask import mask_text
from .adapters.base import ConfigUnreadable, ToolAdapter
from .model import Detection, Env, Instructions, Kind, McpServer, Scope, SkillRef


@dataclass(frozen=True)
class Problem:
    """Problem encountered while attempting to read a tool's configuration."""

    tool: str
    kind: Kind
    path: Path | None
    message: str


@dataclass(frozen=True)
class ToolReading:
    """Result of reading all supported configuration kinds from one tool."""

    tool: str
    label: str
    instructions: Instructions | None
    mcp: Mapping[str, McpServer]
    skills: tuple[SkillRef, ...]
    problems: tuple[Problem, ...]


def read_tool(adapter: ToolAdapter, env: Env, scope: Scope = Scope.GLOBAL) -> ToolReading:
    """Read instructions, MCP, and skills for an adapter. Catch only ConfigUnreadable."""
    paths = adapter.paths(env, scope)
    problems: list[Problem] = []

    instructions: Instructions | None = None
    try:
        instructions = adapter.read_instructions(env, scope)
    except ConfigUnreadable as exc:
        problems.append(
            Problem(
                tool=adapter.id,
                kind=Kind.INSTRUCTIONS,
                path=getattr(exc, "path", paths.instructions),
                message=mask_text(str(exc)),
            )
        )

    mcp: dict[str, McpServer] = {}
    try:
        mcp = dict(adapter.read_mcp(env, scope))
    except ConfigUnreadable as exc:
        problems.append(
            Problem(
                tool=adapter.id,
                kind=Kind.MCP,
                path=getattr(exc, "path", paths.mcp),
                message=mask_text(str(exc)),
            )
        )

    skills: tuple[SkillRef, ...] = ()
    try:
        skills = tuple(adapter.read_skills(env, scope))
    except ConfigUnreadable as exc:
        problems.append(
            Problem(
                tool=adapter.id,
                kind=Kind.SKILLS,
                path=getattr(exc, "path", paths.skills_dir),
                message=mask_text(str(exc)),
            )
        )

    return ToolReading(
        tool=adapter.id,
        label=adapter.label,
        instructions=instructions,
        mcp=mcp,
        skills=skills,
        problems=tuple(problems),
    )


def read_all(
    env: Env,
    adapters: Sequence[ToolAdapter | type[ToolAdapter]],
    detections: Sequence[Detection],
) -> list[ToolReading]:
    """Read configuration from all adapters whose detection reports installed=True."""
    adapter_by_id = {
        (a.id if isinstance(a, ToolAdapter) else a.id): (a if isinstance(a, ToolAdapter) else a())
        for a in adapters
    }
    readings: list[ToolReading] = []
    for d in detections:
        if d.installed and d.tool and d.tool in adapter_by_id:
            readings.append(read_tool(adapter_by_id[d.tool], env))
    return readings
