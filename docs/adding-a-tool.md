# Guide to Adding a New Tool Adapter

This guide walks you through adding a new tool adapter to `one-setup-all-agents`.

Before starting, read `docs/decisions/0008-adapter-engine-boundary.md` and `docs/decisions/0009-adapter-interface-v1.md`.

## The Core Rule: Separation of Concerns

The central architectural rule of this project is:

> **Adapters detect, read and plan. They never write.**

Only the `engine/` package writes files, takes backups, manages locks, writes journals, and executes undos. Because adapters only plan operations (`Op` objects) rather than executing them, every tool gets atomic updates, backup snapshots, drift protection, and complete multi-step undo for free.

## Prerequisites

- Python 3.11 or newer.
- No new runtime dependencies: all adapter logic must rely exclusively on the Python standard library.
- Neutral fixtures: test fixtures must never contain real user paths, personal usernames, real tokens, or machine-dependent paths.
- Two boundary tests protect this architecture:
  - `tests/test_tool_boundary.py`: static boundary test asserting that no file outside `adapters/` (and its tests) references any concrete tool id, label, or tool-specific configuration file.
  - `tests/test_drop_in_tool.py`: dynamic boundary test proving that an adapter can be added in a single module without touching engine or model code.
  - See `tests/demo_tool_adapter.py` for a complete reference implementation of a drop-in adapter.

## The ToolAdapter Contract

Every adapter inherits from `ToolAdapter` (in `src/one_setup_all_agents/adapters/base.py`).

### Required Class Attributes

- `id`: A unique, short lowercase identifier used in CLI arguments and journal ledgers (e.g. `"demo-tool"`).
- `label`: A human-readable display name (e.g. `"Demo Tool"`).
- `capabilities`: A `frozenset[Kind]` listing the configuration kinds this tool can express (`Kind.INSTRUCTIONS`, `Kind.MCP`, `Kind.SKILLS`).

### Discovery Methods

- `detect(env)`: Abstract method. Checks whether the tool is installed in `env` and returns a `Detection` object containing installation status, configuration directory roots, and any detection warnings. It is strictly read-only and never spawns processes.
- `version_probe(env, binary)`: Declares how to probe the binary's version without executing it directly during detection. Defaults to `VersionProbe((str(binary), "--version"), parse_version)`. Return `None` if running the tool has unavoidable side effects.
- `paths(env, scope)`: Abstract method. Returns a `Paths` dataclass containing the locations of instructions, MCP configuration, settings, and skills directory for the given `Scope` (e.g. global). All paths must be derived from `env.home` or `env.environ`; never query the host operating system directly.

### Reading Methods

- `read_instructions(env, scope)`: Abstract method. Reads and returns the current `Instructions` file (with `path` and decoded `text`), or `None` if absent.
- `read_mcp(env, scope)`: Abstract method. Reads the tool's configured MCP servers into a dictionary `{name: McpServer}`, including any unowned user servers.
- `read_skills(env, scope)`: Abstract method. Reads the skills visible to the tool, returning a list of `SkillRef`.
- `snapshot_files(env, scope)`: Returns a tuple of existing configuration file paths to preserve in the safety backup snapshot prior to any writes. Defaults to paths for instructions, MCP, and settings.
- `import_notes(env, scope)`: Returns a list of `ImportNote` items describing unimported secrets or unsupported settings during initialisation. Defaults to an empty list.

### Planning Methods

- `plan_instructions(env, scope, desired, owned)`: Abstract method. Compares `desired` instructions against the current state and returns `Op` objects. If the file exists and is unowned, return a `Conflict`.
- `plan_mcp(env, scope, desired, owned)`: Abstract method. Compares desired MCP servers against current servers, updating only owned entries. Returns `Op` objects (e.g. `EditFile` or `RunCli`).
- `plan_skills(env, scope, desired, owned)`: Abstract method. Compares desired skills against current skills and returns `Link` ops linking or unlinking skill directories.
- `wanted_links(env, scope, desired)`: Returns `WantedLink` descriptors for skill links this adapter desires, preventing premature deletion when multiple tools share a common directory.
- `plan_hooks(env, scope, desired, owned)`: Hook planning for future releases. Default returns an `Unsupported` op.
- `plan_permissions(env, scope, desired, owned)`: Permission planning for future releases. Default returns an `Unsupported` op.
- `plan_memory(env, scope, desired, owned)`: Memory planning for future releases. Default returns an `Unsupported` op.

### Normalization and Documentation

- `normalize_instructions(text)`: Normalizes instruction text to match what survives a write and read cycle. Defaults to returning `text` unchanged.
- `normalize_mcp(servers)`: Normalizes desired `McpServer` mappings by stripping fields the tool's format cannot store (e.g. resetting `startup_timeout_sec=None`). Anything not explicitly stripped must survive round-trip tests.
- `notes()`: Returns a list of strings documenting known limits or quirks of the tool displayed by `status`.

## Operation Rules

When planning operations, every adapter must adhere to the following rules:

1. **Claims and Releases**:
   - Every state-modifying op (`EditFile`, `RunCli`, `Link`) must set `claims` for items it creates or updates, and `releases` for items it removes.
   - Items are referenced by `ItemRef(adapter.id, kind, item_key)`.
   - The engine ledger uses these to track ownership.
2. **Mandatory Inverse for CLI Commands**:
   - Every `RunCli` operation must specify `inverse_argv`.
   - Without an inverse command, the engine cannot undo changes.
3. **Conflict Over Overwrite**:
   - Never overwrite existing, unowned configuration. If a user already created a server or file that differs from `desired`, yield a `Conflict` op instead of replacing it.

## Testing and Contract Suite

### Reusable Contract Suite

Every adapter is tested against `tests/adapter_contract.py`. To plug into the contract test runner:

- `REGISTERED_SEEDS`: In `tests/test_adapter_contract.py`, map your adapter id to a seed function `seed_home(env)` that creates initial fixture files (e.g. the user's own native config before `all-agents`).
- `REGISTERED_EXTRAS`: Optional mapping in `tests/test_adapter_contract.py` from adapter id to a function returning extra members for its contract class (the hooks `make_env` and `make_cli` of `AdapterContract`, needed by an adapter that plans `RunCli` ops).
- `unwritten_mcp`: Hook returning names of harness desired MCP servers that this adapter deliberately does not write (defaults to `frozenset()`).
- `removable_mcp`: Hook specifying the name of an MCP server to delete during the removal test (defaults to `"demo-remote"`).

## Registering the Adapter

Once your adapter class is implemented, register it in `src/one_setup_all_agents/adapters/__init__.py`:

1. Import your adapter class (e.g. `from .demo_tool import DemoToolAdapter`).
2. Add the adapter class to the `ADAPTERS` tuple:

```python
from .demo_tool import DemoToolAdapter

ADAPTERS: tuple[type[ToolAdapter], ...] = (
    # ...,
    DemoToolAdapter,
)
```

Adding the adapter to `ADAPTERS` in `adapters/__init__.py` automatically exposes it to discovery, reading, planning, CLI help text generation, and the boundary tests.

### Platform Behavior Rule

Always label unverified platform behavior. If a file location or CLI option is not confirmed by a live test on a given operating system (Windows, Linux, macOS), state that it is unverified in your docstrings, in `notes()`, and in the accompanying decision record. Never guess platform behaviors.

## Worked Example

Below are excerpts from `tests/demo_tool_adapter.py` illustrating a complete adapter implementation.

### Class Definition and Paths

<!-- excerpt: tests/demo_tool_adapter.py -->
```python
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
```

### Reading State and Planning Instructions

<!-- excerpt: tests/demo_tool_adapter.py -->
```python
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


def plan_skills(
    self, env: Env, scope: Scope, desired: Sequence[SkillRef], owned: Owned
) -> list[Op]:
    return plan_skill_links(self.id, self.paths(env, scope).skills_dir, desired, owned)
```

### Planning MCP with Conflict Detection

<!-- excerpt: tests/demo_tool_adapter.py -->
```python
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


def normalize_mcp(self, servers: Mapping[str, McpServer]) -> dict[str, McpServer]:
    return {n: replace(s, startup_timeout_sec=None) for n, s in servers.items()}
```
