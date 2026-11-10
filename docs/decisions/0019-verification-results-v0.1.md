# 0019: Verification results v0.1

Status: Proposed

This ADR records the empirical verification results obtained during Phase 6 (M06) trial against live tool binaries across Windows, Linux (WSL), and macOS CI runners. It settles or formally leaves unverified the open points in ADRs 0007, 0011, 0012, 0013, and 0014 without modifying the text of those accepted ADRs.

## Context

Phase 6 executed a 3-tier trial:
- **Windows (Maintainer workstation):** Live trial against installed binaries (Claude Code 2.1.319, Codex 0.167.0, OpenCode 2.2.6, Antigravity 1.5.1) using L0 (unit tests), L1 (read-only real detection dry-run with SHA-256 before/after verification), and L2 (isolated sandbox execution with child process environment overrides). Full results in `docs/verification/windows.md`.
- **Linux (Ubuntu 24.04 WSL2):** Full test suite execution on native Linux filesystem (`~/osaa-copy`). Per maintainer gate decision, tool binaries were not installed in WSL. Results in `docs/verification/linux.md`.
- **macOS (GitHub Actions `macos-latest` runner):** CI suite execution against test fixtures (Run ID 42667223877). Per ADR 0007, maintainer has no macOS hardware, so real tools cannot be run live. Results in `docs/verification/macos.md`.

## Resolution of Open Points by ADR

### 1. ADR 0007: Skills distribution per OS

| Open Point | Status | Evidence / Notes |
|---|---|---|
| Directory junctions without admin rights on Windows | **Resolved** | Verified in **V04**. Junctions created and resolved across all adapters without elevation. |
| Symlink traversal by tools on Linux | **Unverified (Live tools)** | Linux test suite verifies native symlink creation/traversal (**P02**), but live tool binaries were not installed in WSL. |
| Symlink traversal by tools on macOS | **Unverified (Live tools)** | Contract and unit tests pass against fixtures on CI (**M01**, **M02**); real tools not run. |
| Antigravity symlink vs manifest resolution | **Resolved** | Refined by ADR 0013 and verified in **V04** / **V14**. Antigravity uses direct folder entries in `skills.json` instead of directory links. |
| Windows deep path filesystem limits | **Unverified** | Standard path lengths tested; MAX_PATH limits with deep nesting unverified. |

### 2. ADR 0011: Claude Code adapter

| Open Point | Status | Evidence / Notes |
|---|---|---|
| `CLAUDE_CONFIG_DIR` relocation | **Resolved (Windows)** | Verified in **V02**. Environment variable correctly directs `.claude.json` and `CLAUDE.md`. Unverified on Linux/macOS. |
| MCP CLI integration (`add-json` and `remove -s user`) | **Resolved (Windows)** | Verified in **V08**. Real CLI mutates `.claude.json`, journal captures mutations, and `undo` successfully invokes inverse CLI command. |
| Windows launchers plain names without `.cmd` | **Resolved (Windows)** | Verified in **V01** & **V08**. Claude Code CLI executes direct PATH binaries. |
| Secret refusal heuristics | **Resolved** | Tested via unit and contract test suites. Heuristics safely guard cleartext secrets from reaching CLI argv. |
| CLI execution without drift checks | **Accepted** | Existing design rationale confirmed: CLI handles internal concurrency of `.claude.json`. |

### 3. ADR 0012: Codex adapter and shared links

| Open Point | Status | Evidence / Notes |
|---|---|---|
| `AGENTS.override.md` conflict detection | **Resolved (Windows)** | Verified in **V07**. Adapter correctly detects non-empty override and flags `Conflict`. |
| In-place TOML editing in managed region of `config.toml` | **Resolved (Windows)** | Verified in **V09**. Clean insertion, update, and removal inside managed section with byte restoration on undo (**V11**). |
| Codex reading stdio servers | **Resolved (Windows)** | Verified in **V09**. `codex mcp list` reports stdio server with status `enabled`. |
| Windows `.cmd` suffix requirement for launchers | **Resolved (Windows)** | Suffix preserved per ADR 0012; verified working in **V01** detection. |
| Linux and macOS live tool execution | **Unverified** | Fixtures pass in CI; live execution unverified. |

### 4. ADR 0013: Antigravity adapter

| Open Point | Status | Evidence / Notes |
|---|---|---|
| Shared `mcp_config.json` reading | **Resolved (Windows)** | Verified in **V05** & **V09**. Real `agy mcp list` reads stdio servers. |
| Manifest `skills.json` direct path entries | **Resolved (Windows)** | Verified in **V04** & **V14**. Avoids junction traversal limitations in Antigravity. |
| 24,000-byte instructions limit guard | **Resolved** | Verified in **V10**. File size check prevents silent truncation. |
| Shared manifest between App and CLI surfaces | **Resolved (Windows)** | Verified in **V14**. Both CLI and Desktop App share `~/.gemini/config/mcp_config.json`. |
| `env_vars` inheritance live check | **Partial** | Stdio server environment verified in **V05**. macOS GUI dock launch inheritance remains unverified. |
| Linux and macOS live tool execution | **Unverified** | Fixtures pass in CI; live execution unverified. |

### 5. ADR 0014: Tool detection and version probes

| Open Point | Status | Evidence / Notes |
|---|---|---|
| Version probe output parsing | **Resolved (Windows)** | Verified in **V01** for all four tools: Claude Code (`2.1.319`), Codex (`0.167.0`), Antigravity (`1.5.1`), and OpenCode (`2.2.6`). |
| OpenCode startup timeout key | **Resolved (Windows)** | Verified in **V06**. Decompiled binary schema inspection confirmed `mcp.timeout.startup` is natively supported under `mcp.timeout`. |
| Windows app execution aliases / store stubs | **Resolved (Windows)** | Verified in **V13**. All detected tools reside on standard paths without triggering Windows Store stubs. |
| Probe timeout (8.0s) | **Resolved** | All four probes complete in < 1.5s cold on Windows. |
| Linux and macOS tool version probes | **Unverified (Live tools)** | Probing logic passes CI tests against mocks; live probing unverified on those platforms. |

## Consequences

- The `README.md` Support Table reflects exact empirical status: all 4 tools are marked `verified` on Windows with their measured version numbers, and `fixtures only (CI)` on Linux and macOS.
- Global scope support for v0.1 remains `partial` for all tools because multi-OS verification requires both Windows and Linux to be verified live.
- Automated tests (`test_docs_support_table.py`) guard against documentation drift, verifying that any `verified` claim in the README links to a valid report with matching evidence rows.
