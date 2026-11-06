"""`apply` (dry-run by default), `rollback` and `verify` for the MCP source.

Safety, in the style of publish.py:
  * dry-run is the default; --apply is needed to write anything,
  * every file is backed up under <state>/backups/backup-<ts>-sync/ before it is touched (+ manifest.json),
  * writes are atomic (temp file + os.replace) and refuse if the file changed since it was read,
  * the new text is parsed and compared with the plan in memory first, and re-read after writing;
    a failed check restores the file automatically,
  * Claude Code's file is changed only through its own CLI (`claude mcp add-json/remove -s user`),
  * a lock file stops two applies from running together.
No secret value is ever printed. Backups and the manifest hold the same data as the config files they copy.
"""

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..engine import atomic as _atomic
from . import jsonc, jsonc_edit, locations, toml_edit
from .mask import mask_text
from .parsers import TOOLS, load_all, parse_text
from .plan import build_plan, render_plan
from .report import _sig
from .source import placeholder_leak

LOCK_MAX_AGE = 300  # seconds
FILE_TOOLS = ("opencode", "codex", "agy")


class ApplyError(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def state_dir(home):
    return locations.state_root(home)


def ledger_path(home):
    return locations.ledger_file(home)


def read_ledger(home):
    p = ledger_path(home)
    if not p.exists():
        return {}
    try:
        return {
            t: list(v.get("servers", []))
            for t, v in json.loads(p.read_text(encoding="utf-8")).get("tools", {}).items()
        }
    except (ValueError, AttributeError):
        raise ApplyError(f"ledger {p} is unreadable; fix or remove it first")


def read_ledger_hooks(home):
    """{tool: {hook name: entry}} - what the hooks step wrote (kept in the same ledger file under "hooks")."""
    p = ledger_path(home)
    if not p.exists():
        return {}
    try:
        hooks = json.loads(p.read_text(encoding="utf-8")).get("hooks", {})
        return {t: dict(v) for t, v in hooks.items()}
    except (ValueError, AttributeError):
        raise ApplyError(f"ledger {p} is unreadable; fix or remove it first")


def read_ledger_projects(home):
    """{project name: entry} - what the projects step wrote (kept in the same ledger file under "projects")."""
    p = ledger_path(home)
    if not p.exists():
        return {}
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")).get("projects", {}))
    except (ValueError, AttributeError):
        raise ApplyError(f"ledger {p} is unreadable; fix or remove it first")


def read_ledger_agy_skills(home):
    """Entry paths all-agents wrote into agy's global skills.json (kept in the same ledger file under "agy_skills")."""
    p = ledger_path(home)
    if not p.exists():
        return []
    try:
        owned = (json.loads(p.read_text(encoding="utf-8")).get("agy_skills") or {}).get("owned", [])
        return [str(x) for x in owned]
    except (ValueError, AttributeError, TypeError):
        raise ApplyError(f"ledger {p} is unreadable; fix or remove it first")


def ledger_bytes(
    home, ts, tools=None, hooks=None, projects=None, permissions=None, memory=None, agy_skills=None
):
    """New ledger content: replaces the part given, keeps the others (servers, hooks, projects, permissions, memory, agy_skills) as they are on disk."""
    p = ledger_path(home)
    old = {}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            raise ApplyError(f"ledger {p} is unreadable; fix or remove it first")
    data = {"version": 1, "updated": ts, "tools": old.get("tools", {}) if tools is None else tools}
    h = old.get("hooks", {}) if hooks is None else hooks
    if h:
        data["hooks"] = h
    pr = old.get("projects", {}) if projects is None else projects
    if pr:
        data["projects"] = pr
    pm = old.get("permissions", {}) if permissions is None else permissions
    if pm:
        data["permissions"] = pm
    mm = old.get("memory", {}) if memory is None else memory
    if mm:
        data["memory"] = mm
    ag = old.get("agy_skills", {}) if agy_skills is None else agy_skills
    if ag and ag.get("owned"):
        data["agy_skills"] = ag
    return json.dumps(data, indent=2).encode("utf-8")


def atomic_write(path, data, sensitive=False):
    """Crash-safe write that keeps the file's permissions and writes through symlinks (ADR 0006)."""
    try:
        _atomic.atomic_write(path, data, sensitive=sensitive)
    except _atomic.AtomicWriteError as e:
        raise ApplyError(str(e))


class Lock:
    def __init__(self, home):
        self.path = locations.lock_file(home)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {int(time.time())}".encode())
                os.close(fd)
                return self
            except FileExistsError:
                if time.time() - self.path.stat().st_mtime > LOCK_MAX_AGE:
                    self.path.unlink(missing_ok=True)  # stale
                    continue
                raise ApplyError(f"another run holds {self.path} (younger than {LOCK_MAX_AGE}s)")
        raise ApplyError(f"cannot take {self.path}")

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------- reading a file as text
class FileText:
    def __init__(self, path):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        self.sha = sha(self.raw)
        self.bom = self.raw.startswith(b"\xef\xbb\xbf")
        body = self.raw[3:] if self.bom else self.raw
        self.crlf = b"\r\n" in body
        self.text = body.decode("utf-8").replace("\r\n", "\n")

    def encode(self, text):
        out = text.replace("\n", "\r\n") if self.crlf else text
        return (b"\xef\xbb\xbf" if self.bom else b"") + out.encode("utf-8")


# ---------------------------------------------------------------- per-tool text edits
def _detect_indent(text):
    for line in text.split("\n")[1:6]:
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            return line[: len(line) - len(stripped)]
    return "  "


def edit_agy(text, plan):
    data = json.loads(text)
    servers = data.setdefault("mcpServers", {})
    for op in plan.ops:
        if op.kind == "remove":
            servers.pop(op.name, None)
        else:
            servers[op.name] = op.new_native
    out = json.dumps(data, indent=_detect_indent(text), ensure_ascii=False)
    return out + ("\n" if text.endswith("\n") else "")


def edit_opencode(text, plan, source):
    data = jsonc.loads(text)
    mcp = data.get("mcp") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        raise ApplyError('opencode: no "mcp" object in the file')
    path = ["mcp", "servers"] if isinstance(mcp.get("servers"), dict) else ["mcp"]
    removes = [o.name for o in plan.ops if o.kind == "remove"]
    adds = [
        (o.name, o.new_native, source.servers[o.name].description)
        for o in plan.ops
        if o.kind == "add"
    ]
    try:
        # an updated entry is edited key by key, so comments inside it and keys all-agents does not own stay as they are
        for o in (o for o in plan.ops if o.kind == "update"):
            old_e, new_e = o.old_native, o.new_native
            key_removes = [k for k in old_e if k not in new_e]
            key_replaces = {k: v for k, v in new_e.items() if k in old_e and old_e[k] != v}
            key_adds = [(k, v, None) for k, v in new_e.items() if k not in old_e]
            if key_removes or key_replaces or key_adds:
                text = jsonc_edit.edit_object(
                    text, path + [o.name], key_removes, key_replaces, key_adds
                )
        return (
            jsonc_edit.edit_object(text, path, removes, None, adds) if (removes or adds) else text
        )
    except jsonc_edit.EditError as e:
        raise ApplyError(f"opencode: cannot edit the file ({e})")


def edit_codex(text, plan, source):
    remove = [o.name for o in plan.ops if o.kind == "remove"]
    upserts = [
        (o.name, o.new_native, source.servers[o.name].description)
        for o in plan.ops
        if o.kind in ("add", "update")
    ]
    try:
        return toml_edit.edit(text, remove, upserts)
    except toml_edit.TomlEditError as e:
        raise ApplyError(f"codex: cannot edit the file ({e})")


def check_new_text(tool, new_text, plan):
    """Parse the new text and compare it with the plan: touched servers match, untouched ones are unchanged."""
    try:
        servers, _ = parse_text(tool, new_text)
    except Exception as e:  # noqa: BLE001 - any parse failure means the edit is bad
        raise ApplyError(f"{tool}: the edited text does not parse ({type(e).__name__}: {e})")
    before = plan.cfg.servers
    touched = {o.name for o in plan.ops}
    for op in plan.ops:
        leak = placeholder_leak(op.new_native, tool) if op.kind != "remove" else None
        if leak:  # last line of defence: a {{...}} the tool cannot expand is never written
            raise ApplyError(
                f"{tool}: '{op.name}' would be written with an unexpanded {leak} placeholder"
            )
        if op.kind == "remove":
            if op.name in servers:
                raise ApplyError(f"{tool}: '{op.name}' is still present after the edit")
        elif op.name not in servers or _sig(servers[op.name]) != _sig(op.new):
            raise ApplyError(f"{tool}: '{op.name}' does not match the plan after the edit")
    for name, s in before.items():
        if name in touched:
            continue
        if name not in servers or _sig(servers[name]) != _sig(s):
            raise ApplyError(f"{tool}: untouched server '{name}' changed by the edit")
    extra = set(servers) - set(before) - {o.name for o in plan.ops if o.kind == "add"}
    if extra:
        raise ApplyError(f"{tool}: unexpected servers appeared: {sorted(extra)}")


class Change:
    def __init__(self, tool, plan):
        self.tool = tool
        self.plan = plan
        self.path = plan.cfg.path
        self.ft = None
        self.new_bytes = None
        self.claude_calls = 0


def prepare(tool, plan, source):
    ch = Change(tool, plan)
    if not plan.ops:
        return ch
    if tool == "claude":
        ch.claude_calls = sum(2 if o.kind == "update" else 1 for o in plan.ops)
        return ch
    ch.ft = FileText(plan.cfg.path)
    new_text = {
        "agy": lambda: edit_agy(ch.ft.text, plan),
        "opencode": lambda: edit_opencode(ch.ft.text, plan, source),
        "codex": lambda: edit_codex(ch.ft.text, plan, source),
    }[tool]()
    check_new_text(tool, new_text, plan)
    ch.new_bytes = ch.ft.encode(new_text)
    return ch


# ---------------------------------------------------------------- Claude via its CLI
CLAUDE_BIN_ENV = "ALL_AGENTS_CLAUDE_BIN"


def find_claude(explicit=None):
    """Path of the `claude` command, or None. Order: the explicit path, $ALL_AGENTS_CLAUDE_BIN, then PATH.

    A path that is given but does not exist counts as not found (it is not silently replaced by PATH),
    so a typo in --claude-bin is reported instead of using a different install."""
    for candidate in (explicit, os.environ.get(CLAUDE_BIN_ENV)):
        if candidate:
            return str(candidate) if Path(candidate).is_file() else None
    return shutil.which("claude")


class ClaudeCli:
    def __init__(self, home, binary=None):
        self.bin = find_claude(binary)
        self.env = dict(os.environ, USERPROFILE=str(home), HOME=str(home))

    def available(self):
        return bool(self.bin)

    def run(self, *args, cwd=None):
        launch = (
            [sys.executable, "-I", self.bin] if str(self.bin).endswith(".py") else [self.bin]
        )  # a .py stand-in (tests)
        p = subprocess.run(
            [*launch, "mcp", *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=120,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
        if p.returncode != 0:
            raise ApplyError(
                f"claude {' '.join(args[:2])} failed (exit {p.returncode}): {mask_text((p.stderr or p.stdout).strip())[:160]}"
            )

    def add(self, name, entry, scope="user", cwd=None):
        self.run("add-json", name, json.dumps(entry, ensure_ascii=False), "-s", scope, cwd=cwd)

    def remove(self, name, scope="user", cwd=None):
        self.run("remove", name, "-s", scope, cwd=cwd)


def apply_claude(plan, cli, manifest_entry, home):
    """Run the plan through the CLI. Returns after success; on a failure the executed steps are reversed."""
    cfg_now = load_all_tool("claude", home)
    for op in plan.ops:  # concurrency guard: the entries must still look like they did when planned
        now = cfg_now.servers.get(op.name)
        if op.kind == "add" and now is not None:
            raise ApplyError(f"claude: '{op.name}' appeared since the plan was made; run again")
        if op.kind in ("update", "remove") and (now is None or _sig(now) != _sig(op.old)):
            raise ApplyError(f"claude: '{op.name}' changed since the plan was made; run again")
    done = []  # reverse steps: (action, name, previous_native)
    manifest_entry["ops"] = []
    try:
        for op in plan.ops:
            prev = cfg_now.raw.get(op.name)
            rec = {
                "op": op.kind,
                "name": op.name,
                "prev": prev,
                "new": op.new_native if op.kind != "remove" else None,
            }
            if op.kind in ("update", "remove"):
                cli.remove(op.name)
                done.append(("readd", op.name, prev))
            if op.kind in ("add", "update"):
                cli.add(op.name, op.new_native)
                done.append(("remove", op.name, None))
            manifest_entry["ops"].append(rec)
    except Exception:
        _reverse_claude(cli, done)
        raise


def _reverse_claude(cli, steps):
    for action, name, prev in reversed(steps):
        try:
            if action == "remove":
                cli.remove(name)
            elif prev is not None:
                cli.add(name, prev)
        except ApplyError:
            pass


def load_all_tool(tool, home):
    from .parsers import load_tool

    return load_tool(tool, home)


# ---------------------------------------------------------------- apply
def new_ledger(home, source, plans, old):
    ledger = {t: set(v) for t, v in old.items()}
    for tool, p in plans.items():
        if p.errors:
            continue
        cur = ledger.setdefault(tool, set())
        removed = {o.name for o in p.ops if o.kind == "remove"}
        cur -= removed
        cur |= {o.name for o in p.ops if o.kind in ("add", "update")} | set(p.same)
    return {t: sorted(v) for t, v in ledger.items() if v}


def run_apply(home, source, args_tools, args_servers, do_apply, claude_bin=None, out=print):
    home = Path(home)
    ledger = read_ledger(home)
    configs = load_all(home)
    plans = build_plan(source, configs, ledger, args_tools, args_servers)
    text, total = render_plan(plans, "plan" if not do_apply else "applying")
    out(text.rstrip("\n"))
    bad = [t for t, p in plans.items() if p.errors]
    if bad:
        raise ApplyError("cannot continue: " + ", ".join(f"{t}: {plans[t].errors[0]}" for t in bad))
    changes = {t: prepare(t, p, source) for t, p in plans.items()}
    cli = ClaudeCli(home, claude_bin)
    if plans.get("claude") and plans["claude"].ops and not cli.available():
        if (args_tools and "claude" in args_tools) or claude_bin or os.environ.get(CLAUDE_BIN_ENV):
            # asked for by name or by an explicit path (a typo must not be skipped silently): say so loudly
            raise ApplyError("the claude CLI was not found (use --claude-bin or put it on PATH)")
        out(
            "[claude] skipped: the claude CLI was not found (use --claude-bin or put it on PATH); the other tools go on"
        )
        plans = {t: p for t, p in plans.items() if t != "claude"}
        changes = {t: c for t, c in changes.items() if t != "claude"}
        text, total = render_plan(plans, "plan" if not do_apply else "applying")
    ledger_after = new_ledger(home, source, plans, ledger)
    ledger_changed = ledger_after != ledger
    if not total and not ledger_changed:
        out("no changes: every managed entry already matches the source")
        return 0
    for t, ch in changes.items():
        if ch.new_bytes is not None:
            out(
                f"[{t}] edited text validated in memory: {len(ch.ft.raw)} -> {len(ch.new_bytes)} bytes"
            )
        elif ch.claude_calls:
            out(f"[{t}] would run {ch.claude_calls} `claude mcp` call(s) (user scope)")
    if ledger_changed:
        out(
            f"ledger {ledger_path(home)}: {'create' if not ledger else 'update'} "
            f"({', '.join(f'{t} {len(v)}' for t, v in ledger_after.items())})"
        )
    if not do_apply:
        out("dry-run: nothing was written. Add --apply to write (a backup is made first).")
        return 0
    with Lock(home):
        return _write_all(home, source, plans, changes, cli, ledger, ledger_after, out)


def _write_all(home, source, plans, changes, cli, ledger, ledger_after, out):
    ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    bdir = locations.backups_dir(home) / f"backup-{ts}-sync"
    n = 1
    while bdir.exists():
        n += 1
        bdir = locations.backups_dir(home) / f"backup-{ts}-sync-{n}"
    (bdir / "files").mkdir(parents=True)
    manifest = {
        "kind": "one-setup-all-agents/apply",
        "created": ts,
        "home": str(home),
        "source": str(source.path),
        "note": "backups hold the same data as the config files they copy (may include secrets)",
        "status": "started",
        "tools": {},
        "ledger": None,
    }
    mpath = bdir / "manifest.json"

    def save():
        mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    for tool in TOOLS:  # backups first, for every tool that will change
        ch = changes.get(tool)
        if not ch or not ch.plan.ops:
            continue
        src = ch.path
        dst = bdir / "files" / tool / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        manifest["tools"][tool] = {
            "path": str(src),
            "backup": str(dst.relative_to(bdir)),
            "sha_before": sha(src.read_bytes()),
            "sha_after": None,
        }
    lp = ledger_path(home)
    lbackup = None
    if lp.exists():
        lbackup = bdir / "files" / "ledger" / lp.name
        lbackup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lp, lbackup)
    manifest["ledger"] = {
        "path": str(lp),
        "existed": lp.exists(),
        "backup": str(lbackup.relative_to(bdir)) if lbackup else None,
        "sha_after": None,
    }
    save()
    out(f"backup: {bdir}")
    try:
        for tool in TOOLS:
            ch = changes.get(tool)
            if not ch or not ch.plan.ops:
                continue
            entry = manifest["tools"][tool]
            if tool == "claude":
                apply_claude(ch.plan, cli, entry, home)
                after = load_all_tool("claude", home)
                _verify_after(plans["claude"], after, source, ledger)
                entry["sha_after"] = sha(after.path.read_bytes())
            else:
                current = sha(ch.path.read_bytes())
                if current != ch.ft.sha:
                    raise ApplyError(
                        f"{tool}: the file changed while all-agents was working; nothing written, run again"
                    )
                atomic_write(ch.path, ch.new_bytes)
                after = load_all_tool(tool, home)
                try:
                    _verify_after(plans[tool], after, source, ledger)
                except ApplyError:
                    atomic_write(ch.path, ch.ft.raw)  # automatic restore
                    raise
                entry["sha_after"] = sha(ch.new_bytes)
            save()
            out(f"[{tool}] applied and verified")
        if ledger_after != ledger:
            raw = ledger_bytes(home, ts, tools={t: {"servers": v} for t, v in ledger_after.items()})
            atomic_write(lp, raw)
            manifest["ledger"]["sha_after"] = sha(raw)
        manifest["status"] = "complete"
        save()
    except Exception as e:
        manifest["status"] = f"failed: {type(e).__name__}"
        save()
        out(f"FAILED: {mask_text(str(e))}")
        out(
            f"the failing tool was restored; earlier tools stay applied. To undo them: rollback {bdir}"
        )
        return 1
    out(f'done. To undo: rollback "{bdir}"')
    return 0


def _verify_after(tplan, cfg_after, source, ledger):
    if cfg_after.error:
        raise ApplyError(f"{tplan.tool}: cannot read the file after writing ({cfg_after.error})")
    redo = build_plan(
        source,
        {**{t: tplan.cfg for t in TOOLS}, tplan.tool: cfg_after},
        ledger,
        [tplan.tool],
        {o.name for o in tplan.ops},
    )
    left = redo[tplan.tool].ops
    if left:
        raise ApplyError(
            f"{tplan.tool}: still not in sync after writing: {', '.join(o.name for o in left)}"
        )


# ---------------------------------------------------------------- rollback
def run_rollback(backup_dir, claude_bin=None, force=False, out=print):
    bdir = Path(backup_dir)
    try:
        manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ApplyError(f"{bdir} has no readable manifest.json")
    if manifest.get("kind") != "one-setup-all-agents/apply":
        raise ApplyError("not an all-agents apply backup")
    home = Path(manifest["home"])
    with Lock(home):
        problems = []
        for tool in reversed(TOOLS):
            entry = manifest["tools"].get(tool)
            if not entry:
                continue
            path = Path(entry["path"])
            if entry["sha_after"] is None:
                out(f"[{tool}] was not changed by that run; skipped")
                continue
            if tool == "claude":
                cli = ClaudeCli(home, claude_bin)
                if not cli.available():
                    problems.append("claude CLI not found")
                    continue
                for rec in reversed(entry.get("ops", [])):
                    _undo_claude_op(cli, rec)
                out(f"[claude] reversed {len(entry.get('ops', []))} operation(s) through the CLI")
                continue
            current = sha(path.read_bytes()) if path.exists() else None
            if current != entry["sha_after"] and not force:
                problems.append(
                    f"{tool}: {path.name} changed after the apply; not restored (use --force to overwrite)"
                )
                continue
            atomic_write(path, (bdir / entry["backup"]).read_bytes())
            out(f"[{tool}] restored {path}")
        led = manifest.get("ledger") or {}
        if led.get("sha_after"):
            lp = Path(led["path"])
            if led.get("existed"):
                atomic_write(lp, (bdir / led["backup"]).read_bytes())
            else:
                lp.unlink(missing_ok=True)
            out(f"ledger restored ({'previous version' if led.get('existed') else 'removed'})")
    if problems:
        for p in problems:
            out("PROBLEM: " + p)
        return 1
    out("rollback done")
    return 0


def _undo_claude_op(cli, rec):
    if rec["op"] == "add":
        cli.remove(rec["name"])
        return
    try:
        cli.remove(rec["name"])  # an updated entry exists now; a removed one does not
    except ApplyError:
        pass
    if rec["prev"] is not None:
        cli.add(rec["name"], rec["prev"])


# ---------------------------------------------------------------- verify
def run_verify(home, source, tools=None, out=print):
    plans = build_plan(source, load_all(home), read_ledger(Path(home)), tools, None)
    text, total = render_plan(plans, "drift")
    out(text.rstrip("\n"))
    if any(p.errors for p in plans.values()):
        return 1
    return 2 if total else 0
