"""The interface every tool adapter implements.

The rule that defines this project's architecture (docs/decisions/0008):

    Adapters detect, read and plan. They never write.

An adapter returns `Op` objects (`EditFile`, `RunCli`, `Link`, `Unsupported`) and
the engine executes them with backup, journal, verification and undo. Because of
that, a new tool is one module plus a fixture, and every tool gets the same
safety behavior for free.

Required: `detect`, `paths`, `read_*`, `plan_instructions`, `plan_mcp`, `plan_skills`.
Optional (hooks, permissions, memory): the defaults below return an `Unsupported`
op so the plan shows the gap as lossy instead of silently skipping it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import ClassVar

from ..model import (
    Detection,
    Env,
    Instructions,
    Kind,
    McpServer,
    Op,
    Paths,
    Scope,
    SkillRef,
    Unsupported,
)


class ToolAdapter(ABC):
    """Base class for one AI coding tool."""

    #: Short stable id used on the command line and in the journal.
    id: ClassVar[str]
    #: Human-readable name.
    label: ClassVar[str]
    #: Kinds this adapter can express. Everything else is planned as `Unsupported`.
    capabilities: ClassVar[frozenset[Kind]]

    # -- discovery ---------------------------------------------------------

    @abstractmethod
    def detect(self, env: Env) -> Detection:
        """Is the tool installed? Return its binary, version and config roots, per OS."""

    @abstractmethod
    def paths(self, env: Env, scope: Scope) -> Paths:
        """Where this tool keeps instructions, MCP, skills and settings for `scope`."""

    # -- reading (used by init, status and verification) -------------------

    @abstractmethod
    def read_instructions(self, env: Env, scope: Scope) -> Instructions | None:
        """The tool's current instructions file, or None."""

    @abstractmethod
    def read_mcp(self, env: Env, scope: Scope) -> dict[str, McpServer]:
        """The MCP servers currently configured in the tool, by name."""

    @abstractmethod
    def read_skills(self, env: Env, scope: Scope) -> list[SkillRef]:
        """The skills currently visible to the tool."""

    # -- planning (pure: no writes) ----------------------------------------

    @abstractmethod
    def plan_instructions(
        self, env: Env, scope: Scope, desired: str, owned: Mapping[str, object]
    ) -> list[Op]:
        """Ops that make the tool's instructions match `desired`."""

    @abstractmethod
    def plan_mcp(
        self, env: Env, scope: Scope, desired: Mapping[str, McpServer], owned: Mapping[str, object]
    ) -> list[Op]:
        """Ops that make the tool's MCP servers match `desired`, touching only `owned` entries."""

    @abstractmethod
    def plan_skills(
        self, env: Env, scope: Scope, desired: Sequence[SkillRef], owned: Mapping[str, object]
    ) -> list[Op]:
        """Ops that make the skills visible to the tool match `desired`."""

    # -- optional kinds (later releases) -----------------------------------

    def plan_hooks(
        self, env: Env, scope: Scope, desired: object, owned: Mapping[str, object]
    ) -> list[Op]:
        return [Unsupported(self.id, Kind.HOOKS, "hooks are not supported by this adapter yet")]

    def plan_permissions(
        self, env: Env, scope: Scope, desired: object, owned: Mapping[str, object]
    ) -> list[Op]:
        return [
            Unsupported(
                self.id, Kind.PERMISSIONS, "permissions are not supported by this adapter yet"
            )
        ]

    def plan_memory(
        self, env: Env, scope: Scope, desired: object, owned: Mapping[str, object]
    ) -> list[Op]:
        return [Unsupported(self.id, Kind.MEMORY, "memory is not supported by this adapter yet")]

    # -- documentation -----------------------------------------------------

    def notes(self) -> list[str]:
        """Known limits of this tool, shown by `status` (for example, no memory key)."""
        return []
