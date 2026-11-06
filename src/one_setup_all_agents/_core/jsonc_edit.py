"""Surgical edits of one JSONC object (the `mcp.servers` block): remove, replace or add members and leave every
other byte, including the comments, exactly as it was.

Works on text with "\\n" line endings (the caller converts CRLF).
"""

import json


class EditError(Exception):
    pass


def _skip_string(text, i):
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        j += 1
    raise EditError("unterminated string")


def _skip_ws_comments(text, i):
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise EditError("unterminated block comment")
            i = end + 2
        else:
            break
    return i


def _skip_value(text, i):
    """Index just after the JSON value that starts at i."""
    c = text[i]
    if c == '"':
        return _skip_string(text, i)
    if c in "{[":
        depth = 0
        while i < len(text):
            c = text[i]
            if c == '"':
                i = _skip_string(text, i)
                continue
            if text.startswith("//", i) or text.startswith("/*", i):
                i = _skip_ws_comments(text, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise EditError("unbalanced brackets")
    while i < len(text) and text[i] not in ",}] \t\r\n/":
        i += 1
    return i


class Member:
    def __init__(self, key, lead_start, key_start, value_start, value_end, comma, end):
        self.key, self.lead_start, self.key_start = key, lead_start, key_start
        self.value_start, self.value_end, self.comma, self.end = value_start, value_end, comma, end


def members_of(text, open_idx):
    """(members, close_idx) of the object whose '{' is at open_idx."""
    if text[open_idx] != "{":
        raise EditError("expected '{'")
    out = []
    i = open_idx + 1
    prev_end = i
    while True:
        j = _skip_ws_comments(text, i)
        if j >= len(text):
            raise EditError("unterminated object")
        if text[j] == "}":
            return out, j
        if text[j] != '"':
            raise EditError(f"expected a key at offset {j}")
        key_end = _skip_string(text, j)
        key = json.loads(text[j:key_end])
        k = _skip_ws_comments(text, key_end)
        if text[k] != ":":
            raise EditError("expected ':'")
        v = _skip_ws_comments(text, k + 1)
        v_end = _skip_value(text, v)
        after = _skip_ws_comments(text, v_end)
        comma = after if after < len(text) and text[after] == "," else None
        member_end = (comma + 1) if comma is not None else v_end
        # the rest of the line (a trailing // comment) belongs to this member
        nl = text.find("\n", member_end)
        tail = text[member_end : nl if nl != -1 else len(text)]
        if (
            tail.strip() == ""
            or tail.lstrip().startswith("//")
            or (
                tail.lstrip().startswith("/*")
                and "*/" in tail
                and tail[tail.index("*/") + 2 :].strip() == ""
            )
        ):
            member_end = (nl + 1) if nl != -1 else len(text)
        region = text[prev_end:j]
        nl_in = region.find("\n")
        lead_start = prev_end + nl_in + 1 if nl_in != -1 else prev_end
        out.append(Member(key, lead_start, j, v, v_end, comma, member_end))
        i = comma + 1 if comma is not None else v_end
        prev_end = i


def find_object(text, path):
    """Index of the '{' of the object at `path` (list of keys) from the root object."""
    i = _skip_ws_comments(text, 0)
    if i >= len(text) or text[i] != "{":
        raise EditError("root is not an object")
    for key in path:
        members, _ = members_of(text, i)
        hit = [m for m in members if m.key == key]
        if not hit:
            raise EditError(f"key '{key}' not found")
        i = hit[0].value_start
        if text[i] != "{":
            raise EditError(f"'{key}' is not an object")
    return i


def _indent_of(text, idx):
    ls = text.rfind("\n", 0, idx) + 1
    return text[ls:idx] if text[ls:idx].strip() == "" else ""


def render_member(key, value, indent, comment=None, comma=True):
    body = json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)
    lines = []
    if comment:
        for c in str(comment).splitlines():
            lines.append(f"{indent}// {c}")
    lines.append(f"{indent}{json.dumps(key, ensure_ascii=False)}: {body}" + ("," if comma else ""))
    return "\n".join(lines) + "\n"


def edit_object(text, path, removes=(), replaces=None, adds=None):
    """Apply edits to the object at `path`. replaces: {key: value}; adds: [(key, value, comment)] appended at the end."""
    replaces = replaces or {}
    adds = adds or []
    open_idx = find_object(text, path)
    members, close_idx = members_of(text, open_idx)
    by_key = {m.key: m for m in members}
    for k in list(removes) + list(replaces):
        if k not in by_key:
            raise EditError(f"member '{k}' not found")
    edits = []  # (start, end, new_text), applied from the end backwards
    for k, value in replaces.items():
        m = by_key[k]
        indent = _indent_of(text, m.key_start)
        body = json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n" + indent)
        edits.append((m.value_start, m.value_end, body))
    for k in removes:
        m = by_key[k]
        edits.append((m.lead_start, m.end, ""))
    remaining = [m for m in members if m.key not in removes]
    # removing the last member that had no comma: drop the comma of the new last member (keeps strict JSON)
    if (
        removes
        and not adds
        and remaining
        and members[-1].key in removes
        and members[-1].comma is None
        and remaining[-1].comma is not None
    ):
        c = remaining[-1].comma
        edits.append((c, c + 1, ""))
    if adds:
        indent = (
            _indent_of(text, members[0].key_start)
            if members
            else _indent_of(text, close_idx) + "  "
        )
        trailing_style = bool(
            members and members[-1].comma is not None
        )  # keep the file's style for the last member
        close_line_start = text.rfind("\n", 0, close_idx) + 1
        before_close = text[close_line_start:close_idx]
        if before_close.strip() == "":
            ins_at, prefix = close_line_start, ""
        else:
            ins_at, prefix = close_idx, "\n"
        pieces = []
        if remaining and remaining[-1].comma is None and remaining[-1].key not in removes:
            c_at = remaining[-1].value_end
            edits.append((c_at, c_at, ","))
        for n, (key, value, comment) in enumerate(adds):
            last = n == len(adds) - 1
            pieces.append(
                render_member(key, value, indent, comment, comma=(trailing_style or not last))
            )
        edits.append((ins_at, ins_at, prefix + "".join(pieces)))
    # a member removed and an insertion at the same offset cannot overlap; sort by start descending
    edits.sort(
        key=lambda e: (e[0], e[1], len(e[2])), reverse=True
    )  # at one offset the longer insertion goes in first, so a comma lands before it
    for start, end, new in edits:
        text = text[:start] + new + text[end:]
    return text
