# 0004: Minimum Python and zero runtime dependencies

Status: Accepted

## Decision

- Minimum supported Python is **3.11**.
- `[project].dependencies` stays empty. The core uses the standard library only.
- Tests use `unittest`, so running them needs nothing installed.
- CI runs the tests on Windows, Linux and macOS with Python 3.11 (the floor) and 3.13. Lint (`ruff`) runs in CI only and is never a runtime or install dependency.
- APIs newer than 3.11 (for example `os.path.isjunction`, added in 3.12) are used only behind a guard, with a fallback.

## Reason

- `tomllib` (reading TOML, needed for Codex's config and our own `config.toml`) arrived in 3.11. Writing TOML is not in the standard library, so Codex edits are textual and surgical, which also preserves the user's comments.
- A tool that edits other tools' real config files should be easy to audit and have no supply-chain surface. Zero dependencies is part of the safety story.
- Testing the floor in CI is the only way to know the floor is real.

## Open points

- 3.11 on macOS and Linux has not been run yet; CI will be the first check. If it fails, the floor moves up and this ADR is superseded.
- 3.11 reaches end of life in October 2027. Plan the floor bump then.
- Python 3.14 is not in the CI matrix yet; add it once runners provide it by default.
