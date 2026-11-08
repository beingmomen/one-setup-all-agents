# 0017: Init, import, adoption, and safety snapshots

Status: Proposed

Phase 4 milestone M04 introduces the public `all-agents init` command. Its purpose is to detect installed tools, import existing configurations into the unified source configuration layout (`config.toml`, `mcp.jsonc`, `instructions.md`, `skills/`), adopt matching configurations into the engine ledger, and ensure safety guarantees through snapshots and dry-run reviews.

## Decision

1. **Adoption carrier (Option A).**
   - The engine's `apply_plan` accepts an optional parameter: `adopt: Sequence[ItemRef] = ()`.
   - When `adopt` is passed, `_Run` incorporates the adopted items into its working owned set.
   - If `plan.executable` is empty but `adopt` contains items, `apply_plan` executes the operation: opening a journal operation, recording the ownership delta in `owned_delta`, updating the ledger via `journal.replace_owned`, and marking the operation `done`.
   - On failure of any op in a mixed plan (executable ops plus adoption), the automatic revert ensures the uncommitted adoption never takes effect in the ledger, leaving the ledger untouched.
   - If an adoption operation is reverted later via `revert_operation`, `journal.revert_owned` removes only the items added in `owned_delta`, preserving items previously owned.
   - This keeps the closed `Op` hierarchy intact (`EditFile`, `Link`, `RunCli`), avoiding synthetic "Adopt" ops while providing write-ahead journaling and full undoability for adopted items.

2. **Rules per configuration kind during import.**
   - **Instructions (`Kind.INSTRUCTIONS`):**
     If instructions are imported (e.g. from an existing tool), they form the initial shared `instructions.md` (`.core`). If tool instructions match the imported core, they can be adopted into the ledger rather than rewritten.
   - **MCP Servers (`Kind.MCP`):**
     Servers imported across tools are unified into `mcp.jsonc`. Matching servers are scoped to all tools or specific tools via `tools = [...]`. Servers already present and identical in tool configs are marked for adoption into the ledger.
   - **Skills (`Kind.SKILLS`):**
     Existing user skills are imported into the source `skills/` directory. Identical links or shared directories are adopted into the ledger.

3. **Safety snapshot and dry-run design.**
   - Safety snapshot: The engine never edits files in place without a write-ahead backup (`backup.capture`) fsynced to disk in the operation directory before any target file is modified.
   - Snapshot retention: the snapshot is kept as long as its operation is kept (last 20 operations); pinned retention is a decision in M05.
   - Initialisation respects `--dry-run`: when `--dry-run` is passed, `init` detects tools, reads configurations, generates the import proposal, prints the proposal summary, and exits with code 0 without modifying the file system or creating journal entries.
   - Non-interactive safety: In non-interactive environments without `--yes`, `init` refuses to proceed and exits with code 1.

4. **Refusals and validations.**
   - Already initialised: If `ctx.env.config_dir / "config.toml"` already exists, `init` refuses to run with a warning and exits with code 1, directing the user to `all-agents sync`.
   - Unknown `--from`: If `--from <TOOL>` specifies an unknown tool identifier not registered in `ADAPTERS`, `init` warns and exits with code 1.
   - Unreadable configs: If a tool configuration is corrupted or unreadable (`ConfigUnreadable`), `reader.read_all` records a masked problem. `init` prints the problem warning and refuses to overwrite the corrupted file (ADR 0016).
   - Unfinished operations: If an earlier engine operation was left open (e.g. after a crash), `init` refuses with `OpenOperationError` and exits with code 1.

5. **Exit codes.**
   - `0`: Success, dry-run completion, or no supported tool installed ("No supported tool was found. Nothing to import.").
   - `1`: Refusal or error (already initialised, unknown tool, declined confirmation, engine locked, unfinished operation, or plan/apply failure).

## Reason

- Option A (Adoption carrier via `apply_plan`) fits naturally into the existing engine lifecycle (ADR 0005, 0008, 0010) without introducing synthetic ops or out-of-band ledger mutations.
- By utilizing the engine's atomic writes and write-ahead journal for creating source configs and recording adopted items, the entire initialisation process is verifiable and undoable.

## Options considered

- **Option B (Synthetic `Adopt` op in `Plan`):** Rejected because `Op` represents external side effects (`EditFile`, `Link`, `RunCli`). Introducing an internal ledger-only op breaks the clean separation between external plan ops and internal engine state.
- **Option C (Out-of-band `journal.set_owned` call):** Rejected because mutating the ledger directly without a journal operation cannot be tracked, audited, or undone via `all-agents undo`.

## Open points

None.
