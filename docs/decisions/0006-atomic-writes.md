# 0006: Atomic writes, permissions and symlinks

Status: Accepted

## Context

A common atomic-write recipe (temporary file, fsync, `os.replace`) is crash-safe but, on POSIX, silently loses the target's permission bits (a `0600` file becomes the umask default) and replaces a symlink with a regular file. Users who keep their dotfiles in a repository and link them into place would have their links destroyed, and a private config could become world-readable. A tool that edits real config files must not do either.

## Decision

All file writes go through `engine/atomic.py`. Adapters never call it (0008).

1. **Same-directory temp file.** Create the temporary file in the real target's directory so `os.replace` stays on one filesystem and is atomic. Never in a system temp directory.
2. **Follow symlinks.** If the target path is a symlink, resolve it and write the real file; the link stays a link. A dangling symlink is an error, not a reason to create the target.
3. **Preserve permissions.** If the target exists, copy its mode bits onto the temp file before the replace. If it does not exist, use the default for new files, and `0600` when the op is marked `sensitive` (files known to hold credentials).
4. **Preserve text conventions.** Keep the existing file's line endings (LF or CRLF) and UTF-8 BOM. Edits are minimal and textual, so comments and formatting survive.
5. **Durability.** Flush and `fsync` the temp file, `os.replace`, then on POSIX `fsync` the directory. Skip the directory fsync on Windows.
6. **Windows.** `os.replace` is atomic on one volume. Retry a few times with a short backoff on `PermissionError`, because antivirus and indexers briefly hold files. Give up with a clear error; do not fall back to a non-atomic write.
7. **Optimistic concurrency.** Immediately before replacing, compare the file's current hash with the op's `expect_sha256`. A mismatch aborts that op (the tool changed the file since planning). A small race window remains and is documented, not hidden.
8. **Verify.** After the replace, re-read and compare with the intended bytes (0005).
9. **Files owned by a running tool** (for example a config file that live sessions also write) are not edited in place; the adapter plans a `RunCli` through the tool's own command instead.
10. **Cleanup.** The temp file is removed on any failure.

## Reason

Each rule closes a specific way an "atomic" write still damages a user's setup. The test suite for `atomic.py` must have one test per rule, run on all three OSes (POSIX-only rules are skipped on Windows, not removed).

## Open points

- Hard links are broken by a replace (the other name keeps the old content). Documented as a limit, not handled.
- Ownership (`chown`), extended attributes and macOS ACLs are not preserved. Unverified whether any supported tool relies on them.
- Behavior when the real target is on a read-only or network filesystem is not specified beyond "fail with a clear error".
- The exact retry count and backoff for Windows are placeholders to tune with real data.
