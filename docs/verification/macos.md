# macOS Verification Results

Verified via GitHub Actions hosted runner: `macos-latest` (macOS 14/15 arm64 Apple Silicon).
Verified commit: `aea08da` (HEAD of `main`).
CI Run ID: `42667223877` (Date: 2026-11-09T17:42:53Z).
Maintainer machine constraint: The maintainer has no macOS hardware (ADR 0007). Only checks executable on hosted CI runners without GUI/tool logins are verified; real tool live tests are recorded as `unverified`.

## Summary Table

| Id | Check | Tool version | OS version | Command (masked) | Expected | Observed | Verdict | Date |
|---|---|---|---|---|---|---|---|---|
| **M01** | Full unit tests (Python 3.11) | N/A | macOS (macos-latest, CI) | `python -m unittest discover -s tests` | 0 failures, skipped=4 | 989 ran in 20.4s, 0 failures, skipped=4 (Job 129334011546) | **verified** | 2026-11-09 |
| **M02** | Full unit tests (Python 3.13) | N/A | macOS (macos-latest, CI) | `python -m unittest discover -s tests` | 0 failures, skipped=4 | 989 ran in 19.9s, 0 failures, skipped=4 (Job 129334011167) | **verified** | 2026-11-09 |
| **M03** | Packaging & CLI smoke test | N/A | macOS (macos-latest, CI) | `smoke` job in CI workflow | Exit 0 | Clean run in 8s (Job 129334011314) | **verified** | 2026-11-09 |
| **M04** | Contract test suite | All adapters | macOS (macos-latest, CI) | `tests/test_adapters.py` & contract | Contract passes | All contract tests passed | **verified** | 2026-11-09 |
| **V01** | Tool detection on real macOS | All tools | macOS | `detect_all(env)` | Real binaries detected | Real tools not installed on runner | unverified | 2026-11-09 |
| **V02** | `CLAUDE_CONFIG_DIR` on real macOS | Claude Code | macOS | `claude mcp list` | Relocates config | Real tool not installed on runner | unverified | 2026-11-09 |
| **V04** | Tool skill symlink following on macOS | All tools | macOS | Tool loads symlink skill | Follows symlink | Real tools not installed on runner | unverified | 2026-11-09 |
| **V05** | Antigravity GUI launch env inheritance | Antigravity | macOS | GUI launch environment | Inherits env_vars | GUI apps not launchable on runner | unverified | 2026-11-09 |
| **V08** | Claude Code CLI on macOS | Claude Code | macOS | `claude mcp add-json / remove` | Modifies `.claude.json` | Real tool not installed on runner | unverified | 2026-11-09 |
| **V09** | Tool config reading on macOS | OpenCode / Codex / Antigravity | macOS | Tool CLI list commands | Reads config | Real tools not installed on runner | unverified | 2026-11-09 |
| **V11** | Undo byte restoration on macOS | All tools | macOS | `all-agents undo` | Restores files & modes | Verified by unit tests (`test_undo_matrix`) | **verified** | 2026-11-09 |

### List of Items that Cannot be Verified on macOS (Unverified)
1. **Real tool login/authentication:** No real accounts or licenses exist on hosted runners.
2. **GUI-launch environment (`env_vars` under macOS Finder/Dock launch):** Antigravity macOS GUI environment inheritance is unverified without physical Mac hardware.
3. **Windows Junctions:** Not applicable on macOS (POSIX symlinks used instead).
4. **Antigravity App surface manifest:** App container path on macOS is unverified with live App.
