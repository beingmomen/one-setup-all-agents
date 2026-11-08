# 0016: Unknown formats are refused

Status: Proposed

The third plan item of Phase 4 is "reject unknown config formats instead of overwriting them". This ADR settles how unknown formats are refused across the four tools and within the shared source configuration.

## Decision

1. **Refusal is structural, not version-gated.**
   A tool's configuration is refused when its content cannot be safely parsed or is structurally malformed (e.g. invalid JSON/JSONC, non-object root table, invalid entries shape, or corrupt TOML). The engine and adapters do not maintain an allowed-version window table to refuse a tool binary: tool version numbers are detected and displayed in `init` and `status` reports, but execution is never blocked on an unknown version string alone.

2. **The shared source file `version` is strictly gated.**
   Both `config.toml` and `mcp.jsonc` require `version = 1`. Any missing or differing version raises `SourceError` and immediately aborts loading.

3. **Unified exception hierarchy for unreadable configs.**
   All tool adapters share one base exception class, `one_setup_all_agents.adapters.base.ConfigUnreadable(ValueError)`.
   Adapter-specific manifest exceptions (such as `antigravity.ManifestUnreadable`) inherit from `ConfigUnreadable`.
   When an adapter encounters an unreadable configuration file during `read_mcp` or `read_skills`, it raises `ConfigUnreadable`. During planning (`plan_mcp`), unreadable files produce `Conflict` operations instead of `EditFile`/`RunCli` ops, ensuring the engine never writes to or overwrites an unreadable config file.

4. **Fault-tolerant inspection (`reader.py`).**
   The reader module catches `ConfigUnreadable` on a per-kind basis (`Kind.INSTRUCTIONS`, `Kind.MCP`, `Kind.SKILLS`). A failure in one kind yields an empty result and records a `Problem(tool, kind, path, message)` record, while unaffected kinds are still read normally. Unexpected exceptions (anything other than `ConfigUnreadable`) are not swallowed and propagate immediately. Problem messages are sanitized through `mask_text` to prevent secret leaks.

## Reason

- Tool release cadences and subtle patch versions vary across operating systems. Maintaining a rigid compatibility matrix of allowed tool versions would cause frequent false-positive rejections for users with newer patch updates.
- Refusing structurally malformed files and converting planning into conflicts guarantees that existing corrupted or foreign configs are never destroyed or overwritten.

## Options considered

- **Table of tested versions per adapter:** Rejected because tool versions evolve frequently upstream. Gating by version window introduces high maintenance overhead and false rejections, whereas structural validation directly checks what the adapter actually requires.
- **Swallowing generic `ValueError` in `read_tool`:** Rejected because broad exception catching can conceal bugs or unexpected internal failures. Catching only `ConfigUnreadable` ensures precise error handling.

## Open points

None.
