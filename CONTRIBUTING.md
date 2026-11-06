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
   - `plan_instructions`, `plan_mcp`, `plan_skills`: pure functions that return `Op` objects (`EditFile`, `RunCli`, `Link`). Touch only entries the tool owns (the `owned` argument). Every `RunCli` needs an inverse command.
   - Kinds the tool cannot express need no code: the base class reports them as `Unsupported` so the plan shows the gap.
   - `notes()`: known limits worth showing to the user.
2. **A registry entry** in `adapters/__init__.py`.
3. **A fixture**: a fake home directory with the tool's config files, under `tests/fixtures/`. Use neutral names (`example`, `demo-server`); no real paths, users or secrets.
4. **The contract test** (added in Phase 3) must pass for the adapter: reading what the plan renders gives back the desired state, planning again after an apply gives an empty plan, and undo restores the original bytes.
5. **Unverified behavior is labeled.** If you could not check a file location or a behavior on one OS, say so in the adapter docstring and in the ADR, rather than guessing.

## Pull requests

- English only: code, comments, docs and commit messages.
- Every behavior change comes with a test.
- No personal data in code, fixtures or docs.
- A change to a recorded decision needs a new ADR in `docs/decisions/` (a short file: decision, reason, open points).
