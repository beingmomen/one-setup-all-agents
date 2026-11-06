# 0007: Skills distribution per OS

Status: Accepted. Several behaviors are unverified and marked so.

## Decision

The source of truth for shared skills is `<config-dir>/skills/<name>/` (0003). Each skill is made visible to each tool by one `Link` op per skill and per target directory (never by replacing a whole skills directory), so the tool only ever owns entries it created and user-made skills next to them are untouched.

| OS | Default method | Why |
|---|---|---|
| Windows | directory junction | needs no administrator rights or developer mode (verified in the prototype this design comes from) |
| Linux | symlink | standard |
| macOS | symlink | standard |

Fallback order when a link cannot be created or a tool does not follow links:

1. **Link** (junction or symlink).
2. **Copy plus manifest** (`allow_copy_fallback`): copy the folder, write a small marker file inside it with the source hash so `status` can detect drift and `sync` can refresh it. Where the tool needs its own manifest of skills, the adapter plans the edit of that manifest as an ordinary `EditFile` op.
3. Report the skill as not distributable for that tool. Never fail silently.

Per tool:

- **Antigravity**: does not follow Windows junctions (known), so on Windows it uses copy plus manifest. On Linux and macOS it also uses copy plus manifest until symlink-following is verified (unverified; this is the safe default, not a finding).
- **Claude Code, Codex, OpenCode**: link in each tool's skills directory. Tools that read the shared agents skills directory are linked there once; the engine de-duplicates link targets across adapters.

Rules that make it safe:

- Ownership: a destination that already exists and is not recorded in the ledger is a conflict. It is reported and left alone; it is never overwritten or deleted.
- Undo (0005) records the `before` state of each destination (`absent`, an existing link and its target, or an existing directory). Removing a copied tree on undo requires its hash to match the recorded one.
- Name collisions between a source skill and a skill already in a target directory are conflicts, shown in the plan.

## Reason

- Per-skill links make `sync` additive and reversible, and protect skills the user installed by other means.
- Junctions are the one Windows link type that works without privileges for directories; symlinks need privileges there.
- A copy fallback is the only method that works for a tool that ignores links, at the price of needing a drift check.

## Verification plan

The maintainer has no Mac. Verification is therefore split by what can actually be run:

1. **Windows**: on the maintainer's machine, with the real tools.
2. **Linux**: through WSL on the same machine, with the real tools installed there. This covers symlink-following for Claude Code, Codex, OpenCode and Antigravity, and the Antigravity manifest question, on Linux.
3. **macOS**: only through GitHub Actions macOS runners, and only for what runs there without a login: the unit and contract tests, and the link, atomic-write and path logic against fixtures. Anything that needs a real, signed-in tool on macOS is **not verified**.
4. Everything not verified by 1 to 3 stays marked "unverified" in the adapter docstring, in this ADR and in the support table of the README. Nothing is promoted to "supported" on a guess.

## Open points (unverified)

- Whether Codex, OpenCode and Claude Code follow symlinks to skill folders on Linux and macOS. Believed yes; not checked. Needs a real-tool check on each OS before v0.1 is announced.
- Whether Antigravity follows symlinks on Linux or macOS, and whether it needs its manifest there. Not checked.
- Which tools read the shared agents skills directory is a convention observed in the prototype, not a guarantee; each adapter's `paths()` must state its source and be re-checked per release.
- Junction creation uses either `_winapi.CreateJunction` (undocumented in the standard library) or `mklink /J`; pick one in the implementation and test both Python versions in CI.
- Windows file-system limits (long paths) when copying deep skill trees.
