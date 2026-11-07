# 0010: Engine v1

Status: Accepted

This records the choices made while building `engine/` (plan item "Build the unified engine"): one operation model, one journal for backup and ledger, automatic revert when a write fails. It changes no rule of 0002, 0005, 0006, 0007, 0008 or 0009. Where it settles an open point of an accepted ADR, it says so. The legacy formats in `_core/` are not wired to this engine and stay until the adapters are ported, so "one journal instead of four formats" is true for the new engine only.

## Decision

**Journal and ledger (0005).**

- Layout and ids exactly as in 0005. Every journal and ledger file is written with `engine/atomic.py` (`0600` in `0700` folders on POSIX; an existing state directory is tightened to `0700`). On Windows no ACL is set: the folder inherits the user profile's.
- A path is described by a state dict with a `state` key: `absent`; `file` (`sha256`, `mode`); `symlink` (`target`); `dir_link` (`kind` `junction` or `symlink`, `target`); `copy_tree` (`tree_sha256`, `marker`). A before-state of a file or copied tree also has `backup` (`files/NNNN`); a copied tree has `modes` too.
- Entry fields beyond 0005: `description` (masked), `path`, `real_path` (an edit through a symlink changes the real file and leaves the link), `claims`, `releases`, `created_dirs` (folders the op created, removed again on revert when empty, so an undo leaves no new folders behind), `effect: "none"` (the atomic write was refused, nothing changed), `expected_after` (when verification failed), `result` (`exit_code`, for `run_cli`), `error` (masked), `reverted`. A `run_cli` entry has `before` and `after` of `null`: it has no path.
- Operation fields beyond 0005: `owned_delta`, `pending_revert`, `recovered`, `error`.
- Write-ahead order per op: capture `before`, write and fsync the backup payload, save the entry as `planned` (with the expected `after` computed up front: the transform is pure), execute, observe, compare, save `done`. The copy fallback saves the entry again before it starts, because the expected `after` changes from a link to a copy.
- Ownership: `owned` is updated only at the end of a successful run, from the ops that ran. The delta (`added`, `removed` triples) is saved in the journal first, then the ledger is written, then the operation is marked `done`. A revert applies the delta backwards, as set arithmetic: it is exact (an item already owned before a claim stays owned) and safe to repeat after a crash. A failed run never reaches the ledger.
- `apply_plan(env, plan, *, label, run_cli=None, lock_timeout=0.0)` returns the operation id, or `None` (nothing recorded) when the plan has nothing executable. Errors: `EngineLockedError`, `OpenOperationError`, `PlanError` (checked before anything is touched, for example a `RunCli` without an inverse), `ApplyError` (`reverted` says whether the automatic revert finished).

**Lock.** An operating-system lock on `<state>/lock`: `fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX, `msvcrt.locking(LK_NBLCK, 1)` on Windows. It fails at once with `EngineLockedError` naming the holder (or after `timeout` seconds), never waits forever. A stale lock cannot exist, because the OS drops the lock when the process ends, however it ends; the pid line in the file is informational (it sits after the locked byte so other processes can read it) and is overwritten by the next holder. A crashed run is not a lock problem; it is an `open` operation (below). Resolves the plan wording "stale lock handling".

**Junction (resolves the open point of 0007).** `_winapi.CreateJunction` is the method: in-process, no quoting, no subprocess, no administrator rights. It is undocumented in the standard library, so if the interpreter lacks it the same junction is made with `mklink /J` through `cmd.exe` (argv list). Both paths are tested on Windows. Linux and macOS use a symlink with an absolute target. Tested here on CPython 3.14 only; the CI matrix must cover 3.11 to 3.13.

**Links.**

- Something already at the link path is replaced or removed only if it is a link, junction or marked copy and the ledger records an item of the op as owned: removal needs `releases` and every released item owned; replacement needs at least one of `claims` and `releases` owned. A regular file, a real folder without a marker, and anything unowned are never touched. A marked copy whose content no longer matches its marker is refused (the user's edits would be lost). A path already in the wanted state is a no-op.
- The fallback (`allow_copy_fallback`) copies the folder next to the destination, writes the marker (`.all-agents-copy.json`: `source`, `tree_sha256`), and renames the copy into place, so the destination is absent or whole. Without the flag a link failure is an error and leaves nothing.
- Copy-tree hashing cost: the tree hash is a SHA-256 over sorted names and every file's bytes, streamed; the marker is excluded; modes are not hashed (they are stored in the before-state and restored). It is computed at capture, after the copy, and at each drift check. A before-state copied tree is stored as a full copy in the payload. This is accepted because copied trees exist only for the fallback and skill folders are small; a tree above 64 MiB or 5000 files, or one containing a link, is refused up front. Resolves the open point of 0005 and the hash rule of 0007.

**RunCli.**

- Executed through a runner, `argv -> exit code`. The default uses `subprocess.run` with an argv list, never `shell=True`, `shutil.which` to find the program (so a Windows `.cmd` shim works), a 120 s timeout, output captured and discarded, 127 for a missing program. Tests inject a fake and never start a program.
- "Verified" means: the runner returned 0. The engine cannot re-read a file it does not know, so there is no stronger check at this level; an adapter's own `read_*` after the apply is the stronger check, and the contract suite does it. The same holds for the inverse on a revert.
- The journal keeps `argv` in clear (the revert needs it); the folder is private. Everything the engine prints or raises goes through the mask, and shows at most the first three arguments.
- Revert of a `run_cli` entry: `done` runs its inverse (a failure stops the revert); `failed` with a non-zero exit code is not inverted (the command reported failure, so it is assumed to have had no effect); `planned`, or `failed` without an exit code, has an unknown outcome, so the inverse is tried and a failing inverse is tolerated and noted (`revert_note`).
- A remove-then-add pair (the 0005 open point): if the add fails, the add is not inverted and the remove is, which re-adds the old entry; the run is `failed` and the ledger is unchanged. No `partial` status is needed. If the inverse of the remove also fails, the operation stays `failed` with `pending_revert` and the journal shows which entries are put back. Resolves that open point.

**Crash recovery.** A crash leaves the operation `open` (or `failed` with `pending_revert` if an automatic revert was interrupted). The next `apply_plan` refuses with `OpenOperationError` and points at `apply.recover_operation(env)`, which reverts each such operation, newest first, and marks it `failed`, `recovered`. A `planned` entry may or may not have executed, so recovery accepts the path being in the `before` state, the expected `after` state, or (for a link, which is replaced in two steps) absent. Anything else is drift.

**Revert (`revert.revert_operation(env, op_id, *, run_cli=None, lock_timeout=0.0)`).** Takes the lock. Works on `done` (becomes `undone`), `open` and `failed` with `pending_revert` (become `failed`). Drift preflight is the only mode: if any path of an entry still to be reverted is in a state other than the recorded `after` (or `before`), nothing is changed and `RevertError` lists the paths. Entries are reversed newest first and marked `reverted` as they go, so an interrupted revert resumes. Restore replaces the path through `atomic.py` with the recorded mode, then checks the result. Not built, and not made harder: `--force`, the stack rule, `--list`, retention of 20, and recording the undo as an operation of its own (0005 says an undo is an operation; here it only sets the target to `undone`).

**Planning.** `build_plan(env, adapters, desired, *, scope=GLOBAL, kinds=None)`: `desired` is a `DesiredState` (`instructions`, `mcp`, `skills`, and `hooks`, `permissions`, `memory`) applied to every adapter, or a mapping of adapter id to `DesiredState` (per-tool selection of servers is the engine's job per 0009). `None` for a core kind means "not managed": it is skipped. The optional kinds are always asked so that `Unsupported` shows. `owned` per tool and kind is read from the ledger. The source loader for the config directory is not part of this item. `merge_links` collapses `Link` ops with the same path (0007): same target or both removals give one op with the claims and releases of all adapters (`allow_copy_fallback` stays on only if all allow it); a removal against a link keeps the link and moves the `releases` onto it (another tool still wants it); different targets keep the first and report a `Conflict`. `render_plan` prints what would run, then conflicts and unsupported kinds; file contents are never shown; every line goes through the mask, and an argument that is a JSON document is masked field by field.

**What the contract suite proves (0009 open point).** `tests/contract_applier.make_applier` returns the real engine. A pass shows, for the neutral example adapters in a temporary home: every apply is journaled and undone to the exact files, folders, links and `owned` table it started from; entries of the user and unowned entries are untouched; a conflict changes nothing. Run on Windows (junctions) and on Linux (symlinks, `0700`/`0600`, `flock`) through WSL, CPython 3.14. Not proven: macOS (CI only); CPython 3.11 to 3.13; any real tool (where it keeps files, whether it follows links, what its CLI does); the `undo` command (Phase 5); a crash at an arbitrary instruction (crashes are simulated at the points of the write-ahead order); Windows file symlinks; behavior on network filesystems.

## Reason

- Each choice closes a way an apply could leave a setup half changed or an undo could destroy the user's data: write-ahead plus recovery for crashes, drift preflight for third-party edits, ownership checks for links, the delta for the ledger.
- An OS lock needs no cleanup and has no stale state; a pid file needs liveness probing that differs per OS.
- `_winapi.CreateJunction` avoids spawning `cmd.exe` per skill and its quoting rules; the fallback costs one function.
- Exit code 0 is the only result every CLI offers; claiming more would be a guess.

## Open points

- Advisory locks on NFS or SMB are unverified; the state directory is machine-local by design.
- macOS, and CPython 3.11 to 3.13, are not run here.
- Windows ACLs on the state directory are not set.
- Backups are never pruned yet; retention of 20 is Phase 5.
- A link replace is two steps (remove, create), not atomic; recovery handles it, but a POSIX rename-into-place could remove the window.
- A hard crash during a copy can leave a `.all-agents-tmp-*` sibling folder; recovery does not delete it.
- A `RunCli` has no drift check, and an uncertain one (a crash) is resolved by trying its inverse.
- Two adapters editing the same file are not merged; only links are.
- `RunCli` verification is weak by nature; a per-op read-back hook is possible later.
- Adoption of an existing equal item into `owned` (0009 open point) is still without a carrier; `journal.set_owned` is the low-level function `init` can use.
- An op list that edits a path and also links over it is not analysed; ops run in order.
