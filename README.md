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

## Commands

The command is `all-agents`.

| Command | What it does |
|---|---|
| `all-agents init` | Detect your installed tools, import what you already have (instructions, MCP servers, skills) into the shared config, after taking a full safety snapshot. |
| `all-agents sync` | Show what would change in each tool (dry run), then apply it after you confirm. |
| `all-agents status` | Show, per tool and per kind of config, whether the tool matches the shared config. |
| `all-agents undo` | Reverse the last change using the journal. |

Planned install, once the first release is published:

```text
uvx one-setup-all-agents --help
pipx install one-setup-all-agents    # then run: all-agents --help
```

## Supported tools

| Tool | Adapter id | v0.1 (global scope) | Windows | Linux | macOS | Notes |
|---|---|---|---|---|---|---|
| Claude Code | `claude` | implemented, not yet tried against real tools: M06 pending | not yet verified | unverified | unverified | MCP is written through the tool's own CLI, because running sessions also write its config file. |
| Codex | `codex` | implemented, not yet tried against real tools: M06 pending | not yet verified | unverified | unverified | Only a managed region of its TOML config is edited. |
| OpenCode | `opencode` | implemented, not yet tried against real tools: M06 pending | not yet verified | unverified | unverified | First adapter to be ported (simplest). |
| Antigravity (`agy`) | `agy` | implemented, not yet tried against real tools: M06 pending | not yet verified | unverified | unverified | Does not follow Windows junctions, so each skill is listed in its `skills.json` manifest by the path of the source folder. Has no variable reference for secrets, so an http server with headers is not written. |

The adapters and engine are implemented, but real tool verification is in progress, so no cell says "verified" yet. When the adapters are verified against real tools, verification follows this plan: Windows and Linux (through WSL) are checked with the real tools; macOS is checked only through CI runners for what runs without a login (tests against fixtures). Anything not checked stays marked "unverified" here.

CI runs the tests on Windows, Linux and macOS.

## Roadmap

- **v0.1**: the four commands, global scope, for instructions, MCP servers and skills.
- **v0.2**: project scope, including safety for repositories you do not own.
- **Later**: hooks, permissions, memory, accounts.

## Development

```text
pip install -e .
python -m unittest discover -s tests
```

Decisions are recorded in [docs/decisions](docs/decisions/). To add a tool, see [CONTRIBUTING.md](CONTRIBUTING.md). Rules for AI tools working in this repository are in [AGENTS.md](AGENTS.md).

## License

MIT. See [LICENSE](LICENSE).
