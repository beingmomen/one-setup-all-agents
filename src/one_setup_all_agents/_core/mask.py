"""Masking of anything secret-looking in text that all-agents prints.

The neutral model keeps env-variable and header-variable NAMES, plus non-secret literal env values
(`env_value_looks_secret` is the one check for those). This module also covers the places a literal
secret can hide: `command`, `args` and `url`.
Env references (${VAR}, {env:VAR}, {{VAR}}, {{$VAR}}, $VAR, %VAR%) are names, not secrets, and are kept.
"""

import re

MASK = "***"

_REF_AHEAD = r"(?!\$\{|\{env:|\{\{\$?[A-Za-z_]|\$[A-Za-z_]|%[A-Za-z_])"

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
_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)" + _REF_AHEAD + r"[^\s\"']+")
_URL_USERINFO = re.compile(r"(://[^/\s:@]+):" + _REF_AHEAD + r"[^@/\s]+@")
_NAME_VALUE = re.compile(
    r"(?i)(?<![\w.-])([\w.-]*(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|apikey|credential"
    r"|private[_-]?key|access[_-]?key|authorization|auth[_-]?token|auth[_-]?key|signature|sig)[\w.-]*)"
    r"(=)" + _REF_AHEAD + r"([^\s&;\"']+)"
)
_HEADER_VALUE = re.compile(
    r"(?i)\b(authorization|x-api-key|api-key|x-auth-token)(\s*:\s*)"
    + _REF_AHEAD
    + r"(?!bearer\b|basic\b)[^\s\"']+"
)
_SECRET_FLAG = re.compile(
    r"(?i)^--?[\w.-]*(token|secret|passw(?:or)?d|pwd|api[_-]?key|apikey|credential|private[_-]?key|access[_-]?key)[\w.-]*$"
)
_LONG_TOKEN = re.compile(r"^[A-Za-z0-9_+=.-]{32,}$")

# A variable name that suggests a secret, and a value that is only a reference to a variable.
SECRET_NAME = re.compile(
    r"(?i)(token|secret|passw(or)?d|api[_-]?key|credential|private[_-]?key|access[_-]?key)"
)
ENV_REF = re.compile(
    r"^(\$\{[^}]+\}|\{env:[^}]+\}|\{\{\$?[A-Za-z_]\w*\}\}|\$[A-Za-z_]\w*|%[^%]+%)$"
)


def _is_ref(value):
    return bool(re.match(r"^(\$\{|\{env:|\{\{\$?[A-Za-z_]|\$[A-Za-z_]|%[A-Za-z_])", value))


def mask_text(text):
    """Mask secret-looking parts of one string (a command, an argument or a URL)."""
    if not isinstance(text, str):
        return text
    s = _TOKEN_SHAPES.sub(MASK, text)
    s = _BEARER.sub(lambda m: m.group(1) + m.group(2) + MASK, s)
    s = _URL_USERINFO.sub(lambda m: m.group(1) + ":" + MASK + "@", s)
    s = _NAME_VALUE.sub(lambda m: m.group(1) + m.group(2) + MASK, s)
    s = _HEADER_VALUE.sub(lambda m: m.group(1) + m.group(2) + MASK, s)
    if _LONG_TOKEN.match(s) and re.search(r"[A-Za-z]", s) and re.search(r"\d", s):
        # a whole argument that is one long opaque string (key-like); paths and URLs never match
        s = MASK
    return s


def env_value_looks_secret(name, value):
    """True when an env entry `name=value` must not be stored as a literal.

    A value that masking would change looks like a secret; so does any non-empty value of a
    secret-sounding name unless it is only a reference to a variable. Used by the legacy source
    and by `McpServer` so the rule exists once.
    """
    value = "" if value is None else str(value)
    return mask_text(value) != value or bool(
        SECRET_NAME.search(str(name)) and value and not ENV_REF.match(value.strip())
    )


def mask_args(args):
    """Mask a list of arguments; the value after a secret-looking flag (`--token abc`) is masked too."""
    out = []
    hide_next = False
    for a in args:
        a = str(a)
        if hide_next and not a.startswith("-") and not _is_ref(a):
            out.append(MASK)
            hide_next = False
            continue
        hide_next = False
        masked = mask_text(a)
        if a.startswith("-") and "=" not in a and _SECRET_FLAG.match(a):
            hide_next = True
        out.append(masked)
    return out
