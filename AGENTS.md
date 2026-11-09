# one-setup-all-agents

One setup for all your AI coding agents: a shared agent config for Claude Code, Codex, OpenCode and Antigravity. The command is `all-agents` (`init`, `sync`, `status`, `undo`). Status: the public commands exist now (pre-alpha, not released).

## Rules for AI tools working in this repo

- Language: everything is English (code, comments, docs, messages, commit messages).
- Stdlib only: the core has no runtime dependencies. Do not add one. Dev-only tools (lint) are allowed in CI, never in `[project].dependencies`.
- Python: minimum 3.11. Do not use syntax or stdlib APIs newer than 3.11 without a version guard.
- Adapters never write: `adapters/<tool>.py` only detects, reads and plans (returns `Op` objects). Only `engine/` writes files, takes backups, keeps the journal and undoes. See `docs/decisions/0008-adapter-engine-boundary.md`.
- Every write goes through the engine's atomic write and is recorded in the journal, so `undo` can reverse it. Never write a tool's config file directly.
- Tests are required: every behavior change ships with a test. Run `python -m unittest discover -s tests` before finishing (it works from a clean checkout with no install; the tests add `src/` to the path). Tests must work on Windows, Linux and macOS and must never touch the real home directory (use a temporary home).
- No personal data: the only allowed personal items are the maintainer's name (in `LICENSE` and the `pyproject.toml` author field) and the public repository URL. Everywhere else: no real usernames, home paths, client or company names, emails, tokens or machine-specific paths. Fixtures use neutral names (`example`, `demo-server`). `scripts/check_personal_data.py` enforces this: the forbidden patterns live in the git-ignored `.personal-patterns` file (or the `PERSONAL_PATTERNS` variable), never in the repository. Enable the pre-commit hook with `git config core.hooksPath scripts/hooks`.
- Secrets are referenced by environment-variable name only. Never print or store a secret value.
- Decisions live in `docs/decisions/` as short ADRs. Change a decision by adding a new ADR, not by silently editing code.
- Do not commit, push or publish unless the maintainer asks.
