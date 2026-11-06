"""Edits of Codex's config.toml that touch only `[mcp_servers.<name>...]` tables.

* Entries that must change are removed from wherever they are and written inside a managed region
  (`# >>> one-setup-all-agents >>>` ... `# <<< one-setup-all-agents <<<`) at the end of the file.
* Everything else (other tables, comments, key order) stays byte for byte.
Works on text with "\\n" line endings (the caller converts CRLF).
"""

import json
import re

BEGIN = "# >>> one-setup-all-agents >>>"
END = "# <<< one-setup-all-agents <<<"
_HEADER = re.compile(r"^\s*(\[\[?)\s*(.+?)\s*\]\]?\s*(#.*)?$")
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


class TomlEditError(Exception):
    pass


def _key_path(raw):
    """Split a dotted TOML key (with quoted segments) into its parts."""
    parts, cur, i = [], "", 0
    while i < len(raw):
        c = raw[i]
        if c in "\"'":
            j = raw.find(c, i + 1)
            if c == '"':
                j = i + 1
                while j < len(raw) and raw[j] != '"':
                    j += 2 if raw[j] == "\\" else 1
            if j == -1 or j >= len(raw):
                raise TomlEditError("bad quoted key")
            cur += raw[i + 1 : j]
            i = j + 1
        elif c == ".":
            parts.append(cur.strip())
            cur = ""
            i += 1
        else:
            cur += c
            i += 1
    parts.append(cur.strip())
    return parts


def _toggle_multiline(line, state):
    """Track whether we are inside a multi-line string (''' or \"\"\") after this line."""
    i = 0
    while i < len(line):
        if state is None:
            for q in ('"""', "'''"):
                if line.startswith(q, i):
                    state = q
                    i += 3
                    break
            else:
                if line[i] == "#":
                    return state
                if line[i] == '"':  # a single-line basic string: skip to its end
                    i += 1
                    while i < len(line) and line[i] != '"':
                        i += 2 if line[i] == "\\" else 1
                elif line[i] == "'":
                    j = line.find("'", i + 1)
                    i = len(line) if j == -1 else j
                i += 1
        else:
            j = line.find(state, i)
            if j == -1:
                return state
            state = None
            i = j + 3
    return state


def _bracket_delta(line):
    """Net count of [ ] and { } opened on this line, ignoring strings and comments."""
    d, i = 0, 0
    while i < len(line):
        c = line[i]
        if c == "#":
            break
        if c in "\"'":
            j = i + 1
            while j < len(line) and line[j] != c:
                j += 2 if (c == '"' and line[j] == "\\") else 1
            i = j
        elif c in "[{":
            d += 1
        elif c in "]}":
            d -= 1
        i += 1
    return d


class Block:
    def __init__(self, header, path, lead, body):
        self.header, self.path, self.lead, self.body = (
            header,
            path,
            lead,
            body,
        )  # lines (without "\n")

    def lines(self):
        return self.lead + ([self.header] if self.header is not None else []) + self.body


def split_blocks(text):
    """[Block...]: the first has header None (the preamble). Trailing comment lines glued to the next header are its `lead`."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    blocks = [Block(None, None, [], [])]
    state = None
    depth = 0
    for line in lines:
        is_header = (
            state is None
            and depth == 0
            and _HEADER.match(line)
            and not line.lstrip().startswith("#")
        )
        if is_header:
            lead = []
            prev = blocks[-1].body
            while prev and prev[-1].lstrip().startswith("#"):
                lead.insert(0, prev.pop())
            blocks.append(Block(line, _key_path(_HEADER.match(line).group(2)), lead, []))
        else:
            blocks[-1].body.append(line)
        if not is_header and state is None:
            depth = max(0, depth + _bracket_delta(line))
        state = _toggle_multiline(line, state)
    return blocks


def join_blocks(blocks):
    out = []
    for b in blocks:
        out.extend(b.lines())
    return "\n".join(out) + ("\n" if out else "")


def server_of(block):
    p = block.path
    return p[1] if p and p[0] == "mcp_servers" and len(p) >= 2 else None


def check_layout(blocks, names):
    for b in blocks:
        if b.path == ["mcp_servers"]:
            raise TomlEditError(
                "a bare [mcp_servers] table is not supported; use [mcp_servers.<name>]"
            )


def remove_servers(blocks, names):
    names = set(names)
    return [b for b in blocks if b.header is None or server_of(b) not in names]


def split_region(text):
    """(before, region_body or None, after). Markers must be whole lines."""
    lines = text.split("\n")
    try:
        b = next(i for i, l in enumerate(lines) if l.strip() == BEGIN)
    except StopIteration:
        return text, None, ""
    try:
        e = next(i for i, l in enumerate(lines) if i > b and l.strip() == END)
    except StopIteration:
        raise TomlEditError("found the begin marker of the managed region but not the end marker")
    before = "\n".join(lines[:b]) + ("\n" if b else "")
    region = "\n".join(lines[b + 1 : e]) + ("\n" if e > b + 1 else "")
    after = "\n".join(lines[e + 1 :])
    return before, region, after


# ---------------------------------------------------------------- rendering
def _key(k):
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TomlEditError(f"cannot write a value of type {type(v).__name__}")


def render_server(name, entry, description=None):
    """Lines of `[mcp_servers.<name>]` (+ sub-tables for dict values) for a native Codex entry."""
    order = ["command", "args", "url", "enabled", "startup_timeout_sec", "env_vars"]
    keys = [k for k in order if k in entry] + [k for k in entry if k not in order]
    lines = []
    if description:
        lines += [f"# {d}" for d in str(description).splitlines()]
    lines.append(f"[mcp_servers.{_key(name)}]")
    subtables = []
    for k in keys:
        v = entry[k]
        if isinstance(v, dict):
            subtables.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_value(v)}")
    for k, v in subtables:
        lines.append("")
        lines.append(f"[mcp_servers.{_key(name)}.{_key(k)}]")
        for sk, sv in v.items():
            if isinstance(sv, (dict, list)) and not isinstance(sv, list):
                raise TomlEditError("nested tables deeper than one level are not supported")
            lines.append(f"{_key(sk)} = {_value(sv)}")
    return lines


SKILLS_CONFIG = ["skills", "config"]


def render_skill_disable(path, comment=None):
    """Lines of one `[[skills.config]]` table that turns a skill off (Codex: path + enabled = false)."""
    lines = [f"# {c}" for c in str(comment).splitlines()] if comment else []
    return lines + ["[[skills.config]]", f"path = {_value(path)}", "enabled = false"]


def render_trust(path, comment=None):
    """Lines of one Codex `[projects.'<path>']` table that trusts a folder (a literal-string key, like Codex writes them)."""
    if "'" in path or "\n" in path:
        raise TomlEditError("a trusted folder path cannot contain a quote or a newline")
    lines = [f"# {c}" for c in str(comment).splitlines()] if comment else []
    return lines + [f"[projects.'{path}']", 'trust_level = "trusted"']


def edit(text, remove, upserts, skill_disable=None, trust=None):
    """remove: server names to delete wherever they are; upserts: [(name, entry, description)] written into the region.

    skill_disable: None leaves the `[[skills.config]]` tables of the managed region alone; a list of (path, comment)
    makes the region hold exactly those tables (the ones it held before are dropped).
    trust: None leaves the `[projects.'...']` tables of the region alone; a list of (path, comment) makes the region hold exactly
    those trust tables (tables of that kind outside the region are never touched)."""
    before, region, after = split_region(text)
    names_out = set(remove) | {n for n, _, _ in upserts}
    pre = split_blocks(before)
    post = split_blocks(after) if after.strip() else []
    check_layout(pre + post, names_out)
    pre = remove_servers(pre, names_out)
    post = remove_servers(post, names_out)
    reg_blocks = split_blocks(region) if region is not None and region.strip() else []
    reg_blocks = remove_servers(reg_blocks, names_out)
    if skill_disable is not None:
        reg_blocks = [b for b in reg_blocks if b.path != SKILLS_CONFIG]
    if trust is not None:
        reg_blocks = [b for b in reg_blocks if not (b.path and b.path[0] == "projects")]
    kept_region = join_blocks(reg_blocks).strip("\n")
    new_parts = []
    for name, entry, desc in upserts:
        new_parts.append("\n".join(render_server(name, entry, desc)))
    for path, comment in skill_disable or []:
        new_parts.append("\n".join(render_skill_disable(path, comment)))
    for path, comment in trust or []:
        new_parts.append("\n".join(render_trust(path, comment)))
    region_body = "\n\n".join(([kept_region] if kept_region else []) + new_parts)
    out_before = join_blocks(pre).rstrip("\n")
    out_after = join_blocks(post).strip("\n") if post else ""
    pieces = [out_before] if out_before else []
    if region_body:
        pieces.append(f"{BEGIN}\n{region_body}\n{END}")
    if out_after:
        pieces.append(out_after)
    return "\n\n".join(pieces) + "\n" if pieces else ""
