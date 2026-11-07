# 0012: Codex adapter and shared skill links

Status: Accepted (the maintainer accepted the recommendation on each open point below)

This records the choices made while porting the Codex adapter (`adapters/codex.py`) and while fixing a bug in how the engine merges skill links that two tools share. It changes no rule of 0005 to 0011. Where it settles an open point of an accepted ADR, it says so. Facts marked [docs] come from the Codex source and documentation (read through the `context7` index of the `openai/codex` repository), [prototype] from the legacy implementation this project grew out of (ported in `_core/`), and [unverified] were not checked against a live tool.

## Decision

### Part A: the Codex adapter

**Paths (global scope).** `CODEX_HOME` is read from `env.environ`, never from the OS, and only when it is an absolute path in the style of `env.platform`. An empty, relative, `~` or foreign-style value is ignored, and `detect()` warns about a non-empty one (Codex would resolve a relative value against its own working directory, which an adapter cannot know). `detect()` also warns when an absolute `CODEX_HOME` points to a folder that does not exist, because Codex refuses to start then [docs: `find_codex_home`].

| Item | With `CODEX_HOME=<d>` | Without |
|---|---|---|
| Instructions | `<d>/AGENTS.md` | `<home>/.codex/AGENTS.md` |
| MCP | `<d>/config.toml` | `<home>/.codex/config.toml` |
| Skills | `<home>/.agents/skills/<name>` | `<home>/.agents/skills/<name>` |

Skills do not follow `CODEX_HOME`: Codex reads `<home>/.agents/skills` (and the deprecated `$CODEX_HOME/skills`, which this adapter does not manage) [docs: `ext/skills/src/host_roots.rs`]. That is the folder the OpenCode adapter links into, so a skill is linked once for both tools. [Windows default paths: prototype; the effect of `CODEX_HOME` on Linux and macOS and symlink-following there: unverified.]

**Instructions and `AGENTS.override.md`.** The global file is `<codex home>/AGENTS.md`, owned as one item (shared rule in `_common.py`). Codex reads `AGENTS.override.md` in the same folder instead of `AGENTS.md` when it is not empty [docs]. Then the instructions this adapter writes are not in effect, so `plan_instructions` adds a `Conflict` for the override file (it is never touched; the `AGENTS.md` is still written, ready for when the override is gone) and `detect()` warns. The 32 KiB limit does not apply to the global file [prototype, measured with `codex debug prompt-input`], so no size check is made.

**MCP is edited in `config.toml` as text, through `_core/toml_edit.py`.** Only `[mcp_servers.<name>]` tables are touched. A table that is new or changes is written inside the managed region at the end of the file (`# >>> one-setup-all-agents >>>` ... `# <<< one-setup-all-agents <<<`); an owned table that leaves the source is removed wherever it is; everything else (other tables, comments, key order, BOM, CRLF) is kept. A table that changes loses the comments that were inside it, and moves into the region. The op is an `EditFile` with `expect_sha256` and a pure transform. At plan time the transform is tried on the exact bytes it will receive, and the result is parsed back (`tomllib`) and compared: nothing outside the touched servers may differ, and every touched server must read back as planned. If that fails (the file does not parse, a bare `[mcp_servers]` table, a server defined as an inline table or by dotted keys, nested tables below an owned server) every affected server becomes a `Conflict` with a reason that holds no file content, and nothing is written. An unparseable file gives one `Conflict` per desired or owned server. This is the CLI-free route: unlike `.claude.json`, `config.toml` is not rewritten by a running Codex session while it works, so the content check at apply time is the right drift guard.

**Entry format.** All keys are in `mcp_types.rs` [docs]: stdio `command`, `args`, `env` (literal values), `env_vars` (names Codex passes on; plain names or `{name, source}` tables), `cwd`; http `url`, `bearer_token_env_var`, `http_headers` (literal), `env_http_headers` (header name to variable name); shared `enabled`, `startup_timeout_sec` (seconds, fractions allowed), `tool_timeout_sec`.

| Neutral field | Codex |
|---|---|
| `env_vars` | `env_vars = ["NAME"]` |
| `env` (non-secret literal) | `[mcp_servers.<n>.env]` |
| `HeaderRef("Authorization", V, "Bearer")` | `bearer_token_env_var = "V"` (Codex adds `Bearer `) |
| `HeaderRef(H, V, None)` | `[mcp_servers.<n>.env_http_headers]` with `H = "V"` |
| `enabled = False` | `enabled = false` (native; nothing is "unwritten") |
| `startup_timeout_sec` | `startup_timeout_sec` (native) |

A secret is therefore always a variable NAME, in Codex's own way, and nothing in `env` or `args` is expanded by Codex, so a `${NAME}` there would only be a literal. A header that is neither a whole variable nor `Authorization: Bearer <variable>` (another scheme, or a scheme on another header) cannot be stored: the server is "not written" with a reason (the prototype did the same). A literal `http_headers` table of the user's is not part of the neutral model; it is not read and not touched.

**Which keys are ours.** `command`, `args`, `url`, `env`, `env_vars`, `cwd`, `enabled`, `bearer_token_env_var`, `env_http_headers`, and `startup_timeout_sec` only when the source sets one. "Equal" is equality on those keys with Codex's defaults filled in (`args = []`, `enabled = true`, `.cmd` stripped from launchers), so an equal entry of the user's, with other keys, is left alone and not claimed. A timeout the user added by hand to an owned server survives a sync in which the source has no opinion (the prototype's `KEEP_IF_ABSENT`). All the user's other keys (`tool_timeout_sec`, `required`, `http_headers`, ...) survive an update. A differing entry that is not owned is a `Conflict`.

**Not representable, so `Unsupported` (never silent):** for an http server `command`, `args`, `env`, `env_vars`, `cwd`; for a stdio server `headers`. `auth` is documentation only (like OpenCode) and is not written. Server names are limited to what `codex mcp add` accepts [docs: `validate_server_name`]: letters, digits and `- _ : @ / .`; others are "not written". `normalize_mcp` names exactly what does not survive a round trip, plus the single spelling of `Authorization: Bearer` and the header order (sorted by name).

**Windows launchers.** `npx`, `npm`, `pnpm` and `yarn` are written with the `.cmd` suffix on Windows (the plan decision "add `.cmd` in the global config on Windows"; same as OpenCode), and stripped when reading. [docs] Codex resolves the program through `PATHEXT` itself (`program_resolver::resolve`), so the plain name would work too; the prototype wrote `.cmd` for Codex on Windows in project files only and left the user-level source to say it. The suffix is kept for one consistent rule across the file-edit adapters. Not re-verified here with a live tool.

**Skills.** One `Link` per skill from the source into `<home>/.agents/skills/<name>`, with the copy fallback, through the shared planner of `_common.py`. No code of its own.

### Part B: a withdrawn link that another tool still wants (the engine fix)

**The bug.** OpenCode and Codex link skills into the same folder. If one withdraws a skill (`Link(target=None)`, with `releases`) while the other still wants it but plans nothing, because the link is already as wanted, `merge_links` saw only the removal and deleted the shared link under the other tool.

**The fix, in the engine.** `ToolAdapter` gets a small read-only method with a default: `wanted_links(env, scope, desired_skills) -> list[WantedLink]`. `WantedLink(link, target, ref, allow_copy_fallback)` (in `model.py`) says "I want this link to exist, whatever I plan". The default derives it from `paths().skills_dir` and the desired skills, which fits every adapter that links one folder per skill (OpenCode, Claude Code, Codex), so no adapter had to change; an adapter that places skills another way (Antigravity's copy plus manifest) overrides it. `build_plan` collects it from every adapter whose skills are managed in this run and hands the result to `merge_links(ops, wanted_links)`.

After the normal merge, a removal that is left over is replaced when some OTHER tool wants that path and the path is already in the state that tool wants (a link or junction to its target, or an unedited marked copy of it). The replacement is a `Link` to that target with the `releases` of the withdrawing tools and the `claims` of the keeping tools. The path is already as wanted, so the engine changes nothing on disk (0010, "a path already in the wanted state is a no-op"); it still journals the op, which is what makes the ownership change undoable. No new op shape and no engine change beyond `plan.py` were needed: apply and revert already handle a `Link` that is a no-op on disk (the delta of `owned` is recorded and reverted as before). If the path is not in the wanted state (it points elsewhere), the other tool plans its own relink or reports its own `Conflict`, and the existing rules apply. A tool that is not part of the build, or does not manage skills in this run, keeps nothing: the removal proceeds.

**Ownership, as chosen.** The withdrawing tool stops owning the item. Every tool that keeps it CLAIMS it, so a tool that was "equal but unowned" (it never made the link) becomes the owner. Reason: the link now exists only on that tool's behalf; with no owner it would be an orphan that no later withdrawal could remove (the engine removes only what the ledger records as owned). An undo gives each tool back exactly what it owned before. A tool that already owned the item claims it again (no change to the set).

## Reason

- The fix belongs in the engine because only the engine sees all the adapters at once (0008); an adapter cannot know that another one reads the same folder.
- A query ("what do you want?") is independent of what an adapter plans, which is exactly the information the plan lacks when everything is already in place.
- Reusing a `Link` to the existing target as the release-only carrier keeps the op set closed (0008) and gives the undo for free.
- Text editing of `config.toml` with a dry run and a parse-back check turns "the edit might have gone wrong" into a `Conflict` before anything is written, the same promise the OpenCode adapter makes.

## Open points

- **`AGENTS.override.md` is a permanent `Conflict`** while the file exists and is not empty. Alternative: only warn in `detect()`. Chosen because the desired state is not in effect, and `sync` should not say "nothing to do". Confirmed by the maintainer.
- **`.cmd` on Windows** is applied by the plan decision although Codex resolves `PATHEXT` itself. Alternative: plain names, like Claude Code. Needs a live check on Windows.
- **A changed owned table is rewritten whole inside the managed region**, so comments inside it are lost and it moves. A finer in-place edit of one table would need a new function in `toml_edit.py`; not built.
- **Nested tables below an owned server** (`[mcp_servers.<n>.tools.<tool>]`) make an update a `Conflict` (the legacy renderer handles one level only). A server that is only added or left alone is unaffected.
- **A copy-fallback skill** that another tool still wants is turned back into a link by the keeping op if the link can be made. Harmless (the copy was unedited), but it is a change on disk that the plan shows as a link.
- **Duplicate skills between `~/.agents/skills` and `$CODEX_HOME/skills`** (for example the vendor's `.system` copies) are not handled here; the legacy `[[skills.config]] enabled = false` policy is not ported.
- **Unverified with a live tool:** Windows launcher names with and without `.cmd`, `CODEX_HOME` on Linux and macOS, symlink-following for skills on Linux and macOS, and everything on macOS. The version of the `codex` CLI is not read (`detect()` must not start a program).
- Adoption of an existing equal entry or link into `owned` (0009 open point) is still done only by this claim in the shared-link case; `init` is still expected to record the rest.
