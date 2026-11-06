#!/usr/bin/env python3
"""Fail when a file of this repository contains a forbidden pattern (a personal name, a client name, a real path...).

The pattern list is NOT stored in this repository: listing the names to keep out would publish them. Patterns come from

  * the file `.personal-patterns` at the repository root (git-ignored; one regular expression per line,
    blank lines and lines starting with `#` are ignored), and/or
  * the environment variable `PERSONAL_PATTERNS` (the same format, lines separated by newlines), which is how CI
    passes them from a secret, and/or
  * `--patterns-file PATH`.

Patterns are Python regular expressions, matched case-insensitively against every line of every text file and against
every file path. To allow one specific use (for example the maintainer's name inside the license), write the pattern so it
does not match it, using a negative lookahead: `acme-corp(?!-public)`.

What is scanned: files that git tracks plus files git does not ignore (`git ls-files --cached --others --exclude-standard`),
or, with `--staged`, only the staged versions of files (for a pre-commit hook). Without git, every file under the root
except `.git`. Binary files are skipped.

Output: `path:line: matches pattern #N` only. The matched text and the pattern itself are never printed, so a CI log
does not repeat what the guard protects.

Exit codes: 0 clean, 1 a match was found, 2 no patterns configured or a bad pattern (a guard that checks nothing must not pass).
Python standard library only.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS_FILE = ".personal-patterns"
ENV_NAME = "PERSONAL_PATTERNS"
MAX_BYTES = 2_000_000


def parse_patterns(text):
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_patterns(root, extra_file=None, environ=None):
    environ = os.environ if environ is None else environ
    patterns = []
    files = [Path(root) / PATTERNS_FILE]
    if extra_file:
        files.append(Path(extra_file))
    for f in files:
        if f.is_file():
            patterns += parse_patterns(f.read_text(encoding="utf-8-sig"))
    patterns += parse_patterns(environ.get(ENV_NAME, ""))
    compiled = []
    for i, p in enumerate(patterns, 1):
        try:
            compiled.append((i, re.compile(p, re.IGNORECASE)))
        except re.error:
            # never print the pattern itself
            raise ValueError(f"pattern #{i} is not a valid regular expression") from None
    return compiled


def git(root, *args):
    p = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, stdin=subprocess.DEVNULL
    )
    return p.returncode, p.stdout


def list_files(root, staged=False):
    """[(display path, bytes loader)] for the files to scan."""
    root = Path(root)
    code, out = git(root, "rev-parse", "--is-inside-work-tree")
    if code == 0 and out.strip() == b"true":
        if staged:
            code, names = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
            return [
                (n, _staged_loader(root, n))
                for n in names.decode("utf-8", "replace").split("\0")
                if n
            ]
        code, names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        paths = sorted({n for n in names.decode("utf-8", "replace").split("\0") if n})
        return [(n, _file_loader(root / n)) for n in paths if (root / n).is_file()]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            if name == PATTERNS_FILE:
                continue
            p = Path(dirpath) / name
            found.append((p.relative_to(root).as_posix(), _file_loader(p)))
    return sorted(found, key=lambda x: x[0])


def _file_loader(path):
    def load():
        try:
            return path.read_bytes()
        except OSError:
            return b""

    return load


def _staged_loader(root, name):
    def load():
        code, out = git(root, "show", f":{name}")
        return out if code == 0 else b""

    return load


def is_text(data):
    return b"\0" not in data[:4096]


def scan(files, patterns):
    """[(path, line number or 0, pattern number)] for every match, in order."""
    hits = []
    for name, load in files:
        for num, rx in patterns:
            if rx.search(name):
                hits.append((name, 0, num))
        data = load()
        if len(data) > MAX_BYTES or not is_text(data):
            continue
        text = data.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for num, rx in patterns:
                if rx.search(line):
                    hits.append((name, lineno, num))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="repository root (default: the current directory)")
    ap.add_argument("--patterns-file", help="an extra file of patterns")
    ap.add_argument(
        "--staged", action="store_true", help="scan only what is staged (pre-commit hook)"
    )
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        patterns = load_patterns(root, args.patterns_file)
    except ValueError as e:
        print(f"check_personal_data: {e}", file=sys.stderr)
        return 2
    if not patterns:
        print(
            f"check_personal_data: no patterns configured (create {PATTERNS_FILE} or set {ENV_NAME}); nothing was checked",
            file=sys.stderr,
        )
        return 2
    hits = scan(list_files(root, args.staged), patterns)
    for name, lineno, num in hits:
        where = f"{name}:{lineno}" if lineno else f"{name} (file name)"
        print(f"{where}: matches pattern #{num}")
    if hits:
        print(
            f"check_personal_data: {len(hits)} match(es); see the numbers in your pattern list",
            file=sys.stderr,
        )
        return 1
    print(f"check_personal_data: clean ({len(patterns)} pattern(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
