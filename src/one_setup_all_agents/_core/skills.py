"""Skills: keep the junction chain  <repo>/skills/<name>  ->  ~/.agents/skills/<name>  ->  ~/.claude/skills/<name>.

What it does :
  * a missing junction is created (agents -> repo for the repo's own skills, claude -> agents for everything in ~/.agents/skills),
  * a DEAD junction (its target no longer exists) is removed (and recreated when the target is valid),
  * a REAL directory where a junction should be is only REPORTED and skipped. Nothing is ever deleted with rmtree
    unless the user names the skill with --adopt; then the directory is first copied to a backup, moved into the repo
    and linked back,
  * skills that live only in ~/.agents/skills (installed by other means, e.g. `npx skills add`) get their claude link and
    are otherwise left alone, and ~/.claude/skills/{.trash,synced} are never touched.
`--safe` (used by the agy SessionStart hook) can only create missing junctions and remove dead ones, and keep agy's skills manifest
(below) in step.

agy (Antigravity CLI) does not follow directory junctions when it scans a skills folder (observed on Windows; unverified on Linux and macOS), so the skills
that reach ~/.agents/skills only as a junction (the repo's own skills) are invisible to it. It does read a manifest of extra skill
locations, ~/.gemini/config/skills.json, so all-agents lists each such skill there by its RESOLVED real path (agymanifest.py: merge that
keeps foreign entries, ledger-owned entries, atomic write, backup, rollback). A real folder in ~/.agents/skills is not listed (agy
reads it already, so a name never shows twice). Projects get the same in <project>/.agents/skills.json (projects.py).

Junctions are made with _winapi.CreateJunction on Windows (a symlink elsewhere, for the tests) and removed with
os.rmdir, which never touches the target's content. No secret is read or printed.
"""

import datetime
import hashlib
import json
import os
import shutil
from pathlib import Path

from . import agymanifest as am
from . import apply as ap
from . import fsutil, locations

RESERVED_CLAUDE = {
    ".trash",
    "synced",
}  # real dirs with these names under the Claude skills folder are not ours


class SkillsError(Exception):
    pass


# ---------------------------------------------------------------- junction primitives
def _strip(p):
    p = str(p)
    for pre in ("\\\\?\\UNC\\", "\\\\?\\", "\\??\\"):
        if p.startswith(pre):
            return ("\\\\" + p[len(pre) :]) if pre.endswith("UNC\\") else p[len(pre) :]
    return p


def is_link(p):
    p = str(p)
    if os.path.islink(p):
        return True
    return fsutil.is_junction(p)


def link_target(p):
    try:
        return _strip(os.readlink(str(p)))
    except (OSError, ValueError):
        return None


def norm(p):
    return os.path.normcase(os.path.abspath(_strip(p)))


def real(p):
    return os.path.normcase(os.path.realpath(str(p)))


def make_link(link, target, allow_dead=False):
    link, target = str(link), str(target)
    if os.name == "nt":
        if allow_dead and not os.path.exists(
            target
        ):  # _winapi.CreateJunction needs an existing target; mklink /J does not
            import subprocess

            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", link, target],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode != 0:
                raise OSError(f"mklink /J failed: {r.stdout.strip() or r.stderr.strip()}"[:200])
            return
        import _winapi

        _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))
    else:
        os.symlink(os.path.abspath(target), link, target_is_directory=True)


def remove_link(link):
    """Remove a junction/symlink only; refuses anything that is not a link. Never deletes content."""
    link = str(link)
    if not is_link(link):
        raise SkillsError(f"refusing to remove {link}: not a junction/link")
    try:
        os.rmdir(link)
    except OSError:
        os.unlink(link)


def classify(link, want):
    """('absent'|'ok'|'dead'|'elsewhere'|'dir'|'file', target) for the slot `link` that should lead to `want`."""
    link = str(link)
    if not os.path.lexists(link):
        return "absent", None
    if is_link(link):
        tgt = link_target(link)
        if not os.path.exists(link):
            return "dead", tgt
        return ("ok" if real(link) == real(want) else "elsewhere"), tgt
    if os.path.isdir(link):
        return "dir", None
    return "file", None


def _skill_dirs(root):
    """Names of alive directories (links followed) in `root`; dotted names are skipped."""
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        if not n.startswith(".") and os.path.isdir(os.path.join(root, n)):
            out.append(n)
    return out


def _entries(root):
    try:
        return sorted(os.listdir(root))
    except OSError:
        return []


# ---------------------------------------------------------------- the plan
class Op:
    def __init__(self, kind, layer, name, link, target=None, src=None, data=None):
        self.kind = kind  # create | remove_dead | remove_stray | adopt | manifest
        self.data = (
            data  # manifest: the planned merge (agymanifest.MPlan); never written to the journal
        )
        self.layer = layer  # agents | claude | agy
        self.name = name
        self.link = str(link)
        self.target = str(target) if target else None
        self.src = str(src) if src else None  # adopt: the real directory that moves into the repo

    def describe(self, home, repo):
        def short(p):
            p = str(p)
            for base, label in ((str(home), "~"), (str(repo.parent), "<repo>")):
                if norm(p).startswith(norm(base)):
                    return label + p[len(base) :].replace("\\", "/")
            return p.replace("\\", "/")

        if self.kind == "create":
            return f"create   {short(self.link)} -> {short(self.target)}"
        if self.kind == "remove_dead":
            return f"remove   {short(self.link)}   (dead: target {short(self.target) if self.target else '?'} is gone)"
        if self.kind == "remove_stray":
            return f"remove   {short(self.link)}   (old OpenCode junction -> {short(self.target)}; same skill is already served from ~/.agents/skills)"
        if self.kind == "manifest":
            return describe_manifest(self.data, short(self.link))
        return (
            f"adopt    {short(self.src)} -> {short(self.target)}   (backup first, then linked back)"
        )

    def to_dict(self):
        return {
            "op": self.kind,
            "layer": self.layer,
            "name": self.name,
            "link": self.link,
            "target": self.target,
            "src": self.src,
        }


class SkillsPlan:
    def __init__(self, home, repo, a_dir, c_dir):
        self.home, self.repo, self.a_dir, self.c_dir = (
            Path(home),
            Path(repo),
            Path(a_dir),
            Path(c_dir),
        )
        self.ops = []
        self.conflicts = []  # (name, text) left alone
        self.info = []

    @property
    def changed(self):
        return bool(self.ops)


def paths(home, repo):
    return Path(repo), Path(home) / ".agents" / "skills", Path(home) / ".claude" / "skills"


def describe_manifest(mp, where):
    names = lambda paths: ", ".join(os.path.basename(str(x).rstrip("/")) for x in paths)  # noqa: E731
    head = f"{'create ' if mp.kind == 'create' else 'update '}  {where}"
    parts = []
    if mp.add:
        parts.append(f"list {len(mp.add)} skill(s) agy cannot see on its own: {names(mp.add)}")
    if mp.remove:
        parts.append(
            f"drop {len(mp.remove)} entry(ies) all-agents wrote before: {names(mp.remove)}"
        )
    return head + "   (" + "; ".join(parts) + "; other entries stay as they are)"


def build_plan(
    home,
    repo,
    claude_exclude=(),
    adopt=(),
    clean_opencode=False,
    agy_manifest=False,
    agy_exclude=(),
):
    """Compare the three roots and return the operations that make the chain complete. Reads only.

    agy_manifest = also plan the entries of agy's skills.json for the skills agy cannot see through ~/.agents/skills."""
    repo, a_dir, c_dir = paths(home, repo)
    p = SkillsPlan(home, repo, a_dir, c_dir)
    if not repo.is_dir():
        p.conflicts.append(("(repo)", f"skills folder not found: {repo}"))
        return p
    exclude = set(claude_exclude) | RESERVED_CLAUDE
    repo_names = set()
    for n in _entries(repo):
        if n.startswith(".") or not (repo / n).is_dir():
            continue
        if (repo / n / "SKILL.md").is_file():
            repo_names.add(n)
        else:
            p.info.append(f"{n}: repo folder without SKILL.md is not published")

    moved_a, moved_c = set(), set()
    for n in adopt:
        a, c, r = a_dir / n, c_dir / n, repo / n
        if "/" in n or "\\" in n or n.startswith("."):
            p.conflicts.append((n, "adopt: not a plain skill name"))
        elif os.path.lexists(r):
            p.conflicts.append(
                (n, f"adopt: the repo already has '{n}'; resolve by hand (diff both, keep one)")
            )
        elif os.path.isdir(a) and not is_link(a) and (a / "SKILL.md").is_file():
            p.ops.append(Op("adopt", "agents", n, a, target=r, src=a))
            moved_a.add(n)
            repo_names.add(n)
        elif (
            os.path.isdir(c)
            and not is_link(c)
            and (c / "SKILL.md").is_file()
            and not os.path.lexists(a)
        ):
            p.ops.append(Op("adopt", "claude", n, c, target=r, src=c))
            moved_c.add(n)
            repo_names.add(n)
        else:
            p.conflicts.append(
                (
                    n,
                    "adopt: needs a real directory with SKILL.md in ~/.agents/skills or ~/.claude/skills, "
                    "and no clashing copy elsewhere",
                )
            )

    gone = set()  # dead links that this plan removes
    # layer 1: repo -> ~/.agents/skills
    for n in sorted(repo_names):
        link, want = a_dir / n, repo / n
        kind, tgt = ("absent", None) if n in moved_a else classify(link, want)
        if kind == "absent":
            p.ops.append(Op("create", "agents", n, link, want))
        elif kind == "dead":
            p.ops.append(Op("remove_dead", "agents", n, link, tgt))
            p.ops.append(Op("create", "agents", n, link, want))
        elif kind == "elsewhere":
            p.conflicts.append(
                (n, f"~/.agents/skills/{n} is a junction to {tgt}, not to the repo copy")
            )
        elif kind in ("dir", "file"):
            p.conflicts.append(
                (
                    n,
                    f"~/.agents/skills/{n} is a real {'directory' if kind == 'dir' else 'file'} "
                    f"but the repo has the skill: compare them and delete one by hand",
                )
            )
    # dead links in ~/.agents/skills whose repo skill is gone (or never existed)
    for n in _entries(a_dir):
        link = a_dir / n
        if n in repo_names or n.startswith(".") or not is_link(link):
            continue
        if not os.path.exists(link):
            p.ops.append(Op("remove_dead", "agents", n, link, link_target(link)))
            gone.add(n)

    # layer 2: ~/.agents/skills -> ~/.claude/skills
    alive = {n for n in _skill_dirs(a_dir) if n not in gone and n not in moved_a}
    alive |= {op.name for op in p.ops if op.kind == "create" and op.layer == "agents"}
    for n in sorted(alive - exclude):
        link, want = c_dir / n, a_dir / n
        kind, tgt = ("absent", None) if n in moved_c else classify(link, want)
        if kind == "dead" and tgt and norm(tgt) == norm(want):
            kind = "ok"  # points at the right place; the agents link behind it is (re)created by this plan
        if kind == "absent":
            p.ops.append(Op("create", "claude", n, link, want))
        elif kind == "dead":
            p.ops.append(Op("remove_dead", "claude", n, link, tgt))
            p.ops.append(Op("create", "claude", n, link, want))
        elif kind == "elsewhere":
            p.conflicts.append(
                (n, f"~/.claude/skills/{n} is a junction to {tgt}, not to ~/.agents/skills/{n}")
            )
        elif kind == "dir":
            p.conflicts.append(
                (
                    n,
                    f"~/.claude/skills/{n} is a real directory and ~/.agents/skills has the skill too: "
                    f"compare them and delete one by hand (a plain rmtree would delete it)",
                )
            )
        elif kind == "file":
            p.conflicts.append((n, f"~/.claude/skills/{n} is a file where a junction should be"))
    # strays in ~/.claude/skills
    for n in _entries(c_dir):
        if n in RESERVED_CLAUDE or n.startswith(".") or n in alive or n in moved_c:
            continue
        link = c_dir / n
        if is_link(link):
            if not os.path.exists(link):
                p.ops.append(Op("remove_dead", "claude", n, link, link_target(link)))
            else:
                p.info.append(
                    f"~/.claude/skills/{n}: junction to {link_target(link)} (not from ~/.agents/skills; left alone)"
                )
        elif os.path.isdir(link) and n not in exclude:
            p.conflicts.append(
                (
                    n,
                    f"~/.claude/skills/{n} is a real directory that no other tool sees; "
                    f"`apply --only skills --adopt {n}` moves it into the repo and links it everywhere",
                )
            )
    if agy_manifest:
        _plan_agy_manifest(p, home, repo, a_dir, alive, moved_a, set(agy_exclude))
    strays = opencode_strays(home)
    if clean_opencode:
        od = Path(home) / ".config" / "opencode" / "skills"
        for n in _entries(od):
            link = od / n
            if not is_link(link):
                p.info.append(f"~/.config/opencode/skills/{n}: not a junction (left alone)")
            elif not os.path.exists(link) or (
                os.path.exists(a_dir / n) and real(link) == real(a_dir / n)
            ):
                p.ops.append(Op("remove_stray", "opencode", n, link, link_target(link)))
            else:
                p.info.append(
                    f"~/.config/opencode/skills/{n}: junction to {link_target(link)}, not a duplicate of ~/.agents/skills (left alone)"
                )
    elif strays:
        p.info.append(
            f"~/.config/opencode/skills holds old junction(s) {', '.join(strays)} (duplicates of ~/.agents/skills; report only)"
        )
    return p


def _plan_agy_manifest(p, home, repo, a_dir, alive, moved_a, exclude):
    """Layer 3: one skills.json entry (resolved real path) per ~/.agents/skills junction that holds a SKILL.md. Real folders: none."""
    wanted = []
    for n in sorted(set(alive) | set(moved_a)):
        if n in exclude:
            continue
        link = a_dir / n
        created = next(
            (o for o in p.ops if o.kind == "create" and o.layer == "agents" and o.name == n), None
        )
        if n in moved_a:  # --adopt: a real folder now, the repo copy + junction after this plan
            real, probe = repo / n, link
        elif created is not None:
            real = probe = Path(created.target)
        elif is_link(link) and os.path.isdir(link):
            real = probe = Path(os.path.realpath(link))
        else:
            continue  # a real folder: agy reads it as it is
        if not (probe / "SKILL.md").is_file():
            continue
        wanted.append(am.fwd(os.path.realpath(real)))
    mp = am.plan_merge(am.global_path(home), None, wanted, ap.read_ledger_agy_skills(home))
    mp.home = Path(home)
    if mp.kind == "blocked":
        p.conflicts.append(("(agy skills.json)", f"BLOCKED: {mp.reason}"))
    elif mp.actionable:
        p.ops.append(Op("manifest", "agy", "skills.json", mp.path, data=mp))


def opencode_strays(home):
    """Junctions in ~/.config/opencode/skills (old, duplicated by ~/.agents/skills). Information only."""
    d = Path(home) / ".config" / "opencode" / "skills"
    return [n for n in _entries(d) if is_link(d / n)]


def render_plan(p, title="plan"):
    lines = [f"[skills] repo {p.repo}", f"         agents {p.a_dir}", f"         claude {p.c_dir}"]
    for op in p.ops:
        lines.append("  " + op.describe(p.home, p.repo))
    if not p.ops:
        lines.append("  no changes")
    for n, text in p.conflicts:
        lines.append(f"  CONFLICT (left alone) {n}: {text}")
    for text in p.info:
        lines.append(f"  info: {text}")
    lines.append("")
    lines.append(f"{title}: {len(p.ops)} change(s), {len(p.conflicts)} conflict(s)")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- executing
def _sha_tree(root):
    """{relative path: sha256} of every file under root (links are not followed)."""
    out = {}
    for dp, dns, fns in os.walk(root, followlinks=False):
        for f in fns:
            fp = os.path.join(dp, f)
            h = hashlib.sha256()
            with open(fp, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out[os.path.relpath(fp, root).replace("\\", "/")] = h.hexdigest()
    return out


def _ledger_set(home, owned):
    """Record the entry paths all-agents owns in agy's global skills.json (the ledger part "agy_skills") under the all-agents lock."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    ap.atomic_write(
        ap.ledger_path(home), ap.ledger_bytes(home, ts, agy_skills={"owned": list(owned)})
    )


def _do_manifest(op, rec, bdir):
    mp = op.data
    with ap.Lock(mp.home):
        old_owned = ap.read_ledger_agy_skills(mp.home)
        try:
            rec.update(am.write(mp, bdir))
        except am.ManifestError as e:
            raise SkillsError(str(e))
        rec["ledger_before"] = old_owned
        try:
            _ledger_set(mp.home, mp.owned)
        except Exception:
            am.undo(rec, bdir)
            raise
        rec["home"] = str(mp.home)


def _undo_manifest(rec, bdir):
    am.undo(rec, bdir)
    if rec.get("home"):
        with ap.Lock(Path(rec["home"])):
            _ledger_set(Path(rec["home"]), rec.get("ledger_before") or [])


def _do(op, bdir):
    """Run one op with its precondition re-checked. Returns a manifest record."""
    rec = op.to_dict()
    if op.kind == "create":
        if os.path.lexists(op.link):
            raise SkillsError(f"{op.link} appeared since the plan was made; run again")
        if not os.path.isdir(op.target):
            raise SkillsError(f"target {op.target} is not a directory")
        Path(op.link).parent.mkdir(parents=True, exist_ok=True)
        make_link(op.link, op.target)
        if not os.path.isdir(op.link):
            remove_link(op.link)
            raise SkillsError(f"the new link {op.link} does not resolve")
    elif op.kind == "remove_dead":
        if not is_link(op.link) or os.path.exists(op.link):
            raise SkillsError(f"{op.link} is no longer a dead link; run again")
        rec["target"] = link_target(op.link)
        remove_link(op.link)
    elif op.kind == "remove_stray":
        if not is_link(op.link):
            raise SkillsError(f"{op.link} is no longer a junction; run again")
        rec["target"] = link_target(op.link)
        remove_link(op.link)
    elif op.kind == "manifest":
        _do_manifest(op, rec, bdir)
    else:  # adopt
        src, dst = Path(op.src), Path(op.target)
        if is_link(src) or not src.is_dir() or os.path.lexists(dst):
            raise SkillsError(f"adopt {op.name}: the situation changed since the plan; run again")
        backup = Path(bdir) / "files" / "adopted" / op.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, backup, symlinks=True)
        before = _sha_tree(src)
        if _sha_tree(backup) != before:
            raise SkillsError(f"adopt {op.name}: the backup copy does not match; nothing moved")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, symlinks=True)
        if _sha_tree(dst) != before:
            shutil.rmtree(dst, ignore_errors=True)
            raise SkillsError(
                f"adopt {op.name}: the copy in the repo does not match; nothing moved"
            )
        shutil.rmtree(
            src
        )  # the only rmtree: a real directory the user named with --adopt, already copied twice and verified
        rec["backup"] = str(backup.relative_to(bdir))
    return rec


def _undo(rec, bdir):
    """Reverse one journal record. Returns None, or a text when the record was deliberately skipped (rollback reports it)."""
    kind, link, target = rec["op"], rec["link"], rec.get("target")
    if kind == "create":
        if is_link(link):
            remove_link(link)
    elif kind in ("remove_dead", "remove_stray"):
        if not os.path.lexists(link) and target:
            make_link(link, target, allow_dead=True)
    elif kind == "manifest":
        _undo_manifest(rec, bdir)
    elif kind != "adopt":
        return f"{kind} {rec.get('name')}: unknown record kind; left alone"
    else:
        src, dst = Path(rec["src"]), Path(target)
        if is_link(src):
            remove_link(src)
        if dst.is_dir() and not os.path.lexists(src):
            shutil.copytree(dst, src, symlinks=True)
            shutil.rmtree(dst)


def apply_plan(plan, home, safe, out=print, journal=None):
    """Run the plan. Rolls back what it did if one step fails. Returns the manifest path."""
    home = Path(home)
    ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    has_adopt = any(o.kind == "adopt" for o in plan.ops)
    if safe and (has_adopt or any(o.kind == "remove_stray" for o in plan.ops)):
        raise SkillsError("--safe cannot adopt or clean the OpenCode folder")
    if safe or journal:
        bdir = locations.skills_journal_dir(home) / ts
    else:
        bdir = locations.backups_dir(home) / f"backup-{ts}-skills"
    n = 1
    base = bdir
    while bdir.exists():
        n += 1
        bdir = Path(f"{base}-{n}")
    bdir.mkdir(parents=True)
    manifest = {
        "kind": "one-setup-all-agents/skills",
        "created": ts,
        "home": str(home),
        "repo": str(plan.repo),
        "status": "started",
        "ops": [],
    }
    mpath = bdir / "manifest.json"

    def save():
        mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    save()
    done = []
    try:
        for op in plan.ops:
            rec = _do(op, bdir)
            done.append(rec)
            manifest["ops"] = list(done)
            save()
    except Exception as e:  # noqa: BLE001 - reverse whatever ran, then report
        for rec in reversed(done):
            try:
                _undo(rec, bdir)
            except Exception:  # noqa: BLE001
                pass
        manifest["status"] = f"failed: {type(e).__name__}"
        save()
        raise SkillsError(f"{e} (the steps that had run were reversed; journal {mpath})")
    manifest["status"] = "complete"
    save()
    return mpath


def rollback(manifest_path, out=print, dry_run=False):
    """Undo a skills journal, newest record first. `dry_run` only reports what would happen. Returns 1 when a record was skipped."""
    mp = Path(manifest_path)
    if mp.is_dir():
        mp = mp / "manifest.json"
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SkillsError(f"{mp} is not a readable manifest")
    if manifest.get("kind") != "one-setup-all-agents/skills":
        raise SkillsError("not an all-agents skills manifest")
    skipped = 0
    for rec in reversed(manifest.get("ops", [])):
        if dry_run:
            out(f"would undo: {rec['op']} {rec['layer']}/{rec['name']}")
            continue
        note = _undo(rec, mp.parent)
        if note:
            skipped += 1
            out(f"SKIPPED: {note}")
        else:
            out(f"undone: {rec['op']} {rec['layer']}/{rec['name']}")
    out(
        ("dry-run: nothing was changed. " if dry_run else "")
        + f"skills rollback {'check' if dry_run else 'done'}"
        + (f"; {skipped} record(s) skipped" if skipped else "")
    )
    return 1 if skipped else 0


def run_apply(
    home,
    repo,
    claude_exclude,
    do_apply,
    safe=False,
    adopt=(),
    out=print,
    quiet=False,
    clean_opencode=False,
    agy_manifest=False,
    agy_exclude=(),
):
    """Plan and (with do_apply) run. Returns (exit code, plan)."""
    if safe and (adopt or clean_opencode):
        raise SkillsError("--safe cannot be combined with --adopt or --clean-opencode-skills")
    plan = build_plan(home, repo, claude_exclude, adopt, clean_opencode, agy_manifest, agy_exclude)
    if not quiet:
        out(render_plan(plan, "applying" if do_apply else "plan").rstrip("\n"))
    if not plan.ops or not do_apply:
        if plan.ops and not do_apply and not quiet:
            out(
                "dry-run: nothing was written. Add --apply to write (a journal/backup is made first)."
            )
        return 0, plan
    mpath = apply_plan(plan, home, safe, out)
    after = build_plan(
        home, repo, claude_exclude, agy_manifest=agy_manifest, agy_exclude=agy_exclude
    )
    if after.ops:
        raise SkillsError(
            "still not in sync after writing: "
            + ", ".join(f"{o.kind} {o.layer}/{o.name}" for o in after.ops)
        )
    if not quiet:
        out(f'[skills] applied and verified. To undo: rollback "{mpath.parent}"')
    return 0, plan
