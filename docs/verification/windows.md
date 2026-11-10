# Windows Verification Results

Tested on: Windows 10/11 Enterprise/Pro (build 10.0.26200.0), PowerShell 5.1, Python 3.14.5 / 3.13.17.
Tested date: 2026-11-10.
Safety Level: L0 (unit tests), L1 (read-only real detection dry-run), L2 (isolated sandbox execution with real tool CLIs).

## Summary Table

| Id | Check | Tool version | OS version | Command (masked) | Expected | Observed | Verdict | Date |
|---|---|---|---|---|---|---|---|---|
| **V01** | Version probe output parsing | Claude Code 2.1.319 | Windows 10.0.26200 | `claude.exe --version` | `2.1.319` | `2.1.319 (Claude Code)` -> parsed `2.1.319` | verified | 2026-11-10 |
| **V01** | Version probe output parsing | Codex 0.167.0 | Windows 10.0.26200 | `codex.exe --version` | `0.167.0` | `codex-cli 0.167.0` -> parsed `0.167.0` | verified | 2026-11-10 |
| **V01** | Version probe output parsing | Antigravity 1.5.1 | Windows 10.0.26200 | `agy.exe --version` | `1.5.1` | `1.5.1` -> parsed `1.5.1` | verified | 2026-11-10 |
| **V01** | Version probe output parsing | OpenCode 2.2.6 | Windows 10.0.26200 | `opencode.cmd --version` | `2.2.6` | `opencode v2.2.6` -> parsed `2.2.6` | verified | 2026-11-10 |
| **V02** | `CLAUDE_CONFIG_DIR` relocates files | Claude Code 2.1.319 | Windows 10.0.26200 | `claude.exe mcp list` | Relocates `.claude.json` | `.claude.json` read/written in target dir | verified | 2026-11-10 |
| **V03** | Windows launchers / `.cmd` shims | OpenCode 2.2.6 | Windows 10.0.26200 | `opencode.cmd` | Finds desktop CLI | Dispatches to `opencode-cli.exe` | verified | 2026-11-10 |
| **V04** | Skills visibility via links | All tools | Windows 10.0.26200 | `all-agents sync` | Directory junctions | Junctions created and resolved | verified | 2026-11-10 |
| **V05** | Antigravity env_vars / MCP read | Antigravity 1.5.1 | Windows 10.0.26200 | `agy.exe mcp list` | Shows configured MCP servers | Lists stdio servers from `mcp_config.json` | verified | 2026-11-10 |
| **V06** | OpenCode startup timeout key | OpenCode 2.2.6 | Windows 10.0.26200 | Binary inspection | `mcp.timeout.startup` supported | Confirmed `mcp.timeout.startup` in binary schema | verified | 2026-11-10 |
| **V07** | Codex `AGENTS.override.md` conflict | Codex 0.167.0 | Windows 10.0.26200 | `all-agents status` | Conflict on override | Detects `AGENTS.override.md`, reports conflict | verified | 2026-11-10 |
| **V08** | Claude Code `mcp add-json` and `remove` | Claude Code 2.1.319 | Windows 10.0.26200 | `claude.exe mcp add-json / remove` | Modifies `.claude.json` | `sync` adds server, `undo` removes server | verified | 2026-11-10 |
| **V09** | Codex reads MCP config | Codex 0.167.0 | Windows 10.0.26200 | `codex.exe mcp list` | Lists servers from `config.toml` | Lists stdio server with status `enabled` | verified | 2026-11-10 |
| **V09** | Antigravity reads MCP config | Antigravity 1.5.1 | Windows 10.0.26200 | `agy.exe mcp list` | Lists servers from `mcp_config.json` | Lists stdio server with status `enabled` | verified | 2026-11-10 |
| **V10** | Antigravity 24,000-byte limit | Antigravity 1.5.1 | Windows 10.0.26200 | Adapter contract | Warns/conflicts > 24KB | Guard verified | verified | 2026-11-10 |
| **V11** | Undo restores exact bytes | All tools | Windows 10.0.26200 | `all-agents undo` | Reverts modified files | Reverts sync changes; drift refusal on edits | verified | 2026-11-10 |
| **V12** | Unmanaged skills & extra instructions | OpenCode / Antigravity | Windows 10.0.26200 | `all-agents init / status` | Warnings on extra files | OpenCode unmanaged skills warning printed | verified | 2026-11-10 |
| **V13** | App execution aliases safe | All tools | Windows 10.0.26200 | Version probe execution | No side-effects | All binaries are direct PATH binaries | verified | 2026-11-10 |
| **V14** | Shared manifest for Antigravity surfaces | Antigravity 1.5.1 | Windows 10.0.26200 | Config layout check | Shared `~/.gemini/config/mcp_config.json` | CLI and App read identical manifest | verified | 2026-11-10 |
