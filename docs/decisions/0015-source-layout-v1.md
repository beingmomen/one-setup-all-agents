# 0015: Source layout v1

Status: Proposed

ADR 0003 established the disk locations and file names of the shared source folder (`config.toml`, `instructions.md`, `instructions.<tool>.md`, `mcp.jsonc`, `skills/<name>/SKILL.md`), but left the concrete schemas, per-tool tables, rendering semantics, and loader/writer behaviors to be decided. This ADR defines source layout v1 for the new engine and loader.

## Decision

### 1. Name of the shared config file (Decided by maintainer, 2026-11-08)

The shared config file retains the name `config.toml`, exactly as specified in ADR 0003. It is not renamed to satisfy boundary tests. Instead, the boundary guard in `tests/test_tool_boundary.py` was refined from banning the bare string `config.toml` to banning tool-specific paths (such as `.codex/config.toml`, `.codex\config.toml`, and `.codex`), permitting the generic filename in non-adapter modules while still prohibiting tool-specific config paths outside adapters. The name is maintained as a single constant `SOURCE_CONFIG_NAME = "config.toml"` in `config_source.py`.

### 2. `mcp.jsonc` schema

The JSONC schema uses top-level versioning:
```jsonc
{
  "version": 1,
  "servers": {
    "server-name": {
      "command": "cmd",
      "args": ["arg1"],
      "cwd": "path",
      "url": "https://example.test/mcp",
      "env": { "PUBLIC_KEY": "literal_value" },
      "env_vars": ["SECRET_TOKEN"],
      "headers": { "Authorization": "Bearer ${SECRET_TOKEN}" },
      "startup_timeout_sec": 30,
      "auth": "oauth",
      "enabled": true,
      "tools": ["tool_id"],
      "description": "Optional description"
    }
  }
}
```
- Transport: Determined by presence of `url` (`Transport.HTTP` / `Transport.SSE`). If `url` is present, `command`, `args`, and `cwd` are forbidden. If `url` is absent, `command` is required.
- Variable reference syntax: Standard `${VAR}` syntax (consistent with ADR 0009 and standard tool conventions). Legacy `{{VAR}}` syntax is rejected.
- Secret prevention: Values in `command`, `url`, and `args` must pass masking checks (`mask_text(v) == v`). Non-secret literals in `env` must not resemble secrets (`env_value_looks_secret`). Headers must reference environment variables via `${VAR}` or `<Scheme> ${VAR}`.
- Refusal: Any unknown top-level key or server field raises `SourceError`. Legacy fields (`retired`, `unmanaged`, `keep_live_env`, `vars`) are explicitly rejected.

### 3. Per-tool settings table in `config.toml`

Per-tool configuration is specified under `[tools.<id>]` sections, where `<id>` corresponds to a registered adapter ID:
```toml
version = 1

[tools.example_tool]
enabled = true
skills_exclude = ["skill-a", "skill-b"]

[projects]
# Reserved for v0.2, accepted and ignored in v0.1
```
- Tool independence: The loader does not hardcode tool names; it accepts `[tools.<id>]` based on the injected list of registered adapter IDs. Unknown tool IDs raise `SourceError`.
- `enabled`: Boolean (defaults to `true`). Disabled tools are omitted from planning.
- `skills_exclude`: List of skill names to exclude for that tool.
- Top-level `version = 1` is required. Unknown keys raise `SourceError`.

### 4. Instructions rendering

- Format: If `instructions.md` exists, rendered text is `core.rstrip("\n") + "\n"`. If an appendix `instructions.<tool_id>.md` exists for that tool, it is appended as `"\n" + appendix.rstrip("\n") + "\n"`.
- Header: No generated banner/header is prepended, ensuring exact match with existing unmodified tool instruction files and enabling clean adoption.
- Encoding: UTF-8, LF line endings, BOM stripped. Absent `instructions.md` yields `None` (instructions not managed).
- Unicode warnings: Bidi control characters trigger a warning, not an error.

### 5. Line endings of new source files

All newly written source files use LF (`\n`) across all operating systems.

## Reason

- Standardizing on `${VAR}` aligns `mcp.jsonc` with ADR 0009 and avoids proprietary macro preprocessing.
- Structural validation with immediate rejection of unknown keys prevents silent misconfiguration.
- Excluding generated headers ensures idempotency and clean adoption of user files.

## Options considered

- **Supporting legacy `_core/source.py` syntax (`{{VAR}}`, `retired`, `unmanaged`)**: Rejected because legacy conventions introduce ambiguity and unnecessary complexity into v0.1.
- **Top-level tool exclusion lists (`claude_exclude`, `agy_exclude`)**: Rejected because it hardcodes tool names into the config format rather than using generic `[tools.<id>]` tables.
- **Adding a "# Generated" banner to rendered instructions**: Rejected because it causes immediate content divergence when adopting existing instructions.

## Open points

None. All choices are encapsulated in `config_source.py` and covered by unit tests.
