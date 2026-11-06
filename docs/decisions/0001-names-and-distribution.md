# 0001: Names and distribution

Status: Accepted

## Decision

| Thing | Value |
|---|---|
| Repository | `one-setup-all-agents` |
| PyPI distribution name | `one-setup-all-agents` |
| Import package | `one_setup_all_agents` (under `src/`) |
| Main command | `all-agents` with `init`, `sync`, `status`, `undo` |
| Alias command | `one-setup-all-agents` (same entry point) |
| Positioning | One setup for all your AI coding agents. Write your config once, every agent gets the same setup, any change can be undone with one command. |
| Required phrase | The project description contains `shared agent config` and names Claude Code, Codex, OpenCode and Antigravity. |
| License | MIT |
| Versioning | PEP 440, `0.x` until the journal format is stable; version is single-sourced from `__init__.py`. |
| Install | `uvx` or `pipx`; no other install path is supported in v0.1. |
| Publishing | PyPI trusted publishing (OIDC) from GitHub Actions, no long-lived PyPI token. |
| Repository URL | `https://github.com/beingmomen/one-setup-all-agents` |
| Author | `Abdelmomen Elshatory` (name only, no email). |
| Build backend | `hatchling` (build-time only; not a runtime dependency). |

The project does not compete on the number of supported tools. It competes on being safe to run against your real config.

## Reason

- `uvx <name>` runs the command called `<name>`. The command is `all-agents`, so the alias `one-setup-all-agents` is also registered; without it `uvx one-setup-all-agents` would fail and users would need `uvx --from one-setup-all-agents all-agents`.
- The exact phrase `shared agent config` is what people type when they search for this kind of tool.

## Open points

- On 2026-11-06 neither `one-setup-all-agents` nor `all-agents` was registered on PyPI (HTTP 404 on the JSON API). Reserve the PyPI name early by publishing a pre-release; availability can change.
- Whether another program named `all-agents` is commonly on a user's PATH is not checked.
- The release workflow is not written yet; it is a release-phase task. Do not add one before v0.1 is ready.
