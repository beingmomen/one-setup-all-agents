# 0013: Antigravity adapter

Status: Accepted (the maintainer accepted the recommendation on each open point below)

This records the choices made while porting the Antigravity adapter (`adapters/antigravity.py`, id `agy`, the tool's command is `agy`). It changes no rule of 0005 to 0012, but it refines one line of 0007 (the Antigravity skills method); the maintainer confirmed that refinement. Facts marked [docs] come from the official Antigravity documentation (`antigravity.google/docs`: rules, mcp, skills, customization-discovery, read on 2026-11-07), [prototype] from the legacy implementation this project grew out of (ported in `_core/`, live-tested on Windows with `agy` 1.4.x), and [unverified] were not checked against a live tool.

## Decision

**Paths (global scope).** No environment variable is read: the docs name none. All paths are under `<home>/.gemini`.

| Item | Path |
|---|---|
| Instructions | `<home>/.gemini/GEMINI.md` |
| MCP | `<home>/.gemini/config/mcp_config.json` |
| Skills manifest | `<home>/.gemini/config/skills.json` |
| Folder agy reads skills from by itself (`paths().skills_dir`) | `<home>/.gemini/config/skills/` |

`~/.gemini/GEMINI.md` is read by the App, the CLI and the IDE [docs]. `~/.gemini/AGENTS.md` and `~/.gemini/config/{AGENTS,GEMINI}.md` are read too and are not managed; a second copy there would be read in addition (said by `notes()`).

**Instructions.** Owned as one file (shared rule in `_common.py`). A rendered text over 24,000 bytes is a `Conflict` and nothing is written, because Antigravity truncates such a file silently [docs; prototype]. The limit applies after `@[label](path)` includes are expanded; the adapter checks the bytes it is given and cannot expand includes.

**MCP: text edit of `mcp_config.json`** through `_core/jsonc_edit.py` (comments, the user's other keys and servers, the BOM and CRLF stay). Entry: stdio `command`, `args`, `env`, `cwd`; http `serverUrl` (`url` is read as an alias and replaced when an owned entry changes; `httpUrl` and SSE are unsupported [docs]); `disabled: true` only when the server is disabled (a native switch, so `enabled = false` is stored, unlike Claude Code). "Equal" is equality on the keys the adapter owns with defaults filled in (`args = []`, `url` = `serverUrl`, `disabled: false` = absent, `.cmd` stripped), so an equal entry of the user's, with other keys, is left alone; the user's other keys survive an update. A differing entry that is not owned is a `Conflict`; an unparseable file gives one `Conflict` per desired or owned server and writes nothing.

**Secrets.** Antigravity documents no variable reference in `env` or `headers` [docs]; the prototype concluded that agy does not expand `${NAME}` [prototype]. Consequences:

- An http server with `headers` (always a variable reference in the neutral model) cannot be written: it is "not written" with an `Unsupported` line, never as a literal. An already written owned entry for such a server is not removed (the same rule as the other adapters for an unwritable server).
- `env_vars` is not written. The prototype found that agy gives its own environment to the server, so the variable reaches it without an entry [prototype, unverified]. It is still reported as `Unsupported` ("env_vars not stored") on every plan and `normalize_mcp` resets it, because the file cannot show it.
- Literal `env` values are non-secret by construction (`McpServer` refuses a secret-looking one). Reading never copies a secret-looking literal or a header into the neutral model.
- The Claude Code rule "refuse to rewrite an owned entry that holds a secret-looking literal" exists because that adapter puts the old entry on a command line and into the journal. Here the op is an `EditFile` whose transform only splices fresh keys into the text; no old value reaches an op, a description or the plan text (a test checks it). The whole file is in the engine's backup, which is `0600` and exists for every file edit. So the refusal does not apply; the source of truth wins and an owned entry is rewritten from it. Undo returns the old bytes.

**Not representable, so `Unsupported` (never silent):** `startup_timeout_sec`; for an http server `command`, `args`, `env`, `env_vars`, `cwd`; for a stdio server `headers`. `auth` is documentation only and is not written.

**Windows launchers.** `npx`, `npm`, `pnpm` and `yarn` are written with the `.cmd` suffix on Windows (the plan decision, same as OpenCode and Codex) and stripped when reading. The prototype wrote the user's own source value unchanged for agy. [unverified with a live tool]

**Skills: one manifest entry per skill, straight at the source folder (refines the Antigravity line of 0007).** 0007 says Antigravity uses "copy plus manifest". The prototype tried marked copies and rejected them on 2026-11-05: a copy shows the name twice if the original is seen again, needs a refresh and a hash, and duplicates data. What it live-tested instead [prototype]: agy does not follow a junction in a skills folder; but a `skills.json` entry that names ONE skill folder by an absolute path is read, and so is a project-relative one; an entry that names a folder holding junctions is not. The docs agree that an entry's `path` may be "the folder of a single customization" and that `~` is not expanded [docs]. So the adapter plans one `EditFile` on `~/.gemini/config/skills.json` that lists `{"path": "<config-dir>/skills/<name>"}` for each skill (forward slashes on every OS): no link, no copy, no refresh, the same on every OS, and no symlink question for Linux and macOS (only "does agy read the manifest there", [docs] yes, [unverified] live).

- Ownership: the ledger records skill names, so an entry is identified by the NAME of its folder (the last path segment). A plain `{"path": ...}` entry whose folder name is an owned skill name is ours; it is repointed when the source moves and removed when the skill leaves the source (the source folder need not exist any more). An entry with other keys (`include_only`, `exclude`), or for a name that is not owned, is the user's and is never changed.
- Equal: an entry for the same folder, or an entry of the user's for the folder that contains it, already lists the skill: nothing is planned and nothing is claimed.
- `Conflict`: a user entry from another path with the same folder name, a real folder or link of that name in `~/.gemini/config/skills/` (agy would list it twice), a skill whose folder name differs from its name (the entry is a path), a relative or missing source, an unparseable manifest (one `Conflict` per name), an `entries` array that holds comments or trailing commas (it is replaced as a whole, so the plan refuses instead of losing them), or an owned entry that was customised.
- Every other member of the file (`exclude`, `inherits`, comments outside `entries`, the BOM, CRLF) survives. Before the op is returned the transform is run on the exact bytes it will receive and the result is compared.
- `read_skills` lists the real folders in `~/.gemini/config/skills/` and the skills the manifest names (an entry that is a skill folder, or the real skill folders one level inside an entry that is a folder).
- `wanted_links()` returns `[]`: nothing here is shared with the `~/.agents/skills` links of OpenCode and Codex (0012), and agy does not read that folder by itself (it appears in the prototype's manifest only as a folder entry, which does not reach junctions). A test with all three adapters shows the shared-link fix and agy's manifest edit side by side.

**Contract suite.** `demo-remote` of the harness has a header, so for agy it is the one server that is not written (`unwritten_mcp()`, as `demo-off` is for Claude Code). The harness removes `demo-remote` in its "reduced" state, which would test nothing for an adapter that never wrote it, so the suite got a second hook, `removable_mcp()` (default `demo-remote`; agy names `demo-off`).

**`StubAdapter` is deleted.** No adapter uses it; the registry and contract tests lost their stub branches, and the base-class defaults (`Unsupported` for hooks, permissions and memory) are tested directly.

## Reason

- The manifest route is the one the maintainer already chose and ran live; it is simpler than a marked copy and has no refresh problem.
- A header or token that cannot be referenced must not be written as a value; reporting "not written" is the only honest result until a bridge exists.
- A native `disabled` switch means a disabled server can be stored faithfully, so it is written, not dropped.

## Open points

- **0007 wording.** The Antigravity line of 0007 ("copy plus manifest") is superseded by "manifest entries to the source" if this ADR is accepted. Alternative: marked copies in `~/.gemini/config/skills/` (the shared planner of `_common.py`, no manifest edit); rejected by the prototype.
- **Http servers with headers** (for example GitHub with a bearer token) are not written. The prototype wrapped them in `all-agents _run --derive ... -- npx mcp-remote <url> --header ...`, so the value never touches a file or argv. That needs the runner command (`_core/runner.py`) to be a public, installed command; not built. Recommendation: decide with the runner/launcher work, not now.
- **`env_vars` by inheritance** is the prototype's belief. A live check of "does an `agy`-started MCP server see a variable that only exists in agy's environment", on Windows and under a GUI launch on macOS (GUI apps do not see shell variables, 0002), is needed before v0.1 is announced.
- **`.cmd` on Windows** is applied by the plan decision, unverified for agy.
- **Per-tool exclude lists** (the prototype's `agy_exclude`) are not built: they belong to the loader (config.toml, 0011) and are applied before the desired skills reach the adapter. Antigravity also has its own `exclude` in the manifest, which the adapter never writes.
- **`file://` paths and permissions.** Not needed for the three v0.1 kinds: the manifest holds plain absolute paths, not `file://` URLs. The prototype's `file://` fix for agy permission rules belongs with the deferred permissions module.
- **Duplicate global files.** `~/.gemini/AGENTS.md` and `~/.gemini/config/{AGENTS,GEMINI}.md` are read in addition to the managed `GEMINI.md` and are not touched. A user who has the old hard links there gets the rules twice; a later `status` check could warn.
- **The 24,000-byte check ignores `@[label](path)` includes** (they expand before the limit applies [docs]).
- **Unverified with a live tool:** everything on Linux and macOS; the manifest on those systems; symlink-following there (not needed by this design); the `.cmd` suffix; `env_vars` inheritance; two surfaces (App, CLI, IDE) reading the same manifest. `detect()` finds `agy` on the `PATH` of `env.environ` or, on Windows, at `%LOCALAPPDATA%\agy\bin\agy.exe` [prototype]; it never starts the program, so the version is not read. (Since 0014 the version IS read: the adapter declares `version_probe` and only `detect.py` runs it; `detect()` itself still starts no program.)
