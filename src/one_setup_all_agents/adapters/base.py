"""The interface every tool adapter implements.

The rule that defines this project's architecture (docs/decisions/0008):

    Adapters detect, read and plan. They never write.

An adapter returns `Op` objects and the engine executes them with backup, journal,
verification and undo. Because of that, a new tool is one module plus a fixture, and every
tool gets the same safety behavior for free. The details of the interface are settled in
docs/decisions/0009-adapter-interface-v1.md:

- `desired` is the neutral state for ONE kind: a `str` for instructions, `{name: McpServer}`
  for MCP, a sequence of `SkillRef` (source folders) for skills.
- `owned` is a `frozenset` of the item keys of that kind which the ledger says this adapter
  created: the MCP server names, the skill names, or `{INSTRUCTIONS_KEY}`. An adapter touches
  only those, plus creates what is missing. An existing item that is not owned and differs
  from `desired` is reported as a `Conflict` and left alone; it is never overwritten.
- Every executable op (`EditFile`, `RunCli`, `Link`) lists the items it creates or updates in
  `claims` and the items it removes in `releases`. This is how the ledger learns what is owned.
  Ops that create nothing new and remove nothing carry neither.
- Ops that are never executed: `Unsupported` (the tool cannot express the kind) and
  `Conflict` (the item is in the way or cannot be written). They keep the plan honest.
- `read_*` return what is configured now, including entries the user made. Reading what a
  plan produces gives back `desired` after `normalize_*`, because a tool may legitimately lose
  information (for example, a field its config format has no place for).

Required: `detect`, `paths`, `read_*`, `plan_instructions`, `plan_mcp`, `plan_skills`.
Optional (hooks, permissions, memory): the defaults below return an `Unsupported`
op so the plan shows the gap as lossy instead of silently skipping it.

The reusable contract test is `tests/adapter_contract.py` (CONTRIBUTING.md). It applies and
undoes the ops with the real engine (`engine/`), so a passing adapter has shown, in a temporary
home, that its ops are undone to the exact bytes, modes, links and ownership. What that does and
does not prove is in docs/decisions/0010-engine-v1.md.
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
    Owned,
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
        """Where this tool keeps instructions, MCP, skills and settings for `scope`.

        Every path is derived from `env` (home, platform, environ); never from the real OS.
        """

    # -- reading (used by init, status and verification) -------------------

    @abstractmethod
    def read_instructions(self, env: Env, scope: Scope) -> Instructions | None:
        """The tool's current instructions file, or None."""

    @abstractmethod
    def read_mcp(self, env: Env, scope: Scope) -> dict[str, McpServer]:
        """The MCP servers currently configured in the tool, by name (the user's included)."""

    @abstractmethod
    def read_skills(self, env: Env, scope: Scope) -> list[SkillRef]:
        """The skills currently visible to the tool (the user's included)."""

    # -- planning (pure: no writes) ----------------------------------------

    @abstractmethod
    def plan_instructions(self, env: Env, scope: Scope, desired: str, owned: Owned) -> list[Op]:
        """Ops that make the tool's instructions match `desired`.

        `owned` is `frozenset({INSTRUCTIONS_KEY})` when the ledger says the file is ours, else
        empty. A file that exists, is not owned and differs from `desired` is a `Conflict`; so
        is a `desired` text over the tool's size limit (ADR 0003).
        """

    @abstractmethod
    def plan_mcp(
        self, env: Env, scope: Scope, desired: Mapping[str, McpServer], owned: Owned
    ) -> list[Op]:
        """Ops that make the tool's MCP servers match `desired`, touching only `owned` entries.

        Owned names missing from `desired` are removed (`releases`). A desired name that exists,
        is not owned and differs is a `Conflict`.
        """

    @abstractmethod
    def plan_skills(
        self, env: Env, scope: Scope, desired: Sequence[SkillRef], owned: Owned
    ) -> list[Op]:
        """Ops that make the skills visible to the tool match `desired`.

        Owned skills missing from `desired` are withdrawn with `Link(target=None)`. A destination
        that exists and is not owned is a `Conflict` (ADR 0007).
        """

    # -- optional kinds (later releases) -----------------------------------

    def plan_hooks(self, env: Env, scope: Scope, desired: object, owned: Owned) -> list[Op]:
        return [Unsupported(self.id, Kind.HOOKS, "hooks are not supported by this adapter yet")]

    def plan_permissions(self, env: Env, scope: Scope, desired: object, owned: Owned) -> list[Op]:
        return [
            Unsupported(
                self.id, Kind.PERMISSIONS, "permissions are not supported by this adapter yet"
            )
        ]

    def plan_memory(self, env: Env, scope: Scope, desired: object, owned: Owned) -> list[Op]:
        return [Unsupported(self.id, Kind.MEMORY, "memory is not supported by this adapter yet")]

    # -- round-trip normalization (used by the contract test and by `status`) ---

    def normalize_instructions(self, text: str) -> str:
        """What `read_instructions` returns after `desired` was written. Default: unchanged."""
        return text

    def normalize_mcp(self, servers: Mapping[str, McpServer]) -> dict[str, McpServer]:
        """What `read_mcp` returns for these servers after they were written.

        Default: unchanged. Override it for exactly the fields this tool cannot store (for
        example reset `startup_timeout_sec` to None). Anything the override does not list must
        survive a round trip.
        """
        return dict(servers)

    # -- documentation -----------------------------------------------------

    def notes(self) -> list[str]:
        """Known limits of this tool, shown by `status` (for example, no memory key)."""
        return []
