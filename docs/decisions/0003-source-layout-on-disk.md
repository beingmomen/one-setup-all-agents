# 0003: Source layout on disk

Status: Accepted (MCP source stays JSONC, confirmed by the maintainer)

## Decision

The source of truth is a folder of plain files in the config directory (0002). It is separate from the tool's code: after `pipx install` there is no repository checkout, so nothing in the config may refer to the install location.

```text
<config-dir>/
  config.toml                 version = 1, which tools are enabled
  instructions.md             core instructions, shared by every tool
  instructions.<tool>.md      optional per-tool appendix (claude, codex, opencode, agy)
  mcp.jsonc                   tool-neutral MCP server definitions
  skills/<name>/SKILL.md      one folder per skill
```

- Instructions: each tool receives the core file, plus its own appendix if one exists. The planner checks the rendered size against the tool's limit (Antigravity has one) and reports an overflow instead of writing.
- MCP: neutral fields only (`command`, `args`, `url`, and the names of the environment variables the server needs). The adapter translates to each tool's format. Secrets are referenced by variable name; the file never holds a value.
- Skills: the source of truth for the skills the user wants shared (see 0007).
- `config.toml` carries `version = 1` so the format can change later; an unknown version is refused, not guessed.
- `all-agents init` creates this layout by importing what the tools already contain, after a full safety snapshot (0005).
- Reading all of this needs only the standard library: TOML via `tomllib`, JSONC via a small reader in the project.

## Reason

- Files the user can read, edit and keep under version control build trust; there is nothing hidden to migrate.
- A core file plus optional per-tool appendix covers "same rules everywhere, plus one tool-specific note" without templating.
- JSONC for MCP matches what users already hand-write for these tools (comments allowed), and the project needs a JSONC reader anyway to edit OpenCode's config.

## Open points

- MCP source format: JSONC (decided). TOML would have avoided a custom reader for the source but not for OpenCode's config.
- Whether `init` offers a "merge" when several tools have different global instructions (needs interactive choice design in Phase 4).
- Names of the per-tool appendix files if a fifth tool is added (the pattern `instructions.<id>.md` already covers it).
