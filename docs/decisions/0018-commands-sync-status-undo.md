# 0018: Commands v1 (sync, status, undo)

Status: Proposed

Phase 5 implements the core public commands that maintain configurations across tools: `all-agents sync`, `all-agents status`, and `all-agents undo`.

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
    - Apply the plan via `apply_plan(env, plan, label="sync", run_cli=ctx.run_cli)`.
    - Handle `EngineLockedError`, `OpenOperationError`, `ApplyError`, `PlanError`: display error message and exit 1.
    - If successful, trigger journal retention pruning and report the operation ID.
13. **Re-plan verification:**
    - Re-run `build_plan` on the same scope. If executable operations still remain, warn that changes are still pending.
    - If unresolved conflicts exist, exit 3. Otherwise exit 0.

### 2. Flags and CLI interface

- **`sync`:**
  - `--yes`: Proceed with application without interactive confirmation.
  - `--dry-run`: Show preview of proposed changes and exit without modifying anything.
  - `--tool <ID>` (repeatable): Limit operations to one or more tool identifiers.
  - `--only <KIND>` (repeatable): Limit operations to specific configuration kinds (`instructions`, `mcp`, `skills`).
- **`status`:**
  - `--json`: Output machine-readable JSON status schema.
  - `--versions`: Probe installed tool executables to determine exact version strings.
- **`undo`:**
  - `--list`: List operations that can be undone.
  - `--force`: Force revert even if unmanaged file drift is detected.

### 3. Exit codes

- **General (argparse):**
  - `2`: Usage error (unrecognized command or invalid arguments parsed by argparse).
- **`all-agents init`:**
  - `0`: Success, dry-run completion, or no supported tools found.
  - `1`: Refusal or error (already initialised, unknown tool, declined confirmation, engine locked, unfinished operation, or plan/apply failure).
- **`all-agents sync`:**
  - `0`: Success, no-op (already in sync), dry-run without conflicts, or user-declined confirmation.
  - `1`: Refusal, error, lock contention, unfinished operation, invalid flag, or failed apply.
  - `3`: Unresolved conflicts remain in the plan (both during dry-run and after apply).
- **`all-agents status`:**
  - `0`: In sync (no pending operations or differences).
  - `1`: Error (uninitialised, corrupted source config / syntax error, or unreadable ledger / state error).
  - `2`: Differences, conflicts, or unreadable tool configuration files exist.
- **`all-agents undo`:**
  - `0`: Successful revert or nothing to undo.
  - `1`: Refusal, error, unforced drift detected, or inverse command was skipped during `undo --force`.

### 4. Output shape and secret safety

- All operations format descriptions cleanly and mask sensitive paths and secrets.
- Plan rendering groups operations logically, distinguishing created, modified, relinked, and conflicting items.
- Secret values never appear in preview output, journal summaries, or command logs.
- `build_plan` already turns any `RunCli` op whose argv or inverse argv changes under `mask_args` into a `Conflict`, so `sync` reports it as an unmanaged conflict (exit 3 in dry run, per §3), and `apply_plan` is the last-line guard against secret-bearing `RunCli` operations (leaving the caller's `Plan` object unmutated).

### 5. Status JSON schema

`all-agents status --json` emits structured JSON adhering to format version 1:

1. **Uninitialised state (exit 1):**
```json
{
  "schema": 1,
  "initialised": false,
  "tools": [],
  "undoable_operations": 0
}
```

2. **Source configuration error (exit 1):**
```json
{
  "schema": 1,
  "error": "config.toml: [tools.unknown] is not a registered tool"
}
```

3. **Active configuration state (exit 0 if in sync, exit 2 if differences/conflicts):**
```json
{
  "schema": 1,
  "initialised": true,
  "tools": [
    {
      "id": "claude",
      "label": "Claude Code",
      "state": "in sync",
      "version": "1.0.0",
      "binary": "/usr/local/bin/claude",
      "warnings": [],
      "kinds": {
        "instructions": {
          "state": "in sync",
          "items": [],
          "reasons": []
        },
        "mcp": {
          "state": "differs",
          "items": ["server1"],
          "reasons": []
        },
        "skills": {
          "state": "in sync",
          "items": [],
          "reasons": []
        }
      }
    }
  ],
  "undoable_operations": 3
}
```
- Tool states: `"not installed"`, `"disabled"`, `"unreadable"`, `"conflict"`, `"differs"`, `"in sync"`.
- Kind states: `"not installed"`, `"disabled"`, `"unreadable"`, `"conflict"`, `"differs"`, `"unsupported"`, `"in sync"`.
- Paths in `binary` and `warnings` mask user home directories (`~`) and credentials.

### 6. Undo semantics, additive journal fields, and safety copies

- **Undo as `kind: "undo"`:** An undo operation executes an inverse plan that reverts a target operation. It opens a journal operation with `kind: "undo"` and `undoes: <target_op_id>`.
- **Safety copies and no redo:** Before modifying or unlinking files during an undo, write-ahead safety copies are created in the undo operation directory. Reverting an operation is irreversible in the forward direction: there is no `redo` command.
- **Additive journal fields:** Operations in `state/ops/<op_id>/journal.json` and the ledger in `state/ledger.json` introduce three additive optional fields:
  - `kind`: `"apply"` or `"undo"` (defaults to `"apply"` for legacy entries).
  - `undoes`: string ID of the operation being reverted (only present when `kind == "undo"`).
  - `pinned`: boolean indicating whether the operation is immune to retention pruning (defaults to `false`; `true` for `init` safety snapshots).
  Old journals without these fields load transparently without migrations; format version remains 1.
- **Amendment to ADR 0005:** ADR 0005 stated that "every operation can be undone". This is clarified: only apply operations (`kind: "apply"`) can be undone; undo operations (`kind: "undo"`) and operations with unforced external file drift cannot be undone.

### 7. Stack ordering, recovery, force, and retention policy

- **Monotonic sequence ordering:** Operations in the journal are strictly ordered by sequential ledger append order, ensuring newest-first resolution even when multiple operations occur within the same second.
- **Interrupted undo recovery rules:**
  - If an undo operation is interrupted (Ctrl+C, crash, or step failure), its open entry records `after` states equal to the target's `before` states.
  - During subsequent recovery, `recover_locked` checks the state of the target operation:
    - If the target operation is already undone or all its entries have been reverted: mark the target operation `undone`, mark the undo operation `done`, and clear `pending_revert` on both operations.
    - Otherwise: resume reverting the target operation (`revert_locked`), then mark the undo operation `done` and clear `pending_revert`.
- **`--force` semantics:**
  - When `--force` is supplied, `recover_locked` re-captures fresh safety copies of current file states for any un-reverted entries into the undo operation before reverting.
  - Permanently failing inverse CLI commands are skipped under `--force`: the entry is marked `revert_skipped: true`, recorded in `skipped_entries` with an explanatory note, and the target operation is marked `undone` (or `failed` if an unrecoverable failure occurs).
- **Retention pruning:**
  - Automatic trigger: Pruning runs automatically after successful `apply` and `undo` operations.
  - Count threshold: Keep at most 20 unpinned `done` apply operations (`keep=20`).
  - Initial snapshot pinning: The initial `init` snapshot operation is permanently pinned (`pinned: true`), is never pruned, and does not count toward the retention limit of 20.
  - Terminal operations cap: Non-`done` terminal operations (`undone`, `undo`, and `failed`) are capped at the 20 newest (older ones dropped).
  - Active revert protection: Any operation still referenced by a pending revert or open undo is never pruned.
  - Error visibility: Pruning errors or warnings (e.g. unmanaged files in backup folders) are surfaced to stderr, never swallowed silently.

## Reason

- Clear, structured JSON output enables robust tooling and CI automation without parsing terminal color codes.
- Additive journal schema evolution preserves 100% backward compatibility with pre-existing engine states.
- Pinning the initial snapshot guarantees user safety: even after dozens of sync iterations, the user can always restore their exact original tool configuration.

## Options considered

- **Exit code for conflicts:**
  - **Option A (Exit code 3, Chosen):** Explicit exit code distinguishing environmental errors (1) from intentional unmanaged user configurations (3).
  - **Option B (Exit code 1 for conflicts):** Rejected because CI pipelines cannot distinguish syntax/IO failures from unowned file conflicts.
- **Undo architecture:**
  - **Option A (One-way undo stack with pinned init snapshot, Chosen):** Provides clean, predictable rollbacks with zero risk of infinite undo/redo tree loops.
  - **Option B (Bidirectional undo/redo DAG):** Rejected because maintaining redo state across arbitrary file edits outside the tool introduces severe complexity and data corruption risks.
- **Journal schema migration:**
  - **Option A (Additive optional fields, Chosen):** Kept format version 1 with fallback defaults for `kind` and `undoes`.
  - **Option B (Format version bump to 2 with migration script):** Rejected because additive fields require no migration and maintain total backward compatibility.

## Open points

1. **Selective non-head undo:** Assessing whether `all-agents undo <OP_ID>` should be supported for arbitrary past operations when file change sets do not intersect.
2. **Interactive visual diff in `status`:** Evaluating an optional `--diff` flag for `all-agents status` to print side-by-side unified diffs of divergent instructions and MCP server configs.
3. **Configurable retention limit:** Evaluating whether `keep=20` should be configurable via `config.toml` for storage-constrained environments.
4. **Known false positive on long names:** A single opaque token of 32+ characters with letters and digits is treated as a secret by the argument masking guard; users are advised to shorten or split the argument or move it to an environment variable.
