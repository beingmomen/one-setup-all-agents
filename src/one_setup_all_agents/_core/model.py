"""Neutral model of one MCP server entry, as read from any of the four tools.

Secrets policy: env and header VALUES are never kept. Only their names (and the total size of the
env values, which is not secret) survive parsing. command/args/url go through mask.py.
"""

import re

from .mask import mask_args, mask_text

# Fields that decide "present vs differs" in `status`.
COMPARE_FIELDS = ("transport", "command", "args", "url", "env_keys", "auth", "enabled")
# Fields shown by `diff` only (tool-specific or informational).
INFO_FIELDS = ("env_pass", "header_keys", "env_size", "cwd", "startup_timeout_sec")
ALL_FIELDS = COMPARE_FIELDS + INFO_FIELDS

_ENV_REF = re.compile(
    r"^(\$\{[^}]+\}|\{env:[^}]+\}|\{\{\$?[A-Za-z_]\w*\}\}|\$[A-Za-z_]\w*|%[^%]+%)$"
)
_BEARER_REF = re.compile(
    r"(?i)^bearer\s+(\$\{[^}]+\}|\{env:[^}]+\}|\{\{\$?[A-Za-z_]\w*\}\}|\$[A-Za-z_]\w*|%[^%]+%)$"
)
_SECRET_NAME = re.compile(
    r"(?i)(token|secret|passw(or)?d|api[_-]?key|credential|private[_-]?key|access[_-]?key)"
)


class Server:
    def __init__(self, name, tool):
        self.name = name
        self.tool = tool
        self.transport = "stdio"
        self.command = None
        self.args = []
        self.url = None
        self.env_keys = []
        self.env_pass = []
        self.env_size = 0
        self.header_keys = []
        self.auth = None
        self.enabled = True
        self.cwd = None
        self.startup_timeout_sec = None
        self.internal = False
        self.warnings = []

    def value(self, field):
        return getattr(self, field)

    def to_dict(self):
        d = {f: getattr(self, f) for f in ALL_FIELDS}
        d.update(
            name=self.name, tool=self.tool, internal=self.internal, warnings=list(self.warnings)
        )
        return d


def set_stdio(srv, command, args):
    srv.transport = "stdio"
    srv.command = mask_text(command) if command is not None else None
    srv.args = mask_args(args or [])


def set_http(srv, url, transport="http"):
    srv.transport = transport
    srv.url = mask_text(url) if url is not None else None


def set_env(srv, env):
    """env is a mapping NAME -> VALUE. Keep names and the total value size only."""
    if not isinstance(env, dict):
        return
    srv.env_keys = sorted(str(k) for k in env)
    srv.env_size = sum(len(str(v)) for v in env.values())
    for k, v in env.items():
        v = "" if v is None else str(v)
        if _SECRET_NAME.search(str(k)) and v and not _ENV_REF.match(v.strip()):
            srv.warnings.append(f"env {k} holds a literal value (hidden)")


def names_of(value):
    """Variable names from a list that may hold plain strings or {name: ...} tables."""
    out = []
    for item in value or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and "name" in item:
            out.append(str(item["name"]))
    return sorted(out)


def set_headers(srv, headers, header_env=None, bearer_env=None):
    """headers: NAME -> VALUE (literal or reference). header_env: NAME -> ENVVAR. bearer_env: ENVVAR name."""
    kinds = set()
    keys = set()
    if isinstance(headers, dict):
        for k, v in headers.items():
            keys.add(str(k))
            v = "" if v is None else str(v).strip()
            if _ENV_REF.match(v) or _BEARER_REF.match(v):
                kinds.add(
                    "bearer-env"
                    if (str(k).lower() == "authorization" or v.lower().startswith("bearer"))
                    else "header-env"
                )
            else:
                kinds.add("header-literal")
                srv.warnings.append(f"header {k} holds a literal value (hidden)")
    if isinstance(header_env, dict):
        for k in header_env:
            keys.add(str(k))
            kinds.add("bearer-env" if str(k).lower() == "authorization" else "header-env")
    if bearer_env:
        kinds.add("bearer-env")
    srv.header_keys = sorted(keys)
    for kind in ("header-literal", "bearer-env", "header-env"):
        if kind in kinds:
            srv.auth = kind
            break
