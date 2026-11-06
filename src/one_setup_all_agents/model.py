"""Shared data types.

These are the only objects that cross the adapter/engine boundary
(see docs/decisions/0008-adapter-engine-boundary.md):

- Adapters receive an `Env` and a `Scope`, and return `Detection`, `Paths`, read
  results and lists of `Op`.
- The engine consumes `Op` objects and is the only code that touches the disk.

Phase 0: the shapes below are the contract sketched in the design; field details
are expected to change while the first adapter is ported, before v0.1.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


class Scope(enum.Enum):
    """Where a setting applies. v0.1 supports GLOBAL only; PROJECT arrives in v0.2."""

    GLOBAL = "global"
    PROJECT = "project"


class Kind(enum.Enum):
    """The kinds of configuration the tool manages."""

    INSTRUCTIONS = "instructions"
    MCP = "mcp"
    SKILLS = "skills"
    # Later releases (not in v0.1):
    HOOKS = "hooks"
    PERMISSIONS = "permissions"
    MEMORY = "memory"


@dataclass(frozen=True)
class Env:
    """Everything an adapter may know about the machine. No adapter reads the OS directly.

    Making this explicit lets tests run against a temporary home and lets the same
    code run on Windows, Linux and macOS.
    """

    home: Path
    platform: str  # "windows" | "linux" | "macos"
    environ: Mapping[str, str]
    config_dir: Path  # the user's source of truth (see docs/decisions/0002)
    state_dir: Path  # journal, backups, lock (see docs/decisions/0002)


@dataclass(frozen=True)
class Detection:
    """Result of `ToolAdapter.detect`: is the tool present, and where."""

    installed: bool
    binary: Path | None = None
    version: str | None = None
    config_roots: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Paths:
    """Where one tool keeps each kind of config for a given scope and OS."""

    instructions: Path | None = None
    mcp: Path | None = None
    skills_dir: Path | None = None
    settings: Path | None = None


@dataclass(frozen=True)
class Instructions:
    """A tool's current global instructions file."""

    path: Path
    text: str


@dataclass(frozen=True)
class McpServer:
    """A tool-neutral MCP server definition. Secrets are env-var names only, never values."""

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env_vars: tuple[str, ...] = ()  # names of environment variables, never values


@dataclass(frozen=True)
class SkillRef:
    """A skill folder (a directory containing SKILL.md) as seen on disk."""

    name: str
    path: Path
    is_link: bool = False


# ---------------------------------------------------------------------------
# Operations. The engine executes exactly these four and nothing else.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditFile:
    """Replace a file's bytes. `transform` is pure: old bytes (or None if absent) -> new bytes.

    The engine applies it atomically (docs/decisions/0006), verifies `expect_sha256`
    before writing (None means "the file must not exist"), and journals the change.
    """

    path: Path
    transform: Callable[[bytes | None], bytes]
    expect_sha256: str | None
    description: str
    sensitive: bool = False  # new file gets mode 0600 instead of the umask default


@dataclass(frozen=True)
class RunCli:
    """Run a tool's own CLI because the tool owns the file (for example MCP via `claude mcp`).

    `inverse_argv` is required: undo runs it. An op without an inverse is not allowed.
    """

    argv: Sequence[str]
    inverse_argv: Sequence[str]
    description: str


@dataclass(frozen=True)
class Link:
    """Make `link` point at `target` (symlink, junction, or copy fallback)."""

    link: Path
    target: Path
    description: str
    allow_copy_fallback: bool = False


@dataclass(frozen=True)
class Unsupported:
    """A kind the tool cannot express. Shown in the plan as lossy instead of silently dropped."""

    tool: str
    kind: Kind
    reason: str


Op = EditFile | RunCli | Link | Unsupported


@dataclass
class Plan:
    """An ordered list of operations produced by the adapters, ready for review or apply."""

    ops: list[Op] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(not isinstance(op, Unsupported) for op in self.ops)
