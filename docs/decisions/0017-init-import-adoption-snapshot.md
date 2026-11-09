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

2. **Atomic file editing (`EditFile.mode` Option A).**
   - The engine implements atomic file modification with write-ahead safety copies and exact permission preservation.
   - File modes (Unix permissions) are read prior to modification and restored onto replacement files.

3. **Safety snapshot and the `snapshot_files` hook.**
   - Before modifying any tool configuration during initialization, the engine performs a preflight safety snapshot (snapshots happen during `init` only, not `sync`).
   - Tool adapters declare the exact paths they read and manage via the `snapshot_files(env, scope)` hook.
   - Snapshot entries are recorded in the journal operation as no-op entries (`snapshot (no change)`) capturing the byte-for-byte before-state. If an operation is reverted, files are restored to their exact pre-init state.
   - Missing files are skipped silently. Warnings are only emitted for broken symlinks or non-regular paths (directories).
   - If a snapshot backup fails due to concurrent modification or permission error (or a file changed while planning), `init` retries once by re-reading tool configurations and re-building the import proposal. In non-interactive mode or during snapshot retry, if the retried proposal differs from the confirmed proposal, `init` aborts with exit 1.

4. **Rules per configuration kind during import.**
   - **Instructions (`Kind.INSTRUCTIONS`):**
     - Normalization: Candidate instructions are normalized via `_normalize_instructions_text`, stripping BOM and CRLF and enforcing strictly one trailing newline (`\n`).
     - Validation: Files containing Unicode replacement characters (`U+FFFD`) or `NUL` bytes are rejected with a warning and omitted from proposal and adoption.
     - Disambiguation:
       - If instructions across all installed tools normalize identically, the shared text is written to `instructions.md`, and all tools are adopted.
       - If instructions differ across tools, `--from <tool>` selects the chosen tool's text. In interactive mode without `--from`, the user is prompted to choose by number, displaying short SHA prefixes (e.g. `[1] Claude Code (sha: a1b2c3d4)`) rather than full texts.
       - Matching files are adopted into the ledger. Divergent files are replaced only with explicit confirmation (or in `--yes` mode); otherwise, they remain untouched and unowned.
   - **MCP Servers (`Kind.MCP`):**
     - Unification: Servers from all detected tools are merged into `mcp.jsonc`.
     - Majority rule: If a server definition appears identically across multiple tools, the majority version is selected.
     - Tie resolution: If equal counts exist for differing definitions, ties are resolved deterministically by registered adapter order (`opencode`, `claude`, `codex`, `agy`).
     - Secret prevention and adoption exclusion (FIX-T1, FIX-T4):
       - Candidate servers are checked via `server_secret_problems`. A server containing literal environment secrets or literal header values is skipped whole (not imported into proposed servers, rather than having individual fields dropped), and `import_notes` are generated.
       - The advisory warning explicitly names the problematic field (e.g. `header 'Authorization' had a literal value...; reference an environment variable as ${NAME}`).
       - Any server with an `ImportNote` is excluded from adoption for that tool. It is left unowned in the ledger so that the next `all-agents sync` plan flags a `Conflict` instead of silently stripping the user's active credentials.
   - **Skills (`Kind.SKILLS`):**
     - Limits: Skill directories are guarded against resource exhaustion, enforcing limits of at most 5,000 files and 64 MiB total size per skill. Exceeding skills are skipped with a warning.
     - Copy vs. relink: Tools storing standalone skill directories (e.g. `~/.agents/skills/<name>`) have their folders copied to `skills/<name>/`. Tools using symlinks or directory junctions to shared skills have identical links adopted directly.

5. **Dry-run and conflict preflight.**
   - When `--dry-run` is passed, `init` detects tools, reads configurations, generates the import proposal, prints the proposal summary, and exits with code 0 without touching disk or creating journal operations.
   - Dry-run conflict checking takes candidate adopted items into account, avoiding false "unmanaged / left alone" warnings for files that will be adopted upon apply.

6. **Refusals and validations.**
   - Second-init refusal: If `ctx.env.config_dir / "config.toml"` already exists, `init` refuses with "already initialised; use `all-agents sync`" and exits with code 1.
   - In-memory validation: Proposed source files (`config.toml`, `mcp.jsonc`, `instructions.md`, appendices) are validated in memory using `load_source_texts` before applying any plan, with no temporary files written to disk.
   - Unknown `--from`: Passing an unrecognized tool ID exits with code 1 and lists valid installed tools.
   - Unreadable configs: Corrupted or invalid JSON/TOML files raise `ConfigUnreadable`; `init` reports masked warnings and refuses to touch the unreadable files (ADR 0016).
   - Non-interactive safety: In non-interactive environments without `--yes`, `init` refuses to modify files and exits with code 1.
   - Retry divergence: If a retried proposal differs from the confirmed proposal in non-interactive execution, `init` aborts and exits with code 1.
   - Unfinished operations: If an earlier engine operation was left uncommitted or locked, `init` refuses with code 1.

7. **Exit codes.**
   - `0`: Success, dry-run completion, or no supported tools found.
   - `1`: Refusal or error (already initialised, unknown tool, declined confirmation, engine locked, unfinished operation, or plan/apply failure).

## Reason

- Option A for adoption avoids synthetic external ops, keeping the plan model closed while ensuring full transactionality and reversibility.
- Strict secret hygiene and unowned retention ensure that credentials are never imported in plaintext, while preventing subsequent syncs from silently destroying user tokens.
- Explicit disambiguation rules (SHA prefixes, majority vote, tie-breaking order) guarantee predictable, repeatable initialization across diverse environments.

## Options considered

- **Adoption Carrier:**
  - **Option A (Carrier on `apply_plan`, Chosen):** Passed directly to engine, updating ledger and recording deltas within the journal transaction.
  - **Option B (Synthetic `Adopt` op in `Plan`):** Rejected because `Op` models external filesystem effects (`EditFile`, `Link`, `RunCli`). Ledger mutations are internal state.
  - **Option C (Out-of-band `journal.set_owned` call):** Rejected because unjournaled mutations cannot be audited or undone via `all-agents undo`.

- **Snapshot Mechanism:**
  - **Option A (`snapshot_files` adapter hook, Chosen):** Adapters declare the exact files they touch, enabling targeted, hermetic, and verifiable pre-modification backups.
  - **Option B (Blind recursive snapshot of home directory):** Rejected due to catastrophic performance, memory bloat, and potential capture of unrelated user data.
  - **Option C (Tool-specific external backup commands):** Rejected because external tool CLIs lack standardized backup formats and atomic rollback capabilities.

- **MCP Conflict Resolution:**
  - **Option A (Majority voting with deterministic tie-breaking, Chosen):** Unifies duplicate configurations with zero user interaction when a consensus exists, falling back to a fixed adapter sequence.
  - **Option B (Prompt for every conflicting server):** Rejected because initial import over multiple installed tools would lead to overwhelming prompt fatigue.

## Open points

1. **Project-level initialisation (v0.2):** Defining `all-agents init --project` workflows where local repository configurations (`.agents/`) layer over global source configs without overwriting user defaults.
2. **Interactive prompts in non-interactive CI environments:** Designing headless fallback modes for disambiguating diverging instructions when running in automated staging environments.
3. **Automated secret migration:** Exploring secure OS credential store integration (e.g. Windows Credential Manager, macOS Keychain, secret-service) to automatically migrate literal secrets to environment variables rather than requiring manual user intervention.
