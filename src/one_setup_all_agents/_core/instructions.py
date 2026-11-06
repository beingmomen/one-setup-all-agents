"""Instructions from one source: build the global instruction file of each tool from a shared core plus an appendix.

Ported from the prototype's publish script, with everything specific to one person removed (mirror folder,
reference files, hard-link cleanup, probes of other tools). What remains is the idea and its safety rules:

Source (ADR 0003), in the config directory:
    instructions.md             the shared core
    instructions.<tool>.md      optional appendix for one tool (claude, codex, opencode, agy)

Generated (a header says so; never edit by hand):
    ~/.claude/CLAUDE.md             = header + core + appendix.claude
    ~/.codex/AGENTS.md              = header + core + appendix.codex
    ~/.config/opencode/AGENTS.md    = header + core + appendix.opencode
    ~/.gemini/GEMINI.md             = header + core + appendix.agy

Dry-run first; `apply` backs up every file it will change, writes atomically and verifies; `rollback` restores.
Files are written as UTF-8 with LF line endings and no BOM.
"""

import datetime
import hashlib
import json
from pathlib import Path

from . import apply as ap
from . import locations

CORE_NAME = "instructions.md"
TARGETS = {  # path of each tool's global instruction file, relative to the home directory
    "claude": (".claude", "CLAUDE.md"),
    "codex": (".codex", "AGENTS.md"),
    "opencode": (".config", "opencode", "AGENTS.md"),
    "agy": (".gemini", "GEMINI.md"),
}
LIMITS = {"agy": 24000}  # bytes, hard; Claude: 200 lines (warn)
BIDI = (
    {chr(c) for c in (0x200E, 0x200F, 0x061C)}
    | {chr(c) for c in range(0x202A, 0x202F)}
    | {chr(c) for c in range(0x2066, 0x206A)}
)
MANIFEST_KIND = "one-setup-all-agents/instructions"
GENERATED_MARK = "GENERATED FILE"


class InstructionsError(Exception):
    pass


def target_path(tool, home):
    return Path(home).joinpath(*TARGETS[tool])


def appendix_name(tool):
    return f"instructions.{tool}.md"


def _read(path):
    return Path(path).read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def sha(data, n=12):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:n]


def read_sources(config_dir, tools=None):
    """(core, {tool: appendix or ''}). The core is required; an appendix is optional."""
    config_dir = Path(config_dir)
    core_path = config_dir / CORE_NAME
    if not core_path.is_file():
        raise InstructionsError(f"missing source: {core_path.name} in {config_dir}")
    core = _read(core_path)
    appendices = {}
    for tool in tools or TARGETS:
        p = config_dir / appendix_name(tool)
        appendices[tool] = _read(p) if p.is_file() else ""
    return core, appendices


def build(tool, core, appendix):
    """The generated text for one tool."""
    stamp = sha(core + "\0" + appendix)
    source = CORE_NAME + (f" + {appendix_name(tool)}" if appendix else "")
    header = (
        f"<!-- {GENERATED_MARK} - do not edit by hand. Source: {source}. inputs-sha256: {stamp} -->"
    )
    body = core.rstrip("\n") + "\n"
    if appendix.strip():
        body += "\n" + appendix.rstrip("\n") + "\n"
    return header + "\n" + body


def lint(tool, text):
    """[(level, message)] with level ERROR (blocks apply) or WARN."""
    out = []
    size, lines = len(text.encode("utf-8")), text.count("\n")
    if any(c in BIDI for c in text):
        out.append(("ERROR", "hidden bidirectional control characters in the text"))
    if "\r" in text:
        out.append(("ERROR", "carriage returns in the text"))
    if tool in LIMITS and size > LIMITS[tool]:
        out.append(("ERROR", f"{size} bytes exceeds the {LIMITS[tool]}-byte limit"))
    if tool == "claude" and lines >= 200:
        out.append(("WARN", f"{lines} lines (the Claude docs recommend fewer than 200)"))
    return out


class Item:
    def __init__(self, tool, path, text, live):
        self.tool, self.path, self.text, self.live = tool, Path(path), text, live
        self.problems = lint(tool, text)

    @property
    def status(self):
        if self.live is None:
            return "new"
        return "identical" if self.live == self.text else "differs"

    @property
    def blocked(self):
        return any(level == "ERROR" for level, _ in self.problems)


def plan(home, config_dir, tools=None):
    core, appendices = read_sources(config_dir, tools)
    items = []
    for tool, appendix in appendices.items():
        path = target_path(tool, home)
        try:
            live = _read(path)
        except FileNotFoundError:
            live = None
        items.append(Item(tool, path, build(tool, core, appendix), live))
    return items


def render(items):
    lines = []
    for it in items:
        lines.append(f"{it.tool}: {it.path}  {len(it.text.encode('utf-8'))} bytes  [{it.status}]")
        for level, msg in it.problems:
            lines.append(f"    {level}: {msg}")
    return "\n".join(lines)


def run_apply(home, config_dir, do_apply, tools=None, out=print):
    """Dry-run unless do_apply. Returns 0 (ok / nothing to do) or 1 (refused or failed)."""
    items = plan(home, config_dir, tools)
    out(render(items))
    bad = [it for it in items if it.blocked]
    if bad:
        out(
            "REFUSED: "
            + ", ".join(it.tool for it in bad)
            + " has errors (above); nothing was written"
        )
        return 1
    todo = [it for it in items if it.status != "identical"]
    if not todo:
        out("no changes: every instruction file already matches the sources")
        return 0
    if not do_apply:
        out("dry-run: nothing was written. Add --apply to write (a backup is made first).")
        return 0
    home = Path(home)
    with ap.Lock(home):
        ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        bdir = locations.backups_dir(home) / f"backup-{ts}-instructions"
        n = 1
        while bdir.exists():
            n += 1
            bdir = locations.backups_dir(home) / f"backup-{ts}-instructions-{n}"
        (bdir / "files").mkdir(parents=True)
        manifest = {"kind": MANIFEST_KIND, "created": ts, "home": str(home), "entries": []}
        for i, it in enumerate(todo):
            entry = {
                "tool": it.tool,
                "path": str(it.path),
                "existed": it.live is not None,
                "sha_after": None,
            }
            if it.path.exists():
                data = it.path.read_bytes()
                (bdir / "files" / f"{i:02d}_{it.tool}").write_bytes(data)
                entry.update(backup=f"files/{i:02d}_{it.tool}", sha_before=ap.sha(data))
            manifest["entries"].append(entry)
        mpath = bdir / "manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        out(f"backup: {bdir}")
        for it, entry in zip(todo, manifest["entries"], strict=True):
            data = it.text.encode("utf-8")
            ap.atomic_write(it.path, data)
            if it.path.read_bytes() != data:
                raise InstructionsError(f"{it.tool}: the file does not read back as written")
            entry["sha_after"] = ap.sha(data)
            mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            out(f"[{it.tool}] wrote {it.path}")
    out(f'done. To undo: rollback "{bdir}"')
    return 0


def rollback(backup_dir, force=False, out=print):
    bdir = Path(backup_dir)
    try:
        manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InstructionsError(f"{bdir} has no readable manifest.json")
    if manifest.get("kind") != MANIFEST_KIND:
        raise InstructionsError("not an instructions backup")
    problems = 0
    for entry in reversed(manifest["entries"]):
        path = Path(entry["path"])
        if entry["sha_after"] is None:
            out(f"[{entry['tool']}] was not changed by that run; skipped")
            continue
        current = ap.sha(path.read_bytes()) if path.exists() else None
        if current != entry["sha_after"] and not force:
            out(
                f"PROBLEM: {entry['tool']}: {path.name} changed after the apply; not restored (use --force to overwrite)"
            )
            problems += 1
            continue
        if entry["existed"]:
            ap.atomic_write(path, (bdir / entry["backup"]).read_bytes())
            out(f"[{entry['tool']}] restored {path}")
        else:
            path.unlink(missing_ok=True)
            out(f"[{entry['tool']}] removed {path} (did not exist before)")
    if not problems:
        out("rollback done")
    return 1 if problems else 0


def verify(home, config_dir, tools=None, out=print):
    """0 when every file is a fresh build of the sources, 2 when something is stale or hand-edited, 1 on a source error."""
    items = plan(home, config_dir, tools)
    out(render(items))
    stale = [it for it in items if it.status != "identical" or it.blocked]
    return 2 if stale else 0
