# 0002: Config and state locations per OS

Status: Accepted (macOS follows Linux, confirmed by the maintainer)

## Decision

Two separate directories, both created on demand, both overridable.

| | Config directory (the user's source of truth) | State directory (machine-local: journal, backups, lock) |
|---|---|---|
| Windows | `%APPDATA%\one-setup-all-agents` | `%LOCALAPPDATA%\one-setup-all-agents` |
| Linux | `$XDG_CONFIG_HOME/one-setup-all-agents`, default `~/.config/one-setup-all-agents` | `$XDG_STATE_HOME/one-setup-all-agents`, default `~/.local/state/one-setup-all-agents` |
| macOS | `~/.config/one-setup-all-agents` (same as Linux) | `~/.local/state/one-setup-all-agents` (same as Linux) |
| Override | `ALL_AGENTS_CONFIG_DIR` or `--config-dir` | `ALL_AGENTS_STATE_DIR` |

Rules:

- The command line flag beats the environment variable, which beats the default.
- State is never inside the config directory. The config directory is meant to be copied, versioned or shared between machines; the state holds absolute paths and backups that only make sense on one machine.
- The state directory is created with mode `0700` on POSIX and its files with `0600`, because backups contain copies of the tools' config files, which may hold secrets. Backup contents are never printed.
- The engine receives both directories through `Env` (`model.py`). No other code computes them.
- Tool config roots (where Claude Code, Codex, OpenCode and Antigravity keep their files) are not decided here. Each adapter's `paths()` owns them and must honor that tool's own override variables.

## Reason

- Separating source from state makes the source portable and keeps secrets-bearing backups out of anything a user might commit.
- XDG paths are the Linux convention. The AI tools this project manages already use `~/.config`-style and dotfolder locations on every OS, so macOS follows Linux rather than `~/Library/Application Support`.

## Open points

- macOS: `~/.config` and `~/.local/state`, same as Linux (decided). `~/Library/Application Support` is not used.
- WSL and Windows have separate homes and therefore separate configs. Sharing one config between them is possible by pointing `ALL_AGENTS_CONFIG_DIR` at the same folder; the documentation must say so. Not tested.
- GUI apps on macOS do not see shell environment variables, so an override set only in a shell profile is not seen by them. Relevant for secrets by variable name; document it.
