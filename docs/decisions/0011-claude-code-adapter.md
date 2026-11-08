# 0011: Claude Code adapter

Status: Accepted

This records the choices made while porting the Claude Code adapter (`adapters/claude_code.py`). It changes no rule of 0005 to 0010. Where it settles an open point of an accepted ADR, it says so. Facts marked [docs] come from the official Claude Code documentation, [prototype] from the legacy implementation this project grew out of (ported in `_core/`), and [unverified] were not checked against a live tool.

## Decision

**Paths (global scope).** `CLAUDE_CONFIG_DIR` is read from `env.environ`, never from the OS, and only when it is an absolute path in the style of `env.platform`. A relative, `~` or foreign-style value is ignored, and `detect()` warns, because Claude Code would resolve a relative value against its own working directory, which an adapter cannot know.

| Item | With `CLAUDE_CONFIG_DIR=<d>` | Without |
|---|---|---|
| Instructions | `<d>/CLAUDE.md` | `<home>/.claude/CLAUDE.md` |
| Skills | `<d>/skills/<name>` | `<home>/.claude/skills/<name>` |
| MCP (user scope) | `<d>/.claude.json` | `<home>/.claude.json` |

[docs] state that with the variable set, Claude Code reads `.claude.json` from inside that directory instead of from the home directory. The default locations are [prototype] verified on Windows. The effect of the variable with a live tool, and everything on Linux and macOS, is [unverified].

**MCP is written through the CLI, not by editing `.claude.json` (settles the 0005 open point for this tool).** Claude Code rewrites `.claude.json` constantly (counters, per-project state, caches). An `EditFile` would be refused by the engine's content check whenever a session wrote in between, and a read-modify-write of the whole file could lose that session's changes. The CLI writes with Claude Code's own logic. So `plan_mcp` returns `RunCli` ops, always with `-s user` (a same-named project or local server is never touched):

| Change | `argv` | `inverse_argv` |
|---|---|---|
| add | `claude mcp add-json <name> '<json>' -s user` | `claude mcp remove <name> -s user` |
| remove | `claude mcp remove <name> -s user` | `claude mcp add-json <name> '<old entry>' -s user` |
| change an owned server | the remove, then the add | as above |

The CLI has no update, so a change is two ops, and the engine's revert of a failed add re-adds the old entry (0010, "A remove-then-add pair"). The file is only read, to compare, with one retry for a torn read. Keys of an owned entry that the adapter does not own (`oauth`, `headersHelper`, ...) are carried into the re-added entry. The old entry goes into `inverse_argv` and a patched copy into `argv`, which means into the journal in clear and onto a process command line. So before a remove or a replace of an owned entry, every nested string of the old entry (env values with their keys, header values, `oauth` fields, arguments after a secret-looking flag) is checked with the `_core/mask.py` detectors; a `${NAME}` reference is fine. If anything looks secret, no `RunCli` is planned for that server: it is a `Conflict` that names the field, never the value, and says to move the value to an environment variable or edit the entry by hand.

The binary is `ALL_AGENTS_CLAUDE_BIN` (a path, the prototype's variable; a path that does not exist is reported, not replaced by a different install found on `PATH`), else `claude` on the `PATH` of `env.environ`. If it is missing, each server that would change becomes a `Conflict` that says so, and the instructions and skills of the same tool are unaffected. A plan with nothing to change needs no CLI. On Windows, a `claude.cmd` shim (the npm install) re-parses its arguments in `cmd.exe`, so an argument with one of `% & | < > ^ !` is refused with a `Conflict` instead of being passed; `claude.exe` has no such limit.

**Entry format.** `{"type": "stdio", "command", "args", "env"}` and `{"type": "http", "url", "headers"}`. `type` is always written (an entry with a `url` and no `type` is a configuration error [docs]). A secret is `${NAME}` (Claude Code expands `${VAR}` and `${VAR:-default}` in `command`, `args`, `env`, `url` and `headers` [docs]); a header is `Scheme ${NAME}`. Reading accepts `sse` and `streamable-http` as http, skips a `ws` entry, never copies a secret-looking literal or a reference to a differently named variable into the neutral model, and strips a `.cmd` suffix from `npx`, `npm`, `pnpm`, `yarn`. "Equal" means equal on the keys the adapter owns, with defaults filled in, so a user's equal entry with extra keys or no `type` is left alone.

**Not representable, so `Unsupported` (never silent):**

- `cwd`: not a documented key [docs]. The prototype wrote it; whether Claude Code honors it is [unverified], so it stays unsupported until a documented key exists.
- `startup_timeout_sec`: Claude Code has only the global `MCP_TIMEOUT`.
- For an http server: `command`, `args`, `env`, `env_vars`, `cwd`. For a stdio server: `headers`.
- `auth`: documentation only; OAuth is the default.
- Not written at all, with a reason: a server name the CLI rejects (only letters, digits, hyphen and underscore, not starting with a hyphen), an env literal containing `${` (Claude Code would expand it), an invalid variable, header or scheme name.

`normalize_mcp` lists exactly what does not survive a round trip.

**A disabled server is not written (`enabled = false`).** The user scope has no per-server switch [docs] (disabling is a per-project list), and the prototype's `disabled` key has no documented effect. Writing the server would turn on something the source disables, so a desired server with `enabled = false` means "should not be present in Claude Code": absent, nothing is planned; present and owned, it is removed by the normal remove path (with a `release`, and with the secret-value refusal above); present and not owned, it is left alone, with no `Conflict` just for being disabled. In every case one `Unsupported` line names the server ("disabled in the source; Claude Code has no per-server switch, so it is not written"). `normalize_mcp` leaves such a server out. The contract suite got a hook, `AdapterContract.unwritten_mcp()`, so an adapter can declare the harness server names it deliberately does not write (`demo-off` for this adapter); nothing else may be dropped.

**Windows launchers.** `npx`, `npm`, `pnpm` and `yarn` are written as plain names on every platform. The prototype did exactly this for Claude Code ("Windows resolves these only as `.cmd` outside Claude Code") and ran it live, and the Claude Code changelog removed its "requires a `cmd /c` wrapper" warning as a false positive [docs]. This differs from the OpenCode adapter, which writes `.cmd` on Windows. Not re-verified here with a live tool.

**Skills.** One `Link` per skill straight from `<config-dir>/skills/<name>` (the source) into `<claude config>/skills/<name>`, with the copy fallback of 0007. Claude Code does not read `~/.agents/skills` [docs, prototype]. The prototype's chain `repo -> ~/.agents/skills -> ~/.claude/skills` is therefore not reproduced: there is no intermediate folder. Skill exclusion is decided but not built: a per-tool exclude list in `config.toml` (the prototype's `claude_exclude`), applied by the loader before the desired skills reach the adapter, so the adapter needs no knob; an owned skill that falls out of the list is withdrawn like any other. Future work for the loader (Phase 4/5). The folders `.trash` and `synced` (managed by Claude Code and claude.ai) are not listed by `read_skills` and are never touched. Symlinks and junctions are followed on Windows [prototype]; Linux and macOS are [unverified] (0007 open point).

**Shared code.** The whole-file instructions rule and the per-skill link planner are identical for OpenCode and Claude Code and live in `adapters/_common.py`. OpenCode's behavior and tests are unchanged.

## Reason

- The CLI route is the only one that is correct while a session is running, and it is the behavior that was used daily in the prototype.
- A plan that says "not stored" for each lost field keeps the product honest (0008); writing an undocumented key to make a round trip look clean would not.
- Refusing a `.cmd` shim with risky characters is cheaper than debugging a mangled server entry.

## Consequences accepted by the maintainer

- **Undo is by inverse command, not by bytes.** Undoing an MCP change runs the recorded inverse (`remove`, or `add-json` with the old entry). The servers are restored; the bytes of `.claude.json` may differ afterwards, because the real CLI may reformat the file and order servers differently. The contract suite checks exact bytes only because its fake CLI writes a canonical form.
- **The prototype's links are adopted by `init` (Phase 4).** Existing junctions `~/.claude/skills/<name> -> ~/.agents/skills/<name>` are not owned and differ from the source, so until then they are reported as `Conflict`. `init` adopts them into ownership. Skills installed by other means into `~/.agents/skills` no longer reach Claude Code unless they are in the source.
- **`cwd` stays unsupported** (above) and **skill exclusion lives in the loader** (above).

## Open points

- **A `RunCli` has no drift check (0010).** An entry changed by a session between the plan and the apply is removed and re-added from the planned view. The window is small; an `add` of a name that appeared meanwhile fails and is reverted.
- **Unverified with a live tool:** Windows launcher names without `.cmd`, `CLAUDE_CONFIG_DIR`, symlinks on Linux and macOS, and everything on macOS.
- The version of the `claude` CLI is not read (`detect()` must not start a program). (Since 0014 the version IS read: the adapter declares `version_probe` and only `detect.py` runs it; `detect()` itself still starts no program.)
- The secret-value refusal uses the project's heuristics, so an owned entry with a long opaque argument can be refused without holding a secret; the user then edits it by hand.
