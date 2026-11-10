# Linux Verification Results

Tested on: Ubuntu 24.04 LTS (WSL2), Linux kernel 6.6.x, Python 3.14.4 (CPython 64-bit).
Tested date: 2026-11-10.
Safety Level: L0 (full unit test suite on Linux native filesystem `~/osaa-copy`).
Real tools installation inside WSL: Not installed (per maintainer gate decision; tool checks marked `unverified`).

## Summary Table

| Id | Check | Tool version | OS version | Command (masked) | Expected | Observed | Verdict | Date |
|---|---|---|---|---|---|---|---|---|
| **L0** | Full test suite on Linux | N/A | Linux (Ubuntu WSL2) | `python3 -m unittest discover -s tests` | 0 failures, POSIX tests run | 989 ran, 0 failures, skipped=4 | **verified** | 2026-11-10 |
| **P01** | POSIX permissions (`0600` files, `0700` dirs) | N/A | Linux (Ubuntu WSL2) | `test_engine_apply.JournalPermsTests` | Files `0600`, Dirs `0700` | Modes strictly enforced | **verified** | 2026-11-10 |
| **P02** | Symlink skills creation and resolution | N/A | Linux (Ubuntu WSL2) | `test_engine_links.SymlinkTests` | Native symlinks | Created and resolved correctly | **verified** | 2026-11-10 |
| **P03** | File locking via `fcntl.flock` | N/A | Linux (Ubuntu WSL2) | `test_engine_lock.FlockTests` | Advisory locking | Locks held and released cleanly | **verified** | 2026-11-10 |
| **P04** | Executable bit preservation (`EditFile.mode`) | N/A | Linux (Ubuntu WSL2) | `test_undo_matrix.UndoMatrixModeTests` | `0755` mode preserved | Byte and mode exact restoration | **verified** | 2026-11-10 |
| **P05** | Drive-letter rejection on POSIX | N/A | Linux (Ubuntu WSL2) | `test_phase2_compat.DriveLetterTests` | Refuse Windows drive letters | Non-Windows path handling enforced | **verified** | 2026-11-10 |
| **P06** | Personal data guard on Linux | N/A | Linux (Ubuntu WSL2) | `python3 scripts/check_personal_data.py` | Clean (24 patterns) | Clean (24 patterns) | **verified** | 2026-11-10 |
| **P07** | Tool boundary isolation test | N/A | Linux (Ubuntu WSL2) | `test_tool_boundary.py` | 6 tests OK | 6 tests OK (0.038s) | **verified** | 2026-11-10 |
| **V01** | Tool version detection in WSL | All tools | Linux (Ubuntu WSL2) | `detect_all(env)` | Tools detected if installed | Tools not installed in WSL | unverified | 2026-11-10 |
| **V02** | `CLAUDE_CONFIG_DIR` in WSL | Claude Code | Linux (Ubuntu WSL2) | `claude mcp list` | Relocates config | Tools not installed in WSL | unverified | 2026-11-10 |
| **V04** | Tool skill symlink traversal | All tools | Linux (Ubuntu WSL2) | Tool reads symlink skill | Tool loads symlinked skill | Tools not installed in WSL | unverified | 2026-11-10 |
| **V08** | Claude Code CLI in WSL | Claude Code | Linux (Ubuntu WSL2) | `claude mcp add-json / remove` | Modifies `.claude.json` | Tools not installed in WSL | unverified | 2026-11-10 |
| **V09** | Tool MCP config reading in WSL | OpenCode / Codex / Antigravity | Linux (Ubuntu WSL2) | Tool `mcp list` | Reads config | Tools not installed in WSL | unverified | 2026-11-10 |
| **V11** | Undo byte restoration in WSL | All tools | Linux (Ubuntu WSL2) | `all-agents undo` | Restores files & modes | Tested via unit suite (`test_undo_matrix`) | **verified** | 2026-11-10 |

### Notes
- **Skipped tests on Linux:** Exactly 4 tests were skipped out of 989:
  1. `test_create_junction_falls_back_to_mklink_when_winapi_has_no_junction_call` (junctions are Windows only)
  2. `test_the_mklink_junction_method_makes_the_same_kind_of_link` (junctions are Windows only)
  3. `test_windows_uses_a_junction_without_administrator_rights` (junctions are Windows only)
  4. `test_build_wheel_optional` (`PACKAGING_TESTS` not set)
- **Cross-OS note:** Windows host and WSL environment have separate filesystem roots and user homes (`/mnt/c/Users/<user>` vs `~` / `/home/<user>`). Shared config between both environments requires setting `ALL_AGENTS_CONFIG_DIR` explicitly.
