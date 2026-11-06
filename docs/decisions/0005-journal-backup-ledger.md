# 0005: One journal, backup and ledger format; the `undo` guarantee

Status: Accepted

## Context

"Any change can be undone with one command" is the product promise and the top risk. A previous prototype of this idea kept four different manifest formats, needed the user to pass a folder path to roll back, refused changed files without a way forward, wrote one tool's config through a non-atomic remove-then-add, and treated links as something other than files. This project has one format for every kind of change.

## Decision

All changes are expressed as the three engine ops (`EditFile`, `RunCli`, `Link`; `Unsupported` is never executed) and recorded in one place.

State directory (0002):

```text
ledger.json                  ownership + index of operations
ops/<op-id>/journal.json     the entries of one operation
ops/<op-id>/files/NNNN       backup payloads referenced by the journal
```

- **Operation id**: UTC timestamp plus a short random suffix, for example `20261106T101500Z-a1b2`. Sortable, unique, no counters.
- **`journal.json`**: `{"format": "one-setup-all-agents/journal", "version": 1, "id", "label", "created", "status", "entries": [...]}`. Status is `open`, `done`, `failed` or `undone`.
- **Entry**: `seq`, `type` (`edit_file`, `run_cli`, `link`), `status` (`planned`, `done`, `failed`), `before` and `after`.
  - `before` and `after` describe a path as one of: `absent`; `file` (sha256, mode, backup reference); `symlink` (target); `dir_link` (kind `junction` or `symlink`, target); `copy_tree` (tree hash, marker).
  - `run_cli` entries carry `argv` and `inverse_argv`. An op without an inverse is rejected at plan time.
- **`ledger.json`**: `{"format": "one-setup-all-agents/ledger", "version": 1, "owned": {tool: {kind: [item keys]}}, "operations": [{id, label, created, status}]}`. `owned` is what lets `sync` touch only entries this tool created, and remove them when they leave the source.
- **Write-ahead**: before an op runs, its entry (with `before` captured and any backup file written and fsynced) is saved with status `planned`. After it runs and is verified, it becomes `done`. A crash therefore leaves a journal that still knows how to put things back.
- **Verification**: after each write the engine re-reads the result and compares it with the expected `after`; a mismatch triggers an automatic revert of the entries already done in this run.
- **Safety snapshot**: `init` first records a `snapshot` operation holding copies of every config file of every detected tool, including those it will not change. `undo` of `init` restores them.
- **Secrets**: backups can contain secrets, so they live in the `0700` state directory, are never printed, and plan or status output goes through a mask.

## The `undo` guarantee (what we promise)

`all-agents undo` reverses the most recent operation that is not undone. After a successful undo, every path that operation changed is back in its recorded `before` state: same bytes, same permission bits, same link-or-file kind. Entries are reversed in the opposite order of execution. The undo is itself an operation in the journal, so it can be undone.

What we do not promise: reverting changes made by other programs after our operation; undoing the side effects of an external CLI beyond running its recorded inverse.

## Drift rules (Accepted)

A path has drifted when its current state differs from the entry's recorded `after`: the tool or the user changed it since.

- `undo` first checks all entries (preflight). If any path has drifted, it changes nothing, lists the drifted paths and exits non-zero.
- `undo --force` proceeds. Before overwriting a drifted path it saves the current content as a new journal operation, so even a forced undo is reversible.

## Undo is a stack (Accepted)

- `undo` reverts only the latest operation that is not undone. Run it again to revert the one before, and so on. There is no way to jump to a chosen operation id.
- `undo --list` shows the operations that can still be undone, newest first. It is informational.
- Reason: operations build on each other. Reverting an older operation directly can break the ones after it (for example a later operation edited the same file or linked into a folder the older one created), and the result is a state no operation ever produced. A stack keeps every intermediate state one that really existed.
- Retention: keep the last 20 operations. Pruning removes the oldest first and never the latest. An operation that was pruned can no longer be undone.

## Reason

- One format means one `undo` path, one test suite for it, and every new adapter inherits the guarantee.
- A preflight check avoids half-undone states, which are worse than none.

## Open points

- Claude Code's MCP config is written through the tool's CLI (`remove` then `add`), which is not atomic. The journal must mark such an entry `partial` if the second step fails, and undo must tolerate a half state. Not designed in detail.
- Whether pruning runs automatically after each `sync` or only on demand (the limit of 20 is decided; the trigger is not).
- Cross-machine use of the journal is out of scope: it holds absolute paths for one machine.
- `copy_tree` before-state for large skill folders: storing a hash plus a copy may be expensive; decide when the skills adapter is built.
