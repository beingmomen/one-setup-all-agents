# Contributing

The project is pre-alpha; the interface in `src/one_setup_all_agents/adapters/base.py` may still change. Open an issue before starting a large change.

## Setup

```text
pip install -e .
python -m unittest discover -s tests
```

Python 3.11 or newer. The core has no runtime dependencies, and this will not change. Tests must run on Windows, Linux and macOS and must never touch your real home directory.

## Adding a tool adapter

The architecture has one rule: **adapters detect, read and plan; they never write.** Only `engine/` writes files, backs them up, journals them and undoes them. That is what makes `undo` work the same for every tool. Read `docs/decisions/0008-adapter-engine-boundary.md` first.

A new tool is:

1. **`src/one_setup_all_agents/adapters/<tool>.py`**: a subclass of `ToolAdapter` with a unique `id`, a `label` and the `capabilities` it supports. Implement:
   - `detect(env)`: is the tool installed, where is its binary and config, which version.
   - `paths(env, scope)`: where it keeps instructions, MCP servers and skills, per OS. Honor the tool's own override variables.
   - `read_instructions`, `read_mcp`, `read_skills`: what is configured now.
   - `plan_instructions`, `plan_mcp`, `plan_skills`: pure functions that return `Op` objects (`EditFile`, `RunCli`, `Link`). Touch only entries the tool owns (the `owned` argument, a `frozenset` of item keys: server names, skill names, or `instructions`). List the items an op creates in `claims` and the ones it removes in `releases`. Report an existing, unowned entry that is in the way as a `Conflict` instead of writing. Every `RunCli` needs an inverse command.
   - Kinds the tool cannot express need no code: the base class reports them as `Unsupported` so the plan shows the gap.
   - If the tool cannot store a field of the neutral model, override `normalize_mcp` (or `normalize_instructions`) to say exactly which.
   - `notes()`: known limits worth showing to the user.
   - `import_notes(env, scope)`: optional notes on unimported secrets or settings during initialisation.
2. **A registry entry** in `adapters/__init__.py`: one import line and one entry in the `ADAPTERS` tuple. Nothing else in that file changes.
3. **A fixture**: a fake home directory with the tool's config files, under `tests/fixtures/`. Use neutral names (`example`, `demo-server`); no real paths, users or secrets.
4. **The contract test** must pass for the adapter (`docs/decisions/0008` and `0009`). It is `tests/adapter_contract.py`, a reusable mixin, and `tests/test_adapter_contract.py` already creates a test class that runs the full suite for every adapter in `ADAPTERS`. Add a seed function for your fixture home to `REGISTERED_SEEDS` in that file only if the home should start with the user's own config (and a `REGISTERED_EXTRAS` entry only if your adapter plans `RunCli` ops or needs a different `make_env`, `unwritten_mcp` or `removable_mcp`). The suite checks, in a temporary home: reading what a plan produces returns the desired state (after the adapter's `normalize_*`), a second plan is empty, `plan_*` and `read_*` write nothing, entries that are not owned are left alone (a clash is a `Conflict`), owned entries that leave the source are removed, `paths()` stays under the given `Env`, every kind outside `capabilities` plans as `Unsupported`, every `RunCli` has an inverse, and undo restores the original bytes. Run it with `python -m unittest discover -s tests` (or `python -m unittest test_adapter_contract` from `tests/`). The suite applies and undoes your ops with the real engine (`tests/contract_applier.py`, one seam: `make_applier`, which calls `engine/`): journal, backups, atomic writes, drift check and ledger. A pass shows that, in a temporary home on the OS the tests run on, your ops are undone to the exact bytes, permission bits, links and ownership. **Caveat:** it does not prove anything about the real tool (where it keeps files, whether it follows links, what a real CLI does) or the `undo` command itself (Phase 5); `docs/decisions/0010-engine-v1.md` lists what is and is not proven.
5. **Nothing else changes.** You do not edit `engine/`, `model.py`, `cli.py`, `adapters/base.py`, `adapters/_common.py` or another adapter. Two tests hold you to it:
   - `tests/test_tool_boundary.py` scans every source file outside the adapter modules (and the legacy `_core/`) and fails if one names a tool, its id or label, or a tool's config file name. If you need something the engine or base class does not offer, add a generic hook with a default to `adapters/base.py` instead of a special case, and keep the other adapters' behavior unchanged.
   - `tests/test_drop_in_tool.py` adds a made-up tool (`tests/demo_tool_adapter.py`, one module) with a patched registry and shows that it is found, planned, applied and undone next to the four real tools without touching their files, that every op it plans lies under its own config folder, and that the contract suite runs for it. Use that module as a template.
6. **Unverified behavior is labeled.** If you could not check a file location or a behavior on one OS, say so in the adapter docstring and in the ADR, rather than guessing.

## Pull requests

- English only: code, comments, docs and commit messages.
- Every behavior change comes with a test.
- No personal data in code, fixtures or docs. `python scripts/check_personal_data.py` checks it against a pattern list you keep in the git-ignored file `.personal-patterns` (one regular expression per line); `git config core.hooksPath scripts/hooks` runs the same check on every commit.
- A change to a recorded decision needs a new ADR in `docs/decisions/` (a short file: decision, reason, open points).
