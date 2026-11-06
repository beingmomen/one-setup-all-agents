# 0008: Adapter and engine boundary

Status: Accepted

## Decision

- An adapter **detects, reads and plans. It never writes.** It returns `Op` objects. Only `engine/` touches the disk, takes backups, keeps the journal, verifies and undoes.
- The engine executes exactly three kinds of op:
  - `EditFile(path, transform, expect_sha256, ...)`: atomic write with verification and journal (0005, 0006).
  - `RunCli(argv, inverse_argv, ...)`: for files a tool owns itself. The inverse is mandatory.
  - `Link(link, target, ...)`: junction, symlink or copy fallback (0007).
  - Plus `Unsupported(tool, kind, reason)`, which is never executed; it makes a gap visible in the plan as lossy.
- The adapter interface (`adapters/base.py`): `id`, `label`, `capabilities`; `detect`, `paths`; `read_instructions`, `read_mcp`, `read_skills`; `plan_instructions`, `plan_mcp`, `plan_skills`; optional `plan_hooks`, `plan_permissions`, `plan_memory` (default: `Unsupported`); `notes`.
- Adapters receive an explicit `Env` (home, platform, environment, config and state directories). They do not read the OS themselves, which makes them testable on any machine.
- `plan_*` functions are pure. Planning twice, with an apply in between, yields an empty second plan.
- Adapters touch only entries recorded as owned in the ledger.
- One adapter contract test runs against every registered adapter. A tool is not merged until it passes: reading what its plan produces returns the desired state, a second plan is empty, undo restores the original bytes.
- Adapters live in-tree. There is no plugin or entry-point mechanism in v0.x.
- Adapter ids: `claude`, `codex`, `opencode`, `agy`. Module names are descriptive (`claude_code.py`, `antigravity.py`).

## Reason

- Safety and undo are properties of the engine, so they cannot differ per tool. If adapters wrote files, each would need its own backup and rollback; that is how earlier prototypes ended up with seven write paths.
- A new tool becomes one module (about 150 to 300 lines) plus a fixture and a registry line, instead of edits across many files.
- Making unsupported kinds explicit keeps the product honest: a tool that cannot express something says so in the plan.

## Open points

- The exact shape of `desired` and `owned` arguments is a sketch; it will be fixed when the first adapter (OpenCode) is ported, and this ADR updated.
- A third-party adapter mechanism (entry points) is deferred until the interface is stable.
- Whether some tools need an op kind beyond these three (for example an edit that must also restart something) is unknown until Claude Code and Antigravity are ported.
