"""`prune`: delete old backup folders (<state>/backups/backup-*). Dry-run by default; `--apply` deletes for real.

Rules:
  * a backup is old when the date in its folder name (backup-YYYY-MM-DD[-HHMMSS]-<kind>[-N]) is more than --older-than days
    before today (default 14); a name without a readable date falls back to the folder's mtime,
  * the newest backup of each kind (the name suffix after the date and optional time, `-2`/`-3` counters stripped) is always kept,
  * a backup that is still the latest rollback point for something is kept: its manifest.json records a file with a `sha_after`
    and that file on disk still has exactly that hash (only the newest such backup per file counts); a backup whose folder name
    is still mentioned by the ledger (the legacy ledger in the state directory) or by another manifest is kept too; an unreadable manifest is
    kept (not judged),
  * deletion is Python only and never follows a junction or symlink: a link is removed itself, its target is left alone;
    manifest.json goes last, so a half-deleted backup still says what it was,
  * `apply` takes the same lock as the writing commands; nothing outside <state>/backups/backup-* is ever touched
    (no .bak files, no tool-owned backup folders).
Run quietly after every successful `apply --apply` / `projects apply --apply` (`auto`); it never fails the apply.
"""

import datetime
import json
import os
import re
import stat
from pathlib import Path

from . import apply as ap
from . import fsutil, locations
from .mask import mask_text

DEFAULT_DAYS = 14
NAME_RE = re.compile(r"^backup-(\d{4})-(\d{2})-(\d{2})(?:-(\d{6})(?=-|$))?(?:-(.*))?$")
COUNTER_RE = re.compile(r"-(\d+)$")
REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


class Backup:
    def __init__(self, path, today):
        self.path = Path(path)
        self.name = self.path.name
        self.dated = False
        m = NAME_RE.match(self.name)
        rest, counter = "", 1
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                t = m.group(4) or "000000"
                self.when = datetime.datetime(y, mo, d, int(t[:2]), int(t[2:4]), int(t[4:]))
                self.dated = True
                rest = m.group(5) or ""
            except ValueError:
                self.dated = False
        if not self.dated:
            try:
                self.when = datetime.datetime.fromtimestamp(os.lstat(self.path).st_mtime)
            except OSError:
                self.when = datetime.datetime.fromtimestamp(0)
            rest = self.name[len("backup-") :]
        c = COUNTER_RE.search(rest)
        if c:
            counter, rest = int(c.group(1)), rest[: c.start()]
        self.kind = rest or "(none)"
        self.counter = counter
        self.age_days = (today - self.when.date()).days
        self.size = 0
        self.action = "keep"
        self.reason = ""

    @property
    def order(self):
        return (self.when, self.counter, self.name)


class PruneError(ap.ApplyError):
    pass


# ---------------------------------------------------------------- links (never followed)
def is_reparse(p):
    p = str(p)
    if os.path.islink(p):
        return True
    if fsutil.is_junction(p):
        return True
    try:
        return bool(getattr(os.lstat(p), "st_file_attributes", 0) & REPARSE)
    except OSError:
        return False


def tree_size(path):
    """Bytes of the regular files under path; a link counts as 0 and is never entered."""
    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if is_reparse(e.path):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        try:
                            total += e.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
        except OSError:
            pass
    return total


def _remove_link(p):
    try:
        os.rmdir(p)  # a junction / directory link: removes the link only
    except OSError:
        os.unlink(p)  # a file link


def _unlink(p):
    try:
        os.unlink(p)
    except PermissionError:
        os.chmod(p, stat.S_IWRITE)
        os.unlink(p)


def _rmdir(p):
    try:
        os.rmdir(p)
    except PermissionError:
        os.chmod(p, stat.S_IWRITE)
        os.rmdir(p)


def remove_backup(path, agents_dir):
    """Delete one backup folder. Refuses anything that is not a direct child named backup-* of agents_dir."""
    path = Path(path)
    if path.parent != Path(agents_dir) or not path.name.startswith("backup-"):
        raise PruneError(
            f"refusing to delete {path}: not a backup-* folder directly under {agents_dir}"
        )
    _remove(str(path), top=True)


def _remove(p, top=False):
    if is_reparse(p):
        _remove_link(p)
        return
    deferred = None
    with os.scandir(p) as it:
        entries = list(it)
    for e in entries:
        if top and e.name == "manifest.json" and not is_reparse(e.path):
            deferred = e.path
            continue
        if is_reparse(e.path):
            _remove_link(e.path)
        elif e.is_dir(follow_symlinks=False):
            _remove(e.path)
        else:
            _unlink(e.path)
    if deferred:
        _unlink(deferred)
    _rmdir(p)


# ---------------------------------------------------------------- manifests: what is still a rollback point
def _walk_records(obj, found):
    """Every dict that carries both `path` and a non-empty `sha_after` (files, the ledger, exclude lines, agy rules ...)."""
    if isinstance(obj, dict):
        if isinstance(obj.get("path"), str) and obj.get("sha_after"):
            found.append((obj["path"], obj["sha_after"]))
        for v in obj.values():
            _walk_records(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_records(v, found)


def _current_sha(path, cache):
    key = os.path.normcase(os.path.normpath(path))
    if key not in cache:
        p = Path(path)
        try:
            cache[key] = ap.sha(p.read_bytes()) if p.is_file() else "deleted"
        except OSError:
            cache[key] = "unreadable"
    return cache[key]


def _protections(home, backups):
    """{backup name: reason} for the backups that must stay although they are old."""
    keep = {}
    texts = {}
    records = {}  # normalized path -> [(order, name, sha_after, shown path)]
    for b in backups:
        mp = b.path / "manifest.json"
        if not mp.is_file() or is_reparse(mp):
            continue
        try:
            raw = mp.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            keep[b.name] = "manifest unreadable"
            continue
        texts[b.name] = raw
        found = []
        _walk_records(data, found)
        for path, sha_after in found:
            key = os.path.normcase(os.path.normpath(path))
            records.setdefault(key, []).append((b.order, b.name, sha_after, path))
    cache = {}
    for key, recs in records.items():
        for _order, name, sha_after, shown in sorted(
            recs, reverse=True
        ):  # newest first: only the newest match counts
            if _current_sha(shown, cache) == sha_after:
                keep.setdefault(name, f"latest rollback point of {Path(shown).name}")
                break
    ledger = ap.ledger_path(home)
    sources = dict(texts)
    try:
        if ledger.is_file():
            sources["(ledger)"] = ledger.read_text(encoding="utf-8")
    except OSError:
        pass
    for b in backups:
        pat = re.compile(re.escape(b.name) + r"(?![\w-])")
        for src, text in sources.items():
            if src != b.name and pat.search(text):
                keep.setdefault(
                    b.name,
                    "named by the ledger"
                    if src == "(ledger)"
                    else f"named by the manifest of {src}",
                )
                break
    return keep


# ---------------------------------------------------------------- the plan
def list_backups(home, today):
    agents = locations.backups_dir(home)
    out = []
    if not agents.is_dir():
        return out
    for e in sorted(os.scandir(agents), key=lambda x: x.name):
        if not e.name.startswith("backup-"):
            continue
        if not (is_reparse(e.path) or e.is_dir(follow_symlinks=False)):
            continue  # a stray file named backup-*: not a backup folder, left alone
        out.append(Backup(e.path, today))
    return out


def plan(home, older_than=DEFAULT_DAYS, today=None):
    today = today or datetime.date.today()
    backups = list_backups(home, today)
    newest = {}
    for b in backups:
        if b.kind not in newest or b.order > newest[b.kind].order:
            newest[b.kind] = b
    protected = _protections(home, backups)
    for b in backups:
        if b.age_days <= older_than:
            b.reason = f"young ({b.age_days}d)"
        elif newest[b.kind] is b:
            b.reason = f"newest of kind {b.kind}"
        elif b.name in protected:
            b.reason = protected[b.name]
        else:
            b.action, b.reason = "delete", f"{b.age_days}d old, kind {b.kind}"
        if b.action == "delete" or not b.reason.startswith("young"):
            b.size = tree_size(b.path)
    return backups


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def render(backups, older_than, apply_mode):
    dele = [b for b in backups if b.action == "delete"]
    old_kept = [b for b in backups if b.action == "keep" and not b.reason.startswith("young")]
    lines = [
        f"prune: {len(backups)} backup folder(s); older than {older_than} day(s), newest of each kind kept"
    ]
    for b in dele:
        lines.append(
            f"  {'delete' if apply_mode else 'would delete'}  {b.name}  {human(b.size)}  ({b.reason})"
        )
    for b in old_kept:
        lines.append(f"  keep    {b.name}  {human(b.size)}  ({b.reason})")
    total = sum(b.size for b in dele)
    lines.append(
        f"  {len(dele)} to delete ({human(total)}), {len(backups) - len(dele)} kept"
        f" ({sum(1 for b in backups if b.reason.startswith('young'))} young, {len(old_kept)} old but protected)"
    )
    return lines, len(dele), total


def run_prune(home, older_than=DEFAULT_DAYS, do_apply=False, today=None, as_json=False, out=print):
    if older_than < 0:
        raise PruneError("--older-than must be 0 or more")
    home = Path(home)
    backups = plan(home, older_than, today)
    lines, n, total = render(backups, older_than, do_apply)
    if as_json:
        out(
            json.dumps(
                {
                    "older_than": older_than,
                    "apply": do_apply,
                    "backups": [
                        {
                            "name": b.name,
                            "kind": b.kind,
                            "age_days": b.age_days,
                            "bytes": b.size,
                            "action": b.action,
                            "reason": b.reason,
                        }
                        for b in backups
                    ],
                },
                indent=2,
            )
        )
        if not do_apply:
            return 0
    else:
        out("\n".join(lines))
    if not do_apply:
        if not as_json:
            out(
                "dry-run: nothing was deleted. Add --apply to delete."
                if n
                else "dry-run: nothing to delete."
            )
        return 0
    if not n:
        return 0
    return _delete(home, older_than, today, out)


def _delete(home, older_than, today, out):
    agents = locations.backups_dir(home)
    failed = []
    freed = deleted = 0
    with ap.Lock(home):
        backups = plan(home, older_than, today)  # re-decided under the lock
        for b in backups:
            if b.action != "delete":
                continue
            try:
                remove_backup(b.path, agents)
                deleted += 1
                freed += b.size
            except OSError as e:
                failed.append(f"{b.name}: {type(e).__name__}")
    out(f"pruned {deleted} backup folder(s), freed {human(freed)}")
    for f in failed:
        out(f"FAILED: {mask_text(f)}")
    return 1 if failed else 0


def auto(home, older_than=DEFAULT_DAYS, today=None, out=print, err=None):
    """After a successful apply: delete quietly, say one line only if something went. Never raises, never changes the apply's result."""
    try:
        lines = []
        run_prune(home, older_than, True, today, False, out=lines.append)
        done = next((x for x in lines if x.startswith("pruned ")), None)
        if done and out:
            out(f"auto-prune: {done}")
        bad = [x for x in lines if x.startswith("FAILED:")]
        if bad:
            raise PruneError(bad[0])
    except Exception as e:  # noqa: BLE001 - must never fail the apply
        msg = f"all-agents: auto-prune skipped ({type(e).__name__}: {mask_text(str(e))[:150]})"
        if err:
            err(msg)
        else:
            import sys

            print(msg, file=sys.stderr)
