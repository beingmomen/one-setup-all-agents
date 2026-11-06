"""Facts about one project folder, read only: its per-tool config files, git state and owner.

Groundwork for project scope (v0.2). Nothing here writes. Git is run with `git -C <dir> ...` (plain read commands).
The owner of a project (personal or team) comes from the user's config (`config.toml`, table `[projects]`), never from
a built-in name: with no configuration every project is "team", the safe side (tracked files are never written).
"""

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from .parsers import ToolConfig, parse_text

# where each tool reads PROJECT-level MCP config, relative to the project folder
REL = {
    "claude": ".mcp.json",
    "codex": ".codex/config.toml",
    "opencode": "opencode.jsonc",
    "agy": ".agents/mcp_config.json",
}
OPENCODE_ALT = "opencode.json"
EMPTY_TEXT = {  # what a new file starts as, before the managed entries are added
    "claude": '{\n  "mcpServers": {\n  }\n}\n',
    "agy": '{\n  "mcpServers": {\n  }\n}\n',
    "opencode": '{\n  "mcp": {\n    "servers": {\n    }\n  }\n}\n',
    "codex": "",
}


class ProjectError(Exception):
    pass


def norm(p):
    """Absolute path with forward slashes and no trailing slash, comparable between spellings.

    Case is folded only on Windows, where paths are case-insensitive; on Linux a folder `Work` and `work` are different."""
    s = os.path.normpath(os.path.abspath(str(p))).replace("\\", "/")
    s = s.rstrip("/") if len(s) > 3 else s
    return s.lower() if os.name == "nt" else s


def rel_path(tool, root):
    """Project config file of `tool` under `root` (an existing opencode.json is used when opencode.jsonc is absent)."""
    root = Path(root)
    if (
        tool == "opencode"
        and not (root / REL["opencode"]).exists()
        and (root / OPENCODE_ALT).exists()
    ):
        return root / OPENCODE_ALT
    return root.joinpath(*REL[tool].split("/"))


# ---------------------------------------------------------------- reading a tool file of a project
def load_file(tool, path):
    """ToolConfig for one project-level file. A missing file is an EMPTY config (cfg.missing = True), not an error."""
    path = Path(path)
    cfg = ToolConfig(tool, path)
    cfg.missing = not path.exists()
    cfg.origin = {}
    if cfg.missing:
        return cfg
    cfg.exists = True
    try:
        text = path.read_bytes().decode("utf-8-sig")
        if not text.strip():
            return cfg
        cfg.servers, cfg.raw = parse_text(tool, text)
    except Exception as e:  # noqa: BLE001 - toml/json errors report line/column only, never the content
        cfg.error = f"cannot parse ({type(e).__name__}: {e})"
        cfg.servers, cfg.raw = {}, {}
    return cfg


def _claude_projects(home):
    """The `projects` table of ~/.claude.json ({} when the file is missing or unreadable). Read only."""
    p = Path(home) / ".claude.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}


def claude_local_keys(home, project):
    """{raw key: {name: native entry}} for every `projects` key of ~/.claude.json that names `project`.

    The file can hold the same folder under several spellings (drive-letter case, slash style), so more than one key may match.
    Read only. The entries may hold env values: callers keep them in memory and never print them."""
    want = norm(project)
    out = {}
    for key, val in _claude_projects(home).items():
        if isinstance(val, dict) and norm(key) == want:
            out[key] = {
                n: e for n, e in (val.get("mcpServers") or {}).items() if isinstance(e, dict)
            }
    return out


def claude_local_entries(home, project):
    """{name: native entry} of the Claude LOCAL-scope servers of `project` (~/.claude.json -> projects -> <path> -> mcpServers).

    Entries of every spelling of the key are merged (see `claude_local_conflicts` for names that differ between spellings).
    Read only. The entries may hold env values: callers keep them in memory and never print them."""
    out = {}
    for entries in claude_local_keys(home, project).values():
        out.update(entries)
    return out


def claude_local_conflicts(home, project):
    """Sorted server names defined DIFFERENTLY under two spellings of the same project key (which one Claude reads is ambiguous)."""
    seen, bad = {}, set()
    for entries in claude_local_keys(home, project).values():
        for n, e in entries.items():
            if n in seen and seen[n] != e:
                bad.add(n)
            seen.setdefault(n, e)
    return sorted(bad)


def is_repo_subfolder(project, root):
    """True when `project` is a folder INSIDE the git repo at `root` (not the repo root itself). Paths are compared resolved."""
    if root is None:
        return False
    try:
        return norm(Path(project).resolve()) != norm(Path(root).resolve())
    except OSError:
        return False


# ---------------------------------------------------------------- git
def git(root, *args, stdin=None, timeout=60):
    exe = shutil.which("git")
    if not exe:
        return 127, ""
    try:
        p = subprocess.run(
            [exe, "-C", str(root), *args],
            capture_output=True,
            input=stdin,
            timeout=timeout,
            **({} if stdin is not None else {"stdin": subprocess.DEVNULL}),
        )  # never inherit a hook's open stdin
    except (OSError, subprocess.SubprocessError):
        return 126, ""
    return p.returncode, p.stdout.decode("utf-8", errors="replace")


def git_root(path):
    """Nearest folder (the path itself or a parent) that has a .git entry, or None."""
    p = Path(path).resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


def remote_url(root):
    code, out = git(root, "config", "--get", "remote.origin.url")
    return out.strip() if code == 0 and out.strip() else None


def tracked_files(folder, rels):
    """Set of tracked file paths (relative to `folder`, '/') among/under the pathspecs `rels`."""
    code, out = git(folder, "ls-files", "-z", "--", *rels)
    return {x for x in out.split("\0") if x} if code == 0 else set()


def ignored_paths(folder, rels):
    """Subset of `rels` (relative to `folder`) that git ignores (.gitignore, info/exclude, global ignore)."""
    if not rels:
        return set()
    code, out = git(
        folder, "check-ignore", "-z", "--stdin", stdin=("\0".join(rels) + "\0").encode("utf-8")
    )
    return {x for x in out.split("\0") if x} if code in (0, 1) else set()


def exclude_file(root):
    """Absolute path of <repo>/.git/info/exclude for the repo that holds `root` (handles worktrees)."""
    code, out = git(root, "rev-parse", "--git-path", "info/exclude")
    if code != 0 or not out.strip():
        raise ProjectError(f"{root} is not inside a git repository")
    p = Path(out.strip())
    return (p if p.is_absolute() else Path(root) / p).resolve()


def codex_trust(home, project):
    """trust_level Codex has for `project` in ~/.codex/config.toml (None when absent). Codex reads project config only if 'trusted'."""
    import tomllib

    try:
        data = tomllib.loads((Path(home) / ".codex" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    want = norm(project)
    for k, v in (data.get("projects") or {}).items():
        if isinstance(v, dict) and norm(k) == want:
            return v.get("trust_level")
    return None


def file_state(folder, rel, tracked, ignored, exists):
    """'missing' | 'tracked' | 'ignored' | 'untracked' for one candidate path."""
    if not exists:
        return "missing"
    if rel in tracked or any(t.startswith(rel.rstrip("/") + "/") for t in tracked):
        return "tracked"
    return "ignored" if rel in ignored else "untracked"


# ---------------------------------------------------------------- owner
_REMOTE = re.compile(
    r"github(?:\.com|-[\w.-]+)[:/]+([^/\s]+)/", re.I
)  # also ssh host aliases (github-work, github-personal)


class OwnerConfig:
    """Who counts as "personal". Both lists are optional; empty means nothing is personal (everything is team).

    personal_owners: GitHub owners (users or organizations), compared lower-case.
    personal_paths: folders; a repo WITHOUT a remote inside one of them counts as personal."""

    def __init__(self, personal_owners=(), personal_paths=()):
        self.personal_owners = tuple(str(o).lower() for o in personal_owners)
        self.personal_paths = tuple(norm(p) for p in personal_paths)


DEFAULT_OWNERS = OwnerConfig()


def load_owner_config(config_dir):
    """OwnerConfig from `<config_dir>/config.toml` (`[projects]` personal_owners, personal_paths). Missing file = default."""
    path = Path(config_dir) / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return DEFAULT_OWNERS
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ProjectError(f"cannot read {path.name}: {type(e).__name__}")
    table = data.get("projects") or {}
    owners, paths = table.get("personal_owners", []), table.get("personal_paths", [])
    if (
        not all(isinstance(x, str) for x in [*owners, *paths])
        or not isinstance(owners, list)
        or not isinstance(paths, list)
    ):
        raise ProjectError(
            "config.toml [projects]: personal_owners and personal_paths must be lists of strings"
        )
    return OwnerConfig(owners, paths)


def owner_of(remote, folder=None, config=DEFAULT_OWNERS):
    """('personal'|'team', reason). Team = anything that is not configured as the user's own.

    Personal means: the remote is under a configured personal owner, or there is no remote and the folder lies inside a
    configured personal path. With the default (empty) config nothing is personal."""
    if remote:
        m = _REMOTE.search(remote)
        org = m.group(1).lower() if m else None
        if org and org in config.personal_owners:
            return "personal", f"remote owner {org} is configured as personal"
        if org:
            return "team", f"remote owner {org}"
        return "team", "remote is not on github.com"
    if folder is not None:
        here = norm(folder)
        for prefix in config.personal_paths:
            if here == prefix or here.startswith(prefix.rstrip("/") + "/"):
                return "personal", "no remote, folder is in a configured personal path"
    return "team", "no remote and the folder is not in a configured personal path"


def slug(path, base):
    """Source file name for a project: its path under `base`, lower-case, '/' -> '-'."""
    p = norm(path)
    b = norm(base)
    rel = p[len(b) :].strip("/") if p.startswith(b) else Path(path).name.lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", rel.lower().replace("/", "-")).strip("-")
    return s or "root"
