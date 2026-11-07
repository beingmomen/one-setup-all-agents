# 0014: Tool detection and version probes

Status: Proposed

`init` and `status` need to know, for each registered tool, whether it is installed, where its binary is, which version it is and where its config lives, on Windows, Linux and macOS. The adapters already had a `detect()` that finds the binary and the config root without starting a program (0008, 0011 to 0013) and left `version` empty. This ADR fills the gap without breaking the boundary of 0008. It adds no rule to 0005 to 0013 and changes none. Facts marked [measured] come from running the tools' `--version` on the maintainer's Windows machine (the version strings in the legacy research notes); [unverified] were not checked against a live tool.

## Decision

**The split.** An adapter *declares*, one module *runs*.

- `ToolAdapter.version_probe(env, binary) -> VersionProbe | None` is pure. A `VersionProbe` is `(argv, parse)`: the command (`argv[0]` is the full path of the binary `detect()` found) and a function from the captured output to a version string or `None`. The default is `<binary> --version` read by the tolerant `parse_version`. Each adapter overrides it only to say which number is the version when a banner holds several (`2.1.310 (Claude Code)`, `codex-cli 0.146.0`); a banner that is not found falls back to the first version-looking token. Returning `None` means "no side-effect-free way to read it": no command is run and no warning is raised.
- `detect.py` (not inside `adapters/`) is the only code that starts that command: `detect_all(env, adapters=None, *, probe=None, timeout=8.0, versions=True) -> list[Detection]`, plus `detect_one` and `render_detections`. It names no tool; the static boundary test scans it like any non-adapter module.
- `adapter.detect()` still never starts a program, so its `version` stays `None`. `detect_all` fills it.

**How the command runs.** A list of arguments (no shell), no stdin, stdout and stderr captured, the environment of `Env.environ` and nothing else, a timeout of 8 seconds, then the process (the whole group on POSIX) is killed. Output goes to temporary files, not pipes, so a launcher shim whose child keeps a pipe open cannot hold the run past the timeout; at most 4096 bytes of each stream are read. The runner is injectable: `(argv, environ, timeout) -> ProbeResult(returncode, stdout, stderr)`, raising `TimeoutError` on a timeout and `OSError` when the command cannot be started. Tests pass an in-process fake, so no tool ever runs in a test. The probes run one after the other; the worst case with every tool hanging is 4 x 8 seconds, then a warning each.

**Failures are per tool and never raise.** Non-zero exit, timeout, a binary that vanished between lookup and run, any other start error, an exception from the runner or the parser, and output with no version all give `version=None` and one warning on that tool's `Detection`. Warnings name the cause (status, timeout, error class or `strerror`), never the command's output or an exception's message. An adapter whose `detect()` raises becomes an absent `Detection` with a warning; the other tools are still detected. The version is read from stdout, then from stderr.

**Finding the binary** (shared helper `adapters/_common.find_binary`, used by all four adapters; it replaces `shutil.which`, which reads `PATHEXT` from the real process):

1. The adapter's own override variable, where it has one. Only Claude Code does (`ALL_AGENTS_CLAUDE_BIN`, 0011); a path that does not exist is a warning and is not replaced by another install.
2. The folders of `PATH` from `env.environ`, in order. The variable name is case-insensitive on Windows. Empty entries and relative entries are skipped.
3. Known install folders that may not be on `PATH`: the same for every tool, per OS (Windows: `~/.local/bin`, `%APPDATA%/npm`, `%LOCALAPPDATA%/Microsoft/WindowsApps`; Linux: `~/.local/bin`, `~/.npm-global/bin`, `~/.bun/bin`, `/usr/local/bin`; macOS: the same plus `/opt/homebrew/bin`), and per adapter (Claude Code `~/.claude/local`; OpenCode `~/.opencode/bin` [unverified]; Antigravity `%LOCALAPPDATA%/agy/bin` on Windows, from the legacy code). `APPDATA` and `LOCALAPPDATA` default to `<home>/AppData/Roaming` and `.../Local` when the variables are missing.

On Windows the extensions come from `PATHEXT` of `env.environ` (default `.COM;.EXE;.BAT;.CMD`), so a `.cmd` shim is found, and the bare name never matches (an npm install leaves an extensionless shell script beside its `.cmd`, which Windows cannot run). On Linux and macOS the file must be executable. The OS and `os.environ` are never consulted.

**What counts as installed, and which signal.** `Detection` gets `binary_source` (`env-override`, `path` or `known-location`), `tool`, `label` and `config_found` (the config roots that exist), all with defaults, so existing code and adapters keep working. `installed` keeps its meaning ("there is something of this tool to manage": a binary or its config). `Detection.status` is the honest label: `installed` (a binary was found), `config-only` (config but no binary) or `absent`. A config-only tool is reported as such and is never shown as installed; a binary found only in a known folder says so. A binary found through a known folder or the override is called by its full path in a `RunCli`; one found on `PATH` is still called by its bare name (0011), so plans read the same on every machine.

**The report.** `render_detections(detections, env)` returns plain text: per tool the status, the binary with how it was found, the version (or `unknown`), each config root with found / not found, and the warnings. Paths under the home are shown as `~/...`, in either separator style (case-insensitive on Windows); free-text warnings get the same masking. The report holds only what the detections hold: no environment value is printed (a variable can appear by name in a warning, as the adapters already wrote them). No public command is wired yet; `init` (next plan item) and `status` will call `detect_all`.

## Reason

- Reading a version means starting the user's program. 0008 keeps the adapter pure so that plans are testable and reviewable; a declaration plus one runner keeps that, and gives the timeout, the environment and the failure handling one home instead of four.
- A probe that hangs or fails must not make `init` fail: the user can still use a tool whose `--version` is broken.
- `shutil.which` uses the real process's `PATHEXT`, so a test or a caller with a different `Env` got different answers; the lookup had to move to `Env`.
- Pipes plus `subprocess.run(timeout=...)` can block on Windows after the kill when a shim's child holds the pipe; files cannot.

## Open points

- **Timeout.** 8 seconds is a guess for a cold start with antivirus scanning [unverified]. Recommendation: keep it, raise it only if a real tool is seen to need more.
- **Override variables for the other tools.** Only Claude Code has one. A uniform `ALL_AGENTS_<TOOL>_BIN` for all four would help users with an install in an unusual place, but it adds four documented names. Recommendation: add them only when someone needs one.
- **Parallel probes.** Sequential is simplest and deterministic; a hang on every tool costs 32 seconds. Recommendation: keep it sequential until `init` shows a progress line, then reconsider.
- **Not verified with live tools:** the exact `--version` output of Antigravity (a bare number is assumed, a banner also reads); the OpenCode install folder `~/.opencode/bin`; a standalone Codex install on Windows (its folder is not in the list, so a copy not on `PATH` is "config only"); the macOS folders; running a `.cmd` shim with `--version` through `CreateProcess` on every Windows version. The runner itself is tested with the Python interpreter, on Windows and Linux (WSL); macOS is not tested.
- A Windows app execution alias (`WindowsApps`) that is only a Store stub would be run by the version probe and could open the Store. None of the four tools is known to ship that way; the timeout bounds it.
- 0011 to 0013 still say "the version is not read". After this ADR it is read by `detect.py`, not by `detect()`.
