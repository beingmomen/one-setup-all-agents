"""Masking of anything secret-looking in text that all-agents prints.

The neutral model keeps env-variable and header-variable NAMES, plus non-secret literal env values
(`env_value_looks_secret` is the one check for those). This module also covers the places a literal
secret can hide: `command`, `args` and `url`.
Env references (${VAR}, {env:VAR}, {{VAR}}, {{$VAR}}, $VAR, %VAR%) are names, not secrets, and are kept.
"""

import json
import re

MASK = "***"

# The start of a reference with a fallback (`${NAME:-x}`, `${NAME-x}`, `{env:NAME:x}`); the
# fallback, up to the matching closing brace, may be a literal secret.
_FALLBACK_START = re.compile(r"\$\{[A-Za-z_]\w*:?-|\{env:[A-Za-z_]\w*:")


def mask_fallbacks(text):
    """Replace the whole fallback of every reference with MASK: nested braces count, an escaped
    character (a backslash and the next one) or a quoted part never closes it, and an unclosed
    fallback is masked to the end of `text` (callers mask a whole text before splitting it into
    lines). A quoted part may span lines only when the fallback then closes; otherwise quotes
    are read on one line, so a stray apostrophe never swallows the lines after it."""
    out, i = [], 0
    while True:
        m = _FALLBACK_START.search(text, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        k, depth = _fallback_end(text, m.end(), across_lines=True)
        if depth:
            k, depth = _fallback_end(text, m.end(), across_lines=False)
        out.append(text[i : m.end()] + MASK + "}")
        i = min(k, len(text))


def _fallback_end(text, k, *, across_lines):
    """(index after the fallback's closing brace, depth left: 0 when it closed)."""
    depth = 1
    while k < len(text) and depth:
        c = text[k]
        if c == "\\":
            k += 2
            continue
        if c in "'\"":  # a shell-quoted part: its braces never close the fallback
            end = _closing_quote(text, k, across_lines=across_lines)
            if end != -1:
                k = end + 1
                continue
        depth += {"{": 1, "}": -1}.get(c, 0)
        k += 1
    return k, depth


_mask_fallbacks = mask_fallbacks


def _closing_quote(text, k, *, across_lines=False):
    """Index of the quote that closes the one at `k` (-1: none, and the quote is then an
    ordinary character). Inside double quotes a backslash escapes the next character."""
    quote, j = text[k], k + 1
    while j < len(text) and (across_lines or text[j] != "\n"):
        if quote == '"' and text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j
        j += 1
    return -1


# Known token shapes.
_TOKEN_SHAPES = re.compile(
    r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|nfp_[A-Za-z0-9]{16,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)
# A quoted value runs to a closing quote that ends the value (a backslash escapes one; a quote
# with more text right after it, `"${T}"tail`, does not end it), or to the end of the line; the
# quotes stay and what they hold is judged.
_END = r"(?=[\s,;&)}\]]|\Z)"
_QUOTED = r"([\"'`])((?:\\[^\r\n]?|[^\r\n\\])*?)(\{q}" + _END + r"|(?=[\r\n]|\Z))"
# A bare value: a quote inside it belongs to it unless the quote closes something (`abc"def`)
_BARE = (
    r"((?:[^\s{x}\"'`]"
    # a quote with its own (unescaped) closing quote later on the line opens a part of the
    # value, spaces included (`abc"my secret"`, `abc" tail"`)
    r"|\"(?:\\[^\r\n]|[^\"\\\r\n])*\"|'(?:\\[^\r\n]|[^'\\\r\n])*'|`(?:\\[^\r\n]|[^`\\\r\n])*`"
    # an unclosed quote that does not close something runs to the end of the line
    r"|(?=[\"'`][^\s,;&)}}\]])(?:\"[^\"\r\n]*|'[^'\r\n]*|`[^`\r\n]*))+)"
)
_BEARER = re.compile(
    r"(?i)\b(bearer|basic)(\s+)(?:"
    + _QUOTED.replace("{q}", "3")
    + "|"
    + _BARE.replace("{x}", "").replace("}}", "}")
    + ")"
)
_URL_USERINFO = re.compile(r"(://[^/\s:@]+):([^@/\s]+)@")
_NAME_VALUE = re.compile(
    r"(?i)(?<![\w.-])([\w.-]*(?:token|secret|passw(?:or)?d|pwd|api[_.-]?key|apikey|credential"
    r"|private[_.-]?key|access[_.-]?key|authorization|auth[_.-]?token|auth[_.-]?key|signature|sig"
    r"|cookie)[\w.-]*)"
    r"(=)(?:"
    + _QUOTED.replace("{q}", "3")
    # a JSON value (`--token={"a": "x"}`) has spaces and quotes: it runs to the end of the line
    # (a plain `{env:NAME}` / `{{NAME}}` reference is a bare value, judged as one)
    + r"|((?!\{env:[A-Za-z_]\w*\}|\{\{\$?[A-Za-z_]\w*\}\})[\[{][^\r\n]*|"
    + _BARE.replace("{x}", "&;").replace("}}", "}")[1:]
    + ")"
)


def _enclosing_quote(text, pos):
    """The quote of a string still open at `pos` on its line (an odd count of unescaped quotes
    before it: `"token=${T}" next` or `{"a": "Bearer ${T}", ...}`), or None."""
    start = max(text.rfind("\n", 0, pos), text.rfind("\r", 0, pos)) + 1
    before = text[start:pos]
    best = None
    for q in _QUOTES:
        spots = [i for i, ch in enumerate(before) if ch == q and (i == 0 or before[i - 1] != "\\")]
        if len(spots) % 2 and (best is None or spots[-1] > best[1]):
            best = (q, spots[-1])
    return best[0] if best else None


def _unescaped(value, q):
    for i, ch in enumerate(value):
        if ch == q and (i == 0 or value[i - 1] != "\\"):
            return i
    return -1


def _sub_values(pattern, text):
    """Mask every `<head><sep><value>` of `pattern`: a quoted value (groups 3-5) inside its
    quotes, a bare one (group 6) whole. A bare value inside a string opened before its name
    ends at that string's closing quote, and the text after it is searched again."""
    out = []
    i = 0
    while (m := pattern.search(text, i)) is not None:
        head = m.group(1) + m.group(2)
        out.append(text[i : m.start()])
        if m.group(3) is not None:
            out.append(head + m.group(3) + _keep_ref(m.group(4)) + m.group(5))
            i = m.end()
            continue
        value = m.group(6)
        q = _enclosing_quote(text, m.start())
        cut = _unescaped(value, q) if q else -1
        if cut >= 0:
            value = value[:cut]
        out.append(head + (_keep_ref(value) if value else ""))
        i = m.start(6) + len(value)
    out.append(text[i:])
    return "".join(out)


# `Name: value` for a credential header (a cookie holds spaces, an Authorization value may be
# `<scheme> <credential>`): see `_mask_header_values`.
_HEADER_NAME = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|x-api-key|api-key|x-auth-token|x-auth-key"
    r"|auth-key|cookie)"
    r"\s*:[ \t]*"
)
_QUOTES = "\"'`"


def _judge_header_value(value):
    """A header value is kept only when it is a reference (alone or after a scheme), also when
    the whole value is quoted; anything else is masked whole."""
    q = value[0]
    if len(value) >= 2 and q in _QUOTES and value[-1] == q:
        inner = value[1:-1]
        if not re.search(r"(?<!\\)" + re.escape(q), inner):  # one quoted value, nothing after it
            return q + _keep_header_ref(inner) + q
    return _keep_header_ref(value)


def _mask_header_values(text):
    """Mask the value of every credential header written as text. When a quote stands right
    before the header name (`-H 'Cookie: a=1'`), the value runs to the matching unescaped quote
    that ends the argument (followed by a space, punctuation or the end of the line; a quote
    with more text right after it, `'X-Api-Key: 'tail`, does not end it); otherwise it runs to
    the end of the line, whatever it holds."""
    s = text
    i = 0
    while (m := _HEADER_NAME.search(s, i)) is not None:
        start = m.end()
        end = len(s) if (nl := s.find("\n", start)) < 0 else nl
        if end > start and s[end - 1] == "\r":
            end -= 1
        q = s[m.start() - 1] if m.start() and s[m.start() - 1] in _QUOTES else None
        if q is not None:
            k = start
            while k < end:
                if s[k] == "\\":
                    k += 2
                elif s[k] == q and (k + 1 >= end or s[k + 1] in " \t,;)}]"):
                    break
                else:
                    k += 1
            end = min(k, end)
        value = s[start:end].rstrip()
        new = _judge_header_value(value) if value else value
        s = s[:start] + new + s[start + len(value) :]
        i = start + len(new)
    return s


_SCHEME_REF = re.compile(r"\A([A-Za-z][\w.-]{0,30})\s+(\S+)\Z")
_SECRET_FLAG = re.compile(
    r"(?i)^--?[\w.-]*(token|secret|passw(?:or)?d|pwd|api[_.-]?key|apikey|credential"
    r"|private[_.-]?key|access[_.-]?key|auth[_.-]?key|cookie|authorization)[\w.-]*$"
)
_LONG_TOKEN = re.compile(r"^[A-Za-z0-9_+=.-]{32,}$")

# A variable name that suggests a secret, and a value that is only a reference to a variable.
SECRET_NAME = re.compile(
    r"(?i)(token|secret|passw(or)?d|api[_.-]?key|credential|private[_.-]?key|access[_.-]?key"
    r"|auth[_.-]?key)"
)
ENV_REF = re.compile(
    # a plain name only: `${NAME:-fallback}` may hold a literal secret in its fallback
    r"^(\$\{[A-Za-z_]\w*\}|\{env:[A-Za-z_]\w*\}|\{\{\$?[A-Za-z_]\w*\}\}|\$[A-Za-z_]\w*"
    r"|%[A-Za-z_][\w()]*%)\Z"
)


def is_env_ref(value):
    """Whether the WHOLE value is a plain reference to a variable whose name is not itself a
    credential (`${<token>}` is a misfiled secret, not a reference)."""
    value = value.strip()
    if not ENV_REF.match(value):
        return False
    name = re.sub(r"^(?:\$\{|\{env:|\{\{\$?|\$|%)", "", value).rstrip("}%")
    return not looks_like_credential(name)


def _is_ref(value):
    return is_env_ref(value)  # the whole value, not a prefix


def _keep_ref(value):
    return value if is_env_ref(value) else MASK


def _keep_header_ref(value):
    """A header value is kept only when it is a plain reference, alone or after a scheme."""
    match = _SCHEME_REF.match(value)
    if match and is_env_ref(match.group(2)):
        return value
    return _keep_ref(value)


# A reference inside a text (`${NAME}`, `{env:NAME}`, `{{NAME}}`, `$NAME`, `%NAME%`): its NAME is
# masked when the name is itself a credential (a token misfiled as a variable name).
_REF_NAME = re.compile(
    r"(\$\{)([A-Za-z_]\w*)(?=\}|:?-)"  # also before a fallback: `${NAME:-x}`, `${NAME-x}`
    r"|(\{env:)([A-Za-z_]\w*)(?=\}|:)"
    r"|(\{\{\$?)([A-Za-z_]\w*)(?=\})"
    r"|(\$)([A-Za-z_]\w*)"  # also right after a word: `prefix$NAME`
    r"|(%)([A-Za-z_][\w()]*)(?=%)"
)


def _mask_ref_names(text):
    def repl(m):
        opener = next(g for g in m.groups()[0::2] if g is not None)
        name = next(g for g in m.groups()[1::2] if g is not None)
        return opener + (MASK if looks_like_credential(name) else name)

    return _REF_NAME.sub(repl, text)


def mask_text(text):
    """Mask secret-looking parts of one string (a command, an argument or a URL)."""
    if not isinstance(text, str):
        return text
    # first the whole fallback of a reference, whatever it holds (`&`, `;`, spaces, braces)
    s = _mask_fallbacks(text)
    s = _mask_ref_names(s)
    s = _TOKEN_SHAPES.sub(MASK, s)
    # a credential is kept only when the WHOLE value is a plain reference (`${NAME}`)
    s = _sub_values(_BEARER, s)
    s = _URL_USERINFO.sub(lambda m: m.group(1) + ":" + _keep_ref(m.group(2)) + "@", s)
    s = _sub_values(_NAME_VALUE, s)
    s = _mask_header_values(s)
    if _LONG_TOKEN.match(s) and re.search(r"[A-Za-z]", s) and re.search(r"\d", s):
        # a whole argument that is one long opaque string (key-like); paths and URLs never match
        s = MASK
    return s


def looks_like_credential(name):
    """Whether a NAME (an env key, a header name) is itself a credential (a misfiled token),
    also when padded with spaces."""
    name = str(name)
    return mask_text(name) != name or mask_text(name.strip()) != name.strip()


def mask_name(name):
    """A NAME as it may be shown: MASK when it is itself a credential (padded too)."""
    return MASK if looks_like_credential(name) else str(name)


def env_value_looks_secret(name, value):
    """True when an env entry `name=value` must not be stored as a literal.

    A value that masking would change looks like a secret; so does any non-empty value of a
    secret-sounding name unless it is only a reference to a variable. Used by the legacy source
    and by `McpServer` so the rule exists once.
    """
    value = "" if value is None else str(value)
    if looks_like_credential(name):
        return True  # the name itself is a credential (a token misfiled as a key)
    if ENV_REF.match(value.strip()) and not is_env_ref(value):
        return True  # a reference whose variable name is itself a credential
    if _FALLBACK_START.search(value):
        return True  # even a fallback that reads `***` already
    secret_name = SECRET_NAME.search(str(name)) or _SECRET_KEY.search(str(name))
    return _mask_json_text(value) != value or bool(
        secret_name and value and not is_env_ref(value)
    )


_SECRET_KEY = re.compile(r"(?i)authorization|cookie")


#: A header scheme (the same rule as `model.HEADER_SCHEME_PATTERN`; `model` imports this module).
_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9._-]*\Z")


def _reference_value(value, *, allow_scheme=False):
    """A plain reference, or (in a header only) `<scheme> <reference>` with one space and a valid,
    non-credential scheme (`Bearer ${TOKEN}`): a name, not a secret."""
    if "\r" in value or "\n" in value:
        return False
    if allow_scheme:
        parts = value.split(" ")
        if len(parts) == 2 and _SCHEME.match(parts[0]) and not looks_like_credential(parts[0]):
            return is_env_ref(parts[1])
    return is_env_ref(value)


def _is_secret_flag(value):
    return (
        isinstance(value, str)
        and value.startswith("-")
        and "=" not in value
        and bool(_SECRET_FLAG.match(value))
    )


_PWD_KEY = re.compile(r"(?i)pwd")


def mask_json(value, key=None, *, parent=None):
    """Mask a parsed JSON document field by field: a non-empty value under a secret key (unless
    it is a plain reference, or `<scheme> <reference>` inside `headers`), a credential-shaped key
    name, and any list judged as a command line (whatever follows `--token` is masked)."""
    secret_key = key is not None and bool(SECRET_NAME.search(key) or _SECRET_KEY.search(key))
    if key is not None and parent != "env" and _PWD_KEY.search(key):
        secret_key = True  # `pwd` is a password field, except the shell's `PWD` in `env`
    if isinstance(value, str):
        allow_scheme = parent == "headers"
        if secret_key and value and not _reference_value(value, allow_scheme=allow_scheme):
            return MASK
        return _mask_json_text(value)
    if secret_key and value is not None:
        return MASK  # a number or a structure under a secret name too
    if isinstance(value, list):
        out, hide_next = [], False
        for item in value:
            plain_ref = isinstance(item, str) and (_looks_like_flag(item) or _is_ref(item))
            if hide_next and item is not None and not plain_ref:
                out.append(MASK)  # anything right after a secret flag: string, number, structure
                hide_next = False
                continue
            hide_next = _is_secret_flag(item)
            out.append(mask_json(item))
        return out
    if isinstance(value, dict):
        return {mask_name(k): mask_json(v, k, parent=key) for k, v in value.items()}
    return value


class DuplicateKeyError(ValueError):
    """A JSON object names the same key twice: which value counts is ambiguous."""


def _unique_pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise DuplicateKeyError("a key appears twice")
    return dict(pairs)


def loads_strict(text):
    """`json.loads` that refuses a key written twice (a reader keeps only the last value, so the
    first one would escape every check)."""
    return json.loads(text, object_pairs_hook=_unique_pairs)


_FLAG_JSON = re.compile(r"\A(-{1,2}[A-Za-z][\w.-]*=)(\s*[\[{].*)\Z", re.DOTALL)


def _mask_json_text(text):
    """`mask_text`, but a JSON document is masked field by field first (`{"api_key": "x"}` holds
    no `name=value` for the text patterns to find); unchanged text is returned as it was. A
    document with a key written twice is masked whole. `--flag=<json>` is judged the same way:
    its JSON field by field, all of it when the flag itself is a secret flag."""
    attached = _FLAG_JSON.match(text)
    if attached:
        flag, rest = attached.group(1), attached.group(2)
        shown = mask_text(flag)  # the flag name may itself be a misfiled token
        if _SECRET_FLAG.match(flag[:-1]):
            masked = rest if is_env_ref(rest) else MASK  # `--token={env:NAME}` is a reference
        else:
            masked = _mask_json_text(rest)
        return text if (shown, masked) == (flag, rest) else shown + masked
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            doc = loads_strict(stripped)
        except DuplicateKeyError:
            return MASK
        except json.JSONDecodeError:
            doc = None  # not JSON after all: judged as text below
        except (ValueError, RecursionError):
            return MASK  # JSON the parser cannot inspect (a huge number, deep nesting)
        if doc is not None:
            # every key and value was judged: the text patterns must not run across delimiters
            try:
                masked = mask_json(doc)
            except RecursionError:
                return MASK
            return json.dumps(masked, sort_keys=True) if masked != doc else text
    return mask_text(text)


_FLAG = re.compile(r"\A(?:-[A-Za-z]|--[A-Za-z][A-Za-z0-9_.-]*)\Z")


def _looks_like_flag(value):
    """Exactly `-x` or `--name`: a flag, not a value (`-123456` or `-demo-value` after `--token`
    is a value)."""
    return bool(_FLAG.match(value))


def mask_args(args):
    """Mask a list of arguments; the value after a secret-looking flag (`--token abc`) is masked too."""
    out = []
    hide_next = False
    for a in args:
        a = str(a)
        if hide_next and not _looks_like_flag(a) and not _is_ref(a):
            out.append(MASK)
            hide_next = False
            continue
        hide_next = False
        masked = _mask_json_text(a)
        if a.startswith("-") and "=" not in a and _SECRET_FLAG.match(a):
            hide_next = True
        out.append(masked)
    return out
