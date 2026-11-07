"""Shared data types.

These are the only objects that cross the adapter/engine boundary
(see docs/decisions/0008-adapter-engine-boundary.md):

- Adapters receive an `Env` and a `Scope`, and return `Detection`, `Paths`, read
  results and lists of `Op`.
- The engine consumes `Op` objects and is the only code that touches the disk.

The shapes follow docs/decisions/0008 (boundary) and 0009 (interface v1: item keys,
ownership claims, conflicts, neutral MCP fields). Still pre-alpha: porting the first real
adapter may refine them, through a new ADR.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ._core.mask import env_value_looks_secret


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


#: The item key of a tool's instructions. The instructions file is owned as one item.
#: The item keys of the other kinds are the MCP server name and the skill name.
INSTRUCTIONS_KEY = "instructions"

#: What an adapter receives as `owned`: the item keys of ONE kind that the ledger records as
#: created by this tool's adapter (docs/decisions/0005, 0009). Anything else is the user's.
Owned = frozenset[str]


@dataclass(frozen=True)
class ItemRef:
    """One managed item: a tool, a kind and the item key (see `INSTRUCTIONS_KEY`).

    Ops carry these in `claims` and `releases`, so the engine can update the ledger's
    `owned` table without knowing anything about the tool.
    """

    tool: str
    kind: Kind
    key: str


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
class HeaderRef:
    """An HTTP header whose value comes from an environment variable (`Bearer ${TOKEN}`).

    Only the variable NAME is stored. A literal header value is not representable.
    """

    name: str  # header name, for example "Authorization"
    env_var: str  # name of the environment variable that holds the value
    scheme: str | None = None  # word before the value, for example "Bearer"


@dataclass(frozen=True)
class McpServer:
    """A tool-neutral MCP server definition. A secret is referenced by env-var NAME, never stored.

    - `env_vars`: names of variables the tool passes on from its own environment.
    - `env`: literal NON-secret values to set (for example `MODE=test`), as `(name, value)`
      pairs sorted by name. A value that looks like a secret raises `ValueError` here, so no
      adapter can carry one; a name may not be in both `env` and `env_vars`.
    - `headers`: http header values always come from a variable (`HeaderRef`); literal header
      values are not representable.

    A server is stdio (`command`, `args`, `cwd`) or http (`url`, `headers`); `transport` follows
    from which one is set. A tool that cannot express a field loses it; the adapter's
    `normalize_mcp` says how (docs/decisions/0009).
    """

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env_vars: tuple[str, ...] = ()  # names of environment variables, never values
    env: tuple[tuple[str, str], ...] = ()  # literal non-secret values, sorted by name
    headers: tuple[HeaderRef, ...] = ()  # http only; values come from environment variables
    cwd: str | None = None  # stdio only
    auth: str | None = None  # None, "oauth" or "none"; documentation, no tool needs a key for it
    enabled: bool = True
    startup_timeout_sec: int | None = None  # only some tools can express it

    def __post_init__(self) -> None:
        pairs = self.env.items() if isinstance(self.env, Mapping) else self.env
        env = tuple(sorted((str(k), str(v)) for k, v in pairs))
        names = [k for k, _ in env]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.name}: a name appears twice in env")
        both = sorted(set(names) & set(self.env_vars))
        if both:
            raise ValueError(f"{self.name}: {both} is in both env and env_vars")
        for key, value in env:
            if env_value_looks_secret(key, value):
                raise ValueError(
                    f"{self.name}: env {key} looks like a secret; pass it by name in env_vars"
                )
        object.__setattr__(self, "env", env)

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


@dataclass(frozen=True)
class SkillRef:
    """A skill folder (a directory containing SKILL.md) as seen on disk."""

    name: str
    path: Path
    is_link: bool = False


@dataclass(frozen=True)
class WantedLink:
    """A skill link an adapter wants to exist after a sync, whether or not it plans any op for it.

    Adapters plan nothing for a link that is already as wanted, so the plan alone cannot tell the
    engine that a path is still in use. `ToolAdapter.wanted_links` returns these so that the
    engine can keep a link that one tool withdraws while another still wants it
    (docs/decisions/0012).
    """

    link: Path  # where the link lives
    target: Path  # the source folder it must point at
    ref: ItemRef  # the item the wanting tool would own
    allow_copy_fallback: bool = True


# ---------------------------------------------------------------------------
# Operations. The engine executes EditFile, RunCli and Link and nothing else. Unsupported and
# Conflict are plan entries that are never executed: they make a gap or a refusal visible.
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
    claims: tuple[ItemRef, ...] = ()  # items this op creates or updates: they become `owned`
    releases: tuple[ItemRef, ...] = ()  # items this op removes: they leave `owned`


@dataclass(frozen=True)
class RunCli:
    """Run a tool's own CLI because the tool owns the file (for example to edit its MCP list).

    `inverse_argv` is required: undo runs it. An op without an inverse is not allowed.
    """

    argv: Sequence[str]
    inverse_argv: Sequence[str]
    description: str
    claims: tuple[ItemRef, ...] = ()
    releases: tuple[ItemRef, ...] = ()


@dataclass(frozen=True)
class Link:
    """Make `link` point at `target` (symlink, junction, or copy fallback).

    `target=None` removes the link. It must be a link, junction or marked copy that the ledger
    records as owned; this is how a skill that left the source is withdrawn.
    """

    link: Path
    target: Path | None
    description: str
    allow_copy_fallback: bool = False
    claims: tuple[ItemRef, ...] = ()
    releases: tuple[ItemRef, ...] = ()


@dataclass(frozen=True)
class Unsupported:
    """A kind the tool cannot express. Shown in the plan as lossy instead of silently dropped."""

    tool: str
    kind: Kind
    reason: str


@dataclass(frozen=True)
class Conflict:
    """An item the plan will not touch, with the reason. Never executed.

    Two cases (docs/decisions/0007, 0003): the destination exists and the ledger does not record
    it as owned (the user's own entry is left alone), or the desired content cannot be written
    (for example instructions over a tool's size limit). Different from `Unsupported`, which
    means the tool cannot express the kind at all.
    """

    tool: str
    kind: Kind
    key: str  # the item key (see `INSTRUCTIONS_KEY`)
    reason: str
    path: Path | None = None  # the destination that is in the way, if there is one


Op = EditFile | RunCli | Link | Unsupported | Conflict

#: The ops the engine executes. The other ops only describe the plan.
EXECUTABLE_OPS = (EditFile, RunCli, Link)


@dataclass
class Plan:
    """An ordered list of operations produced by the adapters, ready for review or apply."""

    ops: list[Op] = field(default_factory=list)

    @property
    def executable(self) -> list[Op]:
        """The ops the engine would run."""
        return [op for op in self.ops if isinstance(op, EXECUTABLE_OPS)]

    @property
    def conflicts(self) -> list[Conflict]:
        return [op for op in self.ops if isinstance(op, Conflict)]

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to run and nothing to report.

        `Unsupported` entries do not count: they are a permanent gap of the tool, not a pending
        change. A `Conflict` does count: the desired state is not reached and the user must be
        told, so a conflict-only plan is not empty (and has no executable ops).
        """
        return not any(isinstance(op, (*EXECUTABLE_OPS, Conflict)) for op in self.ops)
