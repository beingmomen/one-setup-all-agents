"""agy skills manifest: make the skills that agy cannot see visible through its own skills.json.

agy (Antigravity CLI) does not follow directory junctions when it scans a skills folder (observed on Windows; unverified on Linux and macOS), so a skill that
reaches a skills folder only as a junction is invisible to it, and so is a skill that lives only in `.claude/skills`. agy does read a
manifest of extra skill locations:

    ~/.gemini/config/skills.json         global   {"entries": [{"path": "C:/.../skills"}, ...]}
    <project>/.agents/skills.json        project  (paths may be relative to the project folder)

An entry may name a skills folder (real skill folders inside it are read) or ONE skill folder. An entry that names a folder holding
junctions does not help (live test: not followed), so all-agents writes one entry per skill, with the junction's RESOLVED real path.

Ownership: the legacy ledger (in the state directory) records the entry paths all-agents wrote. A merge keeps every other entry
untouched and in place (any other top-level key too), appends new entries at the end, removes only entries all-agents owns that are
no longer wanted (and that nobody customised), and never adds a skill that an existing foreign entry already covers (the same
path, or its containing folder), so a name never shows twice. Writes are atomic and backed up; `undo` restores the exact bytes when
the file is still ours, otherwise it takes all-agents's entries out and puts removed ones back (foreign edits survive).
Python standard library only. No secret is read.
"""

import hashlib
import json
import os
from pathlib import Path

from . import apply as ap

GLOBAL_REL = (".gemini", "config", "skills.json")
PROJECT_REL = (".agents", "skills.json")
BOM = b"\xef\xbb\xbf"


class ManifestError(Exception):
    pass


def global_path(home):
    return Path(home).joinpath(*GLOBAL_REL)


def project_path(root):
    return Path(root).joinpath(*PROJECT_REL)


def fwd(p):
    return str(p).replace("\\", "/")


def key(path, base=None):
    """Comparison key of an entry path: absolute, normalised, lower-case on Windows, forward slashes. Relative paths need `base`."""
    s = fwd(path)
    if not os.path.isabs(s):
        s = fwd(os.path.join(str(base), s)) if base is not None else s
    return fwd(os.path.normcase(os.path.normpath(s)))


def entry_path(entry):
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        return entry["path"]
    return None


def is_plain(entry):
    """True for an entry that only has `path` (the shape all-agents writes); one with extra keys was customised by the user."""
    return isinstance(entry, dict) and set(entry) == {"path"} and isinstance(entry["path"], str)


class Manifest:
    """One skills.json read from disk. `error` is set when it cannot be used (then nothing is ever written)."""

    def __init__(self, path):
        self.path = Path(path)
        self.exists = os.path.lexists(self.path)
        self.raw = None
        self.sha = None
        self.bom = False
        self.crlf = False
        self.data = {}
        self.entries = []
        self.error = None
        if not self.exists:
            return
        try:
            self.raw = self.path.read_bytes()
        except OSError as e:
            self.error = f"cannot read {self.path.name} ({type(e).__name__})"
            return
        self.sha = ap.sha(self.raw)
        self.bom = self.raw.startswith(BOM)
        body = self.raw[3:] if self.bom else self.raw
        self.crlf = b"\r\n" in body
        try:
            data = json.loads(body.decode("utf-8")) if body.strip() else {}
        except (ValueError, UnicodeDecodeError) as e:
            self.error = f"{self.path.name} is not valid JSON ({type(e).__name__}); left alone"
            return
        if not isinstance(data, dict):
            self.error = f"{self.path.name} is not a JSON object; left alone"
            return
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            self.error = f'{self.path.name}: "entries" is not a list; left alone'
            return
        self.data, self.entries = data, entries

    def render(self, entries):
        data = dict(self.data)
        data["entries"] = entries
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if self.crlf:
            text = text.replace("\n", "\r\n")
        return (BOM if self.bom else b"") + text.encode("utf-8")


class MPlan:
    """What to do with one manifest: kind = none | create | update | blocked. `owned` = entry paths all-agents owns afterwards."""

    def __init__(self, man, base):
        self.man, self.base = man, base
        self.kind = "none"
        self.reason = None
        self.add, self.remove = [], []
        self.new_entries = list(man.entries)
        self.new_bytes = None
        self.owned = []
        self.home = None  # set for the global manifest: its ledger part is written with the file

    @property
    def path(self):
        return self.man.path

    @property
    def actionable(self):
        return self.kind in ("create", "update")


def plan_merge(path, base, wanted, owned_prev, blocked=None):
    """Plan the manifest at `path`. `wanted` = ordered entry paths all-agents wants listed; `owned_prev` = entry paths it wrote before;
    `blocked` = a reason that forbids any write (e.g. a tracked team file); then the plan only reports."""
    man = Manifest(path)
    mp = MPlan(man, base)
    owned_keys = {key(p, base) for p in owned_prev}
    wanted_keys = {}
    for w in wanted:
        wanted_keys.setdefault(key(w, base), fwd(w))
    if man.error:
        mp.kind, mp.reason = "blocked", man.error
        return mp
    # foreign = every entry that is not a plain one of ours; they decide what is already covered
    foreign = set()
    for e in man.entries:
        p = entry_path(e)
        if p is not None and not (is_plain(e) and key(p, base) in owned_keys):
            foreign.add(key(p, base))

    def covered(k):
        return k in foreign or key(os.path.dirname(k)) in foreign

    kept, present, removed = [], set(), []
    for e in man.entries:
        p = entry_path(e)
        k = key(p, base) if p is not None else None
        if p is not None and is_plain(e) and k in owned_keys:
            if k in wanted_keys and k not in present and not covered(k):
                kept.append(e)
                present.add(k)
            else:
                removed.append(p)
        else:
            kept.append(e)
    added = [w for k, w in wanted_keys.items() if k not in present and not covered(k)]
    mp.add, mp.remove = added, removed
    mp.new_entries = kept + [{"path": w} for w in added]
    mp.owned = sorted(
        {e["path"] for e in kept if is_plain(e) and key(e["path"], base) in present} | set(added),
        key=str.lower,
    )
    if blocked:
        mp.kind, mp.reason = "blocked", blocked
        return mp
    if mp.new_entries == man.entries:
        return mp
    mp.kind = "update" if man.exists else "create"
    mp.new_bytes = man.render(mp.new_entries)
    return mp


# ---------------------------------------------------------------- writing / undoing
def write(mp, bdir=None, label="manifest"):
    """Write the planned manifest atomically after re-checking that the file is still what the plan saw. Returns the record for undo."""
    path = mp.path
    now = path.read_bytes() if os.path.lexists(path) else None
    if (now is None) == mp.man.exists or (now is not None and ap.sha(now) != mp.man.sha):
        raise ManifestError(
            f"{path.name} changed while all-agents was working; nothing written, run again"
        )
    rec = {
        "path": str(path),
        "base": str(mp.base) if mp.base is not None else None,
        "created": now is None,
        "sha_before": None if now is None else ap.sha(now),
        "sha_after": None,
        "backup": None,
        "added": list(mp.add),
        "removed": [e for e in mp.man.entries if entry_path(e) in set(mp.remove)],
    }
    if now is not None and bdir is not None:
        tag = hashlib.sha1(fwd(path).lower().encode("utf-8")).hexdigest()[:8]
        dst = Path(bdir) / "files" / label / f"{tag}-{path.name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(now)
        rec["backup"] = str(dst.relative_to(bdir))
    ap.atomic_write(path, mp.new_bytes)
    rec["sha_after"] = ap.sha(mp.new_bytes)
    after = Manifest(path)
    if after.error or after.entries != mp.new_entries:
        undo(rec, bdir)
        raise ManifestError(f"{path.name} does not read back as planned; put back as it was")
    return rec


def undo(rec, bdir=None):
    """Reverse `write`. Exact restore when the file is still what all-agents wrote, otherwise an inverse merge (foreign edits survive)."""
    path = Path(rec["path"])
    if not os.path.lexists(path) or rec.get("sha_after") is None:
        return
    man = Manifest(path)
    if man.error:
        raise ManifestError(man.error)
    if man.sha == rec["sha_after"]:
        if rec["created"]:
            path.unlink()
            return
        if rec.get("backup") and bdir is not None and (Path(bdir) / rec["backup"]).is_file():
            ap.atomic_write(path, (Path(bdir) / rec["backup"]).read_bytes())
            return
    base = rec.get("base")
    gone = {key(p, base) for p in rec.get("added", [])}
    entries = [e for e in man.entries if not (is_plain(e) and key(entry_path(e), base) in gone)]
    have = {key(entry_path(e), base) for e in entries if entry_path(e) is not None}
    for e in rec.get("removed", []):
        if key(entry_path(e), base) not in have:
            entries.append(e)
    if entries == man.entries:
        return
    ap.atomic_write(path, man.render(entries))


# ---------------------------------------------------------------- what to list
def resolved_dir(link):
    """Real path of a live junction/symlink folder holding SKILL.md, or None."""
    link = str(link)
    if not os.path.isdir(link):
        return None
    real = os.path.realpath(link)
    return real if os.path.isfile(os.path.join(real, "SKILL.md")) else None


def rel_or_abs(real, root):
    """Entry text for a resolved skill folder: relative to the project when it lies inside it, else absolute (forward slashes)."""
    try:
        rel = os.path.relpath(real, os.path.realpath(str(root)))
    except ValueError:  # another drive
        return fwd(real)
    return fwd(real) if rel.startswith("..") else fwd(rel)
