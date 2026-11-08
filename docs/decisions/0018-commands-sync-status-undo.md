# 0018: Commands v1 (sync, status, undo)

Status: Proposed

Phase 5 milestone M05 implements the core public commands that maintain configurations across tools: `all-agents sync`, `all-agents status`, and `all-agents undo`.

## Decision

### 1. The `sync` command pipeline

The `all-agents sync` command reconciles the shared source configuration with every detected and enabled tool. It follows a 13-step pipeline:

1. **Initialization check:** Refuse if `env.config_dir / "config.toml"` does not exist, warning the user to run `all-agents init` first (exit code 1).
2. **Source loading:** Load source configuration via `config_source.load_source`. If invalid (`SourceError`), display the error message and exit 1.
3. **Tool detection and filtering:**
   - Detect tools via `detect_all(env, versions=False)`.
   - Calculate candidate tools: intersection of installed tools and tools enabled in `config.toml`.
   - If `--tool <ID>` flags are provided, validate each tool ID against registered adapters (`ADAPTERS_BY_ID`). Unknown tool IDs cause a refusal listing valid tools (exit 1). Filter candidates to the specified tools.
   - If no candidate tools remain, report "nothing to sync" and exit 0.
4. **Detection warnings:** Print detection warnings of candidate tools (e.g. unmanaged directories).
5. **Kind filtering:** If `--only <KIND>` is provided, restrict planning to the specified kinds (`Kind.INSTRUCTIONS`, `Kind.MCP`, `Kind.SKILLS`). Otherwise plan all supported kinds.
6. **Plan generation:** Compute desired state via `desired_all(source_config)` (keeping candidate tool IDs) and build the plan with `build_plan(env, active_adapters, desired, kinds=kinds)`.
7. **Preview:** Render the complete plan to stdout using `render_plan(plan)`.
8. **Conflict inspection:** Check whether any item in `plan.items` is a `Conflict`.
9. **Executable check:** If `plan.executable` is empty:
   - If conflicts exist: print "Nothing to apply; conflicts exist." and exit 3.
   - If no conflicts: print "Nothing to do: all tools are in sync." and exit 0.
10. **Dry-run check:** If `--dry-run` is passed:
    - Print "Dry run: nothing was written."
    - Exit 3 if conflicts exist, otherwise exit 0.
11. **Approval gate:**
    - If `--yes` is not passed:
      - In non-interactive mode: print "Dry run only: not interactive; pass --yes to apply" and exit 0.
      - In interactive mode: prompt for confirmation ("Apply N change(s)?"). If declined, exit 0.
12. **Execution:**
    - Trigger journal pruning (placeholder for M05-T3.3).
    - Apply the plan via `apply_plan(env, plan, label="sync", run_cli=ctx.run_cli)`.
    - Handle `EngineLockedError`, `OpenOperationError`, `ApplyError`, `PlanError`: display error message and exit 1.
    - If successful, report the operation ID.
13. **Re-plan verification:**
    - Re-run `build_plan` on the same scope. If executable operations still remain, warn that changes are still pending.
    - If unresolved conflicts exist, exit 3. Otherwise exit 0.

### 2. Flags and CLI interface

- `--yes`: Proceed with application without interactive confirmation.
- `--dry-run`: Show preview of proposed changes and exit without modifying anything.
- `--tool <ID>` (repeatable): Limit operations to one or more tool identifiers.
- `--only <KIND>` (repeatable): Limit operations to specific configuration kinds (`instructions`, `mcp`, `skills`).

### 3. Exit codes

- **`all-agents sync`:**
  - `0`: Success, no-op (already in sync), dry-run without conflicts, or user-declined confirmation.
  - `1`: Refusal, error, lock contention, unfinished operation, invalid flag, or failed apply.
  - `3`: Unresolved conflicts remain in the plan (both during dry-run and after apply).
- **`all-agents status`:**
  - `0`: In sync (no pending operations or differences).
  - `1`: Error (corrupted configs, unreadable files).
  - `2`: Differences or conflicts exist.
- **`all-agents undo`:**
  - `0`: Successful revert.
  - `1`: Refusal, error, or unforced drift detected.

### 4. Output shape and secret safety

- All operations format descriptions cleanly and mask sensitive paths and secrets.
- Plan rendering groups operations logically, distinguishing created, modified, relinked, and conflicting items.
- Secret values never appear in preview output, journal summaries, or command logs.

### 5. Status JSON schema (Milestone M05-T2 preview)

`all-agents status --json` emits structured JSON containing:
- `tools`: map of tool ID to tool status (`status`, `installed`, `binary`, `version`, `config_roots`).
- `kinds`: per-tool status for instructions, mcp, and skills.
- `in_sync`: boolean flag indicating whether all tools match shared source.
- `conflicts`: list of active conflicts.

### 6. Undo semantics and retention (Milestone M05-T3 preview)

`all-agents undo` reverses operations recorded in `state/ops/` using the write-ahead journal:
- Reverts operations in reverse chronological order.
- Detects unmanaged file drift before reverting, refusing unless `--force` is supplied.
- Journal retention preserves up to 20 past operations, pruning older operations safely.

## Reason

- Explicit preview and interactive confirmation ensure users always know what changes will be applied to tool configuration files before they happen.
- Distinct exit codes (specifically code 3 for unresolved conflicts) enable scripting and CI integration without masking hard errors.
- Re-planning immediately after apply provides verification that changes took effect as intended.

## Options considered

- **Option A (Exit code 1 for conflicts):** Rejected because distinguishing environmental errors (locks, bad syntax) from deliberate user conflicts (unowned external files) is essential for automated workflows.
- **Option B (Automatic apply in non-interactive mode):** Rejected for safety; non-interactive executions must require explicit `--yes` to prevent accidental overwrites in automated scripts.

## Open points

None.
