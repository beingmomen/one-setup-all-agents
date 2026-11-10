# one-setup-all-agents

**One setup for all your AI coding agents.** A shared agent config for Claude Code, Codex, OpenCode and Antigravity: write your config once, every agent gets the same setup, and any change can be undone with one command.

> **Status: pre-alpha.** The four public commands (`init`, `sync`, `status`, `undo`) are implemented with full engine safety guarantees (atomic writes, journaled operations, backups, and undo). Real-world verification against live tool environments is in progress. Do not install it expecting a finished production release.

## Why

If you use more than one AI coding agent, you keep the same things in several places: your global instructions, your MCP servers, your skills. Each tool stores them in a different file with a different format, and they drift apart. Syncing them by hand is tedious, and a sync tool that rewrites your real config files is scary.

`one-setup-all-agents` is built around the second problem. It does not try to support the most tools. It tries to be the one you trust with your real config:

- **Write once.** One source config; every detected tool gets the same instructions, MCP servers and skills.
- **Safe by default.** `sync` shows a dry run first. Every write is atomic, backed up and recorded.
- **Undo with one command.** `all-agents undo` reverses the last change, from one journal that covers every kind of change.
- **Nothing to trust blindly.** Zero runtime dependencies (Python standard library only). Secrets are referenced by environment-variable name; their values are never read, printed or stored.

## Quick Start (5 minutes)

Try it in dry-run mode without installing:

```bash
uvx one-setup-all-agents init --dry-run
```

Or install with `pipx`:

```bash
pipx install one-setup-all-agents
```

### 1. Import your existing setup

Detect your installed tools and import existing instructions, MCP servers, and skills into the shared config:

```bash
all-agents init
```

Expected output:
```text
Detected tools:
  Claude Code (claude): installed (~/.claude)
  Codex (codex): installed (~/.codex)
Import proposal:
  - Instructions: imported
  - MCP servers (2): imported
  - Skills (1): imported
initialised operation 20261110T024601Z-71bf
the safety snapshot is kept permanently
```

### 2. Review and apply changes to all agents

Preview the planned changes across all tools, then confirm to apply:

```bash
all-agents sync
```

Expected output:
```text
Plan: 2 change(s), 0 conflict(s)
To run:
  1. edit file ~/.claude/CLAUDE.md: write the shared instructions
  2. edit file ~/.codex/AGENTS.md: write the shared instructions
Apply these changes? [y/N]: y
applied operation 20261110T024601Z-3b2f
```

### 3. Verify that all agents match

Check the status of every tool and kind of configuration:

```bash
all-agents status
```

Expected output:
```text
Claude Code (claude): in sync
  instructions: in sync
  mcp: in sync
  skills: in sync
Codex (codex): in sync
  instructions: in sync
  mcp: in sync
  skills: in sync
Undoable operations: 2
```

### 4. Reverse any change if needed

Undo the last operation with exact byte or CLI inversion from the journal:

```bash
all-agents undo
```

Expected output:
```text
undone operation 20261110T024601Z-3b2f (sync)
1 more can be undone
```

## Config Locations

Source configuration and engine state locations follow standard platform conventions (see [ADR 0002](docs/decisions/0002-config-and-state-locations.md)):

| OS | Shared Config (`config_dir`) | Engine State (`state_dir`) |
|---|---|---|
| Windows | `%APPDATA%/one-setup-all-agents` | `%LOCALAPPDATA%/one-setup-all-agents` |
| Linux | `$XDG_CONFIG_HOME/one-setup-all-agents` (or `~/.config/...`) | `$XDG_STATE_HOME/one-setup-all-agents` (or `~/.local/state/...`) |
| macOS | `~/.config/one-setup-all-agents` | `~/.local/state/one-setup-all-agents` |

Overrides: Setting `ALL_AGENTS_CONFIG_DIR` or `ALL_AGENTS_STATE_DIR` overrides the default paths on all operating systems.

## How Secrets Work

`one-setup-all-agents` operates under a zero-secret-exposure principle:

- **Variable References Only:** Secrets (API keys, tokens, auth headers) are referenced strictly by environment-variable name (e.g. `${GITHUB_TOKEN}` or `Authorization: Bearer <NAME>`).
- **No Secret Values:** Cleartext secret values are never requested, read from your environment, printed to stdout/stderr, written to disk, or stored in journals or backups.
- **CLI Guard:** If an adapter must run an external CLI (such as `claude mcp add-json`), arguments are scanned to ensure no secret values are passed on the process command line.

## Supported tools

| Tool | Adapter id | v0.1 (global scope) | Windows | Linux | macOS | Notes |
|---|---|---|---|---|---|---|
| Claude Code | `claude` | partial | [verified (2.1.319, 2026-11-10)](docs/verification/windows.md) | [fixtures only (CI)](docs/verification/linux.md) | [fixtures only (CI)](docs/verification/macos.md) | MCP is written through the tool's own CLI, because running sessions also write its config file. |
| Codex | `codex` | partial | [verified (0.167.0, 2026-11-10)](docs/verification/windows.md) | [fixtures only (CI)](docs/verification/linux.md) | [fixtures only (CI)](docs/verification/macos.md) | Only a managed region of its TOML config is edited. |
| OpenCode | `opencode` | partial | [verified (2.2.6, 2026-11-10)](docs/verification/windows.md) | [fixtures only (CI)](docs/verification/linux.md) | [fixtures only (CI)](docs/verification/macos.md) | First adapter to be ported (simplest). |
| Antigravity (`agy`) | `agy` | partial | [verified (1.5.1, 2026-11-10)](docs/verification/windows.md) | [fixtures only (CI)](docs/verification/linux.md) | [fixtures only (CI)](docs/verification/macos.md) | Does not follow Windows junctions, so each skill is listed in its `skills.json` manifest by the path of the source folder. Has no variable reference for secrets, so an http server with headers is not written. |

Windows verification was conducted live against the installed CLI binaries for each supported tool. Linux verification executed the full test suite in Ubuntu (WSL2), with tool binaries marked fixtures only as they were not installed in the WSL environment per the maintainer gate decision. macOS verification is performed on GitHub Actions hosted runners (`macos-latest`) against test fixtures without tool logins. See individual verification reports in [`docs/verification/`](docs/verification/) for details.

CI runs the tests on Windows, Linux and macOS.

## Safety Model

`one-setup-all-agents` is designed to be the tool you trust with your real developer environment:

- **Dry-run by default:** `sync` shows a detailed change plan before asking for confirmation.
- **Atomic file writes:** Config files are written using temporary siblings and atomic replacements; incomplete writes never corrupt your setup.
- **Private permissions:** State directories are created with `0700` and journal files with `0600` on POSIX systems.
- **Automatic backups:** Every modified file is backed up before any write occurs.
- **Unified journal:** All operations (file edits, links, CLI calls) are recorded with exact inverse actions in an append-only journal.
- **Drift detection:** `undo` checks that file contents match what was written. If you edited a file manually after running `sync`, `undo` refuses to overwrite your manual changes unless `--force` is specified.
- **What undo does not promise:** External edits made outside `all-agents` cannot be anticipated, and processes terminated abruptly during external CLI invocations must be inspected manually.

## Known Boundaries & Limits

- **Antigravity HTTP headers:** Antigravity does not support environment-variable expansion in HTTP headers; remote MCP servers requiring authorization headers are flagged as unsupported and skipped.
- **Windows Junctions:** Antigravity does not follow directory junctions; skills are referenced by direct directory paths in `skills.json`.
- **Codex Overrides:** If a non-empty `AGENTS.override.md` is present in Codex config, `all-agents` flags a conflict rather than silently writing inactive instructions.
- **Claude Code CLI:** Claude Code configuration is maintained through `claude mcp` CLI calls to avoid race conditions with running Claude sessions. Parameters not supported by the CLI (like `cwd`) are flagged as unsupported.

## Roadmap

- **v0.1**: The four public commands (`init`, `sync`, `status`, `undo`), global scope, for instructions, MCP servers, and skills.
- **v0.2**: Project scope, including safety and permissions for shared repositories you do not own.
- **Later**: Hooks, granular tool permissions, memory, and accounts.

## Development

```bash
pip install -e .
python -m unittest discover -s tests
```

Do not install it expecting stability while in pre-alpha.

Decisions are recorded in [docs/decisions](docs/decisions/). To add a tool, see [docs/adding-a-tool.md](docs/adding-a-tool.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Security policy and vulnerability disclosure are in [SECURITY.md](SECURITY.md). Rules for AI tools working in this repository are in [AGENTS.md](AGENTS.md).

## License

MIT. See [LICENSE](LICENSE).
