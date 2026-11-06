"""The transitional command line of the ported core (reachable as `all-agents _legacy ...`).

Kept so the ported code stays runnable and testable until Phase 5 replaces it with the four public commands.

    status                                  read-only: server x tool table
    diff <server> | diff --all              read-only: one server across the tools / source vs every tool
    import [--write FILE] ...               draft the neutral source from the live configs
    apply [--apply] [--only K] ...          dry-run by default; --apply backs up, writes, verifies (K = mcp, skills, instructions)
    verify [--only K]                       exit 2 if anything drifted from the source
    check [--quiet] [--strict]              cheap drift check: exit 0 always (2 with --strict)
    rollback <backup-dir> [--force]         undo an apply
    prune [--older-than DAYS] [--apply]     delete old backup folders (dry-run by default; 14 days, newest of each kind kept)

Common flags: --home DIR (a fake home for tests), --json (status/diff), --source FILE, --skills-dir DIR.
Never prints env/header values or tokens. Exit codes: 0 ok, 1 error, 2 drift (verify).
"""

import argparse
import datetime
import difflib
import json
import sys
from pathlib import Path

from . import apply as applymod
from . import importer, instructions, locations, report, skills
from . import plan as planmod
from .parsers import TOOLS, load_all
from .source import Source, SourceError

KINDS = ("mcp", "skills")  # the default steps
ALL_KINDS = KINDS + (
    "instructions",
)  # instructions overwrite the global instruction files: only when named


def default_home():
    return Path.home()


def default_source(home):
    return locations.config_root(home) / "mcp.jsonc"


def default_skills_dir(home):
    return locations.config_root(home) / "skills"


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--home", default=argparse.SUPPRESS, help="home directory to read (default: the real home)"
    )
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output"
    )
    common.add_argument(
        "--source",
        default=argparse.SUPPRESS,
        help="neutral MCP source file (default: <config dir>/mcp.jsonc)",
    )
    common.add_argument(
        "--skills-dir",
        default=argparse.SUPPRESS,
        help="folder of your skills (default: <config dir>/skills)",
    )

    p = argparse.ArgumentParser(
        prog="all-agents _legacy", description=__doc__.split("\n")[0], parents=[common]
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "status",
        parents=[common],
        help="server x tool table: present / missing / differs, plus counts",
    )
    df = sub.add_parser(
        "diff", parents=[common], help="normalized field differences of one server across the tools"
    )
    df.add_argument("server", nargs="?")
    df.add_argument(
        "--all",
        action="store_true",
        help="compare the source with every tool (the apply plan, field by field)",
    )
    im = sub.add_parser(
        "import", parents=[common], help="draft the neutral source from the live configs"
    )
    im.add_argument(
        "--write",
        metavar="FILE",
        help="write the draft to FILE (refuses to overwrite without --force)",
    )
    im.add_argument("--force", action="store_true")
    im.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME",
        help="leave this server out of the source",
    )
    im.add_argument(
        "--retire",
        action="append",
        default=[],
        metavar="TOOL:NAME",
        help="leave out and list under retired (apply removes it from that tool)",
    )
    im.add_argument(
        "--keep-live-env",
        action="append",
        default=[],
        metavar="TOOL",
        help="that tool keeps the env already in its file",
    )
    im.add_argument(
        "--auth",
        action="append",
        default=[],
        metavar="NAME=KIND",
        help="declare auth (oauth|none) for a server",
    )
    ap = sub.add_parser(
        "apply", parents=[common], help="write the source into the tools (dry-run unless --apply)"
    )
    ap.add_argument("--apply", action="store_true", help="really write (a backup is made first)")
    ap.add_argument(
        "--tool", help="comma list of tools (claude,codex,opencode,agy); filters the mcp step"
    )
    ap.add_argument("--server", help="comma list of servers")
    ap.add_argument(
        "--claude-bin", help="path of the claude CLI (default: PATH or ALL_AGENTS_CLAUDE_BIN)"
    )
    ap.add_argument(
        "--only",
        help="comma list of mcp,skills,instructions (default: mcp and skills; instructions only when named)",
    )
    ap.add_argument(
        "--clean-opencode-skills",
        action="store_true",
        help="skills: also remove old duplicate junctions in the OpenCode skills folder (never part of --safe)",
    )
    ap.add_argument(
        "--safe",
        action="store_true",
        help="skills only: create missing junctions and remove dead ones, nothing else (implies --apply)",
    )
    ap.add_argument(
        "--adopt",
        action="append",
        default=[],
        metavar="SKILL",
        help="skills: move this REAL skill folder into your skills folder (backup first) and link it back",
    )
    ap.add_argument(
        "--quiet", action="store_true", help="print nothing when there is nothing to do"
    )
    ap.add_argument(
        "--no-prune",
        action="store_true",
        help="skip the automatic prune of old backups after a successful --apply",
    )
    vf = sub.add_parser(
        "verify", parents=[common], help="exit 2 when something drifted from the source"
    )
    vf.add_argument(
        "--only", help="comma list of mcp,skills,instructions (default: mcp and skills)"
    )
    ck = sub.add_parser(
        "check", parents=[common], help="cheap drift check (reads only, exit 0 unless --strict)"
    )
    ck.add_argument("--quiet", action="store_true", help="print nothing when everything is in sync")
    ck.add_argument("--strict", action="store_true", help="exit 2 on drift and 1 on errors")
    pr = sub.add_parser(
        "prune", parents=[common], help="delete old backup folders (dry-run unless --apply)"
    )
    pr.add_argument(
        "--older-than",
        type=int,
        default=14,
        metavar="DAYS",
        help="delete backups older than DAYS (default 14); the newest backup of each kind is always kept",
    )
    pr.add_argument(
        "--apply", action="store_true", help="really delete (default: only list what would go)"
    )
    pr.add_argument("--today", default=None, help=argparse.SUPPRESS)  # YYYY-MM-DD, for tests
    rb = sub.add_parser("rollback", parents=[common], help="undo an apply from its backup folder")
    rb.add_argument("backup_dir")
    rb.add_argument(
        "--force", action="store_true", help="restore even if the file changed after the apply"
    )
    rb.add_argument(
        "--dry-run",
        action="store_true",
        help="skills journals only: list what the rollback would remove or skip",
    )
    rb.add_argument("--claude-bin")
    return p


def _csv(value):
    return [x for x in (value or "").split(",") if x] or None


def _check_kinds(value):
    kinds = _csv(value)
    bad = [k for k in (kinds or []) if k not in ALL_KINDS]
    if bad:
        raise SourceError(f"unknown --only value(s): {', '.join(bad)} (use {', '.join(ALL_KINDS)})")
    return set(kinds) if kinds else set(KINDS)


def _check_tools(names):
    bad = [t for t in (names or []) if t not in TOOLS]
    if bad:
        raise SourceError(f"unknown tool(s): {', '.join(bad)}")


def main(argv=None):
    args = build_parser().parse_args(argv)
    home = Path(getattr(args, "home", None) or default_home())
    as_json = getattr(args, "json", False)
    source_path = Path(getattr(args, "source", None) or default_source(home))
    args.skills_dir = Path(getattr(args, "skills_dir", None) or default_skills_dir(home))
    try:
        return dispatch(args, home, as_json, source_path)
    except (SourceError, applymod.ApplyError, importer.ImportError_, skills.SkillsError) as e:
        if getattr(args, "quiet", False):
            print(f"all-agents: {e}", file=sys.stderr)
            return 1 if getattr(args, "strict", False) else 0
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "quiet", False):
            print(f"all-agents: unexpected {type(e).__name__}", file=sys.stderr)
            return 1 if getattr(args, "strict", False) else 0
        raise


def _print(as_json, text, data):
    print(
        json.dumps(data, indent=2, ensure_ascii=False) if as_json else text,
        end="\n" if as_json else "",
    )


def _manifest_kind(backup_dir):
    try:
        return json.loads((Path(backup_dir) / "manifest.json").read_text(encoding="utf-8")).get(
            "kind"
        )
    except (OSError, ValueError):
        return None


def _apply_all(args, home, source_path):
    from . import prune

    kinds = _check_kinds(args.only)
    if args.safe:
        if args.only and kinds != {"skills"}:
            raise SourceError("--safe works with --only skills only")
        if args.adopt:
            raise SourceError("--safe cannot be combined with --adopt")
        kinds, args.apply = {"skills"}, True
    if (args.adopt or args.clean_opencode_skills) and "skills" not in kinds:
        raise SourceError("--adopt and --clean-opencode-skills belong to the skills step")
    if args.safe and args.clean_opencode_skills:
        raise SourceError("--safe cannot be combined with --clean-opencode-skills")
    tools, servers = _csv(args.tool), _csv(args.server)
    _check_tools(tools)
    rc = 0
    if "mcp" in kinds:
        rc = applymod.run_apply(
            home,
            Source(source_path),
            tools,
            set(servers) if servers else None,
            args.apply,
            args.claude_bin,
        )
    if "skills" in kinds and rc == 0:
        rc, _ = skills.run_apply(
            home,
            args.skills_dir,
            [],
            args.apply,
            args.safe,
            args.adopt,
            quiet=args.quiet,
            clean_opencode=args.clean_opencode_skills,
            agy_manifest=True,
        )
    if "instructions" in kinds and rc == 0:
        rc = instructions.run_apply(
            home, locations.config_root(home), args.apply, _instruction_tools(tools)
        )
    if (
        rc == 0 and args.apply and not args.safe and not args.no_prune
    ):  # --safe makes no backup folder, so no prune
        prune.auto(home, out=None if args.quiet else print, err=lambda m: print(m, file=sys.stderr))
    return rc


def _instruction_tools(tools):
    return [t for t in tools if t in instructions.TARGETS] if tools else None


def _verify_all(args, home, source_path):
    kinds = _check_kinds(args.only)
    rc = 0
    if "mcp" in kinds:
        rc = applymod.run_verify(home, Source(source_path))
        if rc == 1:
            return 1
    if "skills" in kinds:
        p = skills.build_plan(home, args.skills_dir, [], agy_manifest=True)
        print(skills.render_plan(p, "drift").rstrip("\n"))
        if p.ops or p.conflicts:
            rc = 2
    if "instructions" in kinds:
        rc = max(rc, instructions.verify(home, locations.config_root(home)))
    return rc


def _check(args, home, source_path):
    """Cheap drift summary (no CLI calls, no writes). Exit 0 unless --strict."""
    drift, notes = [], []
    try:
        plans = planmod.build_plan(Source(source_path), load_all(home), applymod.read_ledger(home))
        n = sum(len(p.ops) for p in plans.values())
        bad = [t for t, p in plans.items() if p.errors]
        if n:
            drift.append(f"mcp({n})")
        if bad:
            notes.append(f"mcp unreadable in {', '.join(bad)}")
    except (SourceError, applymod.ApplyError) as e:
        notes.append(f"mcp: {e}")
    sp = skills.build_plan(home, args.skills_dir, [], agy_manifest=True)
    if sp.ops or sp.conflicts:
        drift.append(f"skills({len(sp.ops)} to do, {len(sp.conflicts)} conflicts)")
    if drift:
        print("all-agents: drift in " + ", ".join(drift))
    for t in notes:
        print(f"all-agents: {t}", file=sys.stderr if args.quiet else sys.stdout)
    if not drift and not notes and not args.quiet:
        print("all-agents: in sync (mcp, skills)")
    if args.strict:
        return 2 if drift else (1 if notes else 0)
    return 0


def _prune(args, home, as_json):
    from . import prune

    today = None
    if args.today:
        try:
            today = datetime.date.fromisoformat(args.today)
        except ValueError:
            raise SourceError("--today must be YYYY-MM-DD")
    return prune.run_prune(home, args.older_than, args.apply, today, as_json)


def dispatch(args, home, as_json, source_path):
    if args.cmd == "rollback":
        kind = _manifest_kind(args.backup_dir)
        if getattr(args, "dry_run", False) and kind != "one-setup-all-agents/skills":
            raise SourceError("--dry-run works for skills journals only")
        if kind == "one-setup-all-agents/skills":
            return skills.rollback(args.backup_dir, dry_run=getattr(args, "dry_run", False))
        if kind == instructions.MANIFEST_KIND:
            return instructions.rollback(args.backup_dir, args.force)
        return applymod.run_rollback(args.backup_dir, args.claude_bin, args.force)
    if args.cmd == "prune":
        return _prune(args, home, as_json)
    if args.cmd == "apply":
        return _apply_all(args, home, source_path)
    if args.cmd == "verify":
        return _verify_all(args, home, source_path)
    if args.cmd == "check":
        return _check(args, home, source_path)

    configs = load_all(home)
    failed = any(c.error for c in configs.values())

    if args.cmd == "import":
        _check_tools(args.keep_live_env)
        auth = dict(a.split("=", 1) for a in args.auth)
        srv, retired, unmanaged, notes = importer.draft(
            configs, args.exclude, args.retire, args.keep_live_env, auth
        )
        text = importer.render(srv, retired, unmanaged, notes)
        for (
            name,
            keys,
        ) in (
            notes.items()
        ):  # names only; applying this draft would drop these from tools without keep_live_env
            print(
                f"note: {name}: env variable names not imported: {', '.join(keys)}", file=sys.stderr
            )
        if args.write:
            importer.write_new(args.write, text, args.force)
            Source(args.write)  # the draft must pass the same validation as a hand-written file
            print(
                f"wrote {args.write}: {len(srv)} server(s); retired {sum(map(len, retired.values()))}; "
                f"unmanaged {sum(map(len, unmanaged.values()))}"
            )
        else:
            print(text, end="")
        return 1 if failed else 0

    if args.cmd == "status":
        text, data = report.render_status(configs)
        data["home"] = str(home)
        _print(as_json, text, data)
        return 1 if failed else 0

    # diff
    if args.all:
        plans = planmod.build_plan(Source(source_path), configs, applymod.read_ledger(home))
        _print(
            as_json,
            planmod.render_plan(plans, "differences from the source")[0],
            planmod.plan_data(plans),
        )
        return 1 if failed else 0
    if not args.server:
        print("error: give a server name or --all", file=sys.stderr)
        return 1
    names = report.server_names(configs)
    if args.server not in names:
        close = difflib.get_close_matches(args.server, names, n=5)
        msg = f"unknown server '{args.server}'" + (
            f"; did you mean: {', '.join(close)}" if close else ""
        )
        print(json.dumps({"error": msg}) if as_json else msg, file=sys.stderr)
        return 1
    text, data = report.render_diff(configs, args.server)
    _print(as_json, text, data)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
