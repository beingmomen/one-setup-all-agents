"""Compare the neutral source with the live configs and produce a per-tool plan (add / update / remove).

Ownership: all-agents only touches
  * servers named in the source (for the tools they apply to),
  * servers listed in the source's `retired` for a tool,
  * servers the ledger says all-agents wrote earlier that left the source ("stale").
Everything else in a tool's file is left alone and only reported as unmanaged.
"""

from .model import COMPARE_FIELDS
from .parsers import INTERNAL, NORMALIZERS, TOOLS, finish_servers
from .report import _sig, fmt
from .source import patch_native, render_native, wrap

_ENV_FIELD = "env_keys"


class Op:
    def __init__(
        self,
        kind,
        name,
        reason=None,
        fields=None,
        old=None,
        new=None,
        new_native=None,
        old_native=None,
    ):
        self.kind = kind  # add | update | remove
        self.name = name
        self.reason = reason  # source | retired | stale
        self.fields = fields or []  # [(field, live_value, wanted_value)]
        self.old = old  # live Server (normalized) or None
        self.new = new  # expected Server (normalized) or None
        self.new_native = new_native
        self.old_native = old_native  # may hold env values; never printed

    def describe(self):
        if self.kind == "add":
            return f"add     {self.name}"
        if self.kind == "remove":
            return f"remove  {self.name}   ({self.reason})"
        diffs = "; ".join(f"{f}: {fmt(a)} -> {fmt(b)}" for f, a, b in self.fields)
        return f"update  {self.name}   {diffs}"

    def to_dict(self):
        return {
            "op": self.kind,
            "server": self.name,
            "reason": self.reason,
            "fields": [{"field": f, "live": a, "wanted": b} for f, a, b in self.fields],
        }


class ToolPlan:
    def __init__(self, tool, cfg):
        self.tool = tool
        self.cfg = cfg
        self.ops = []
        self.same = []
        self.unmanaged = []
        self.errors = []
        self.unsupported = []  # [(server, reason)]: in the source but not writable for this tool (skipped, reported)

    @property
    def changed(self):
        return bool(self.ops)


def expected_server(tool, spec, live_raw):
    """(fresh native entry, normalized Server) for `spec` in `tool`; env is carried over from the live entry on request."""
    live_env = None
    if live_raw:
        live_env = live_raw.get("environment" if tool == "opencode" else "env")
        if tool == "opencode" and not live_env:
            live_env = live_raw.get("env")
    fresh = render_native(tool, spec, live_env if isinstance(live_env, dict) else None)
    parsed = finish_servers(NORMALIZERS[tool](wrap(tool, spec.name, fresh)))[spec.name]
    return fresh, parsed


def build_plan(source, configs, ledger, only_tools=None, only_servers=None):
    plans = {}
    for tool in TOOLS:
        if only_tools and tool not in only_tools:
            continue
        cfg = configs[tool]
        tp = ToolPlan(tool, cfg)
        plans[tool] = tp
        if cfg.error:
            tp.errors.append(cfg.error)
            continue
        protected = set(source.unmanaged.get(tool, [])) | INTERNAL.get(tool, set())
        managed = source.managed_names(tool)
        tp.unsupported = [
            (n, why)
            for n, why in sorted(source.unsupported_for(tool).items())
            if not (only_servers and n not in only_servers)
        ]
        for name in managed:
            if only_servers and name not in only_servers:
                continue
            if name in protected:
                tp.errors.append(
                    f"'{name}' is managed in the source but protected (tool-internal/unmanaged) in {tool}"
                )
                continue
            spec = source.servers[name]
            live = cfg.servers.get(name)
            fresh, want = expected_server(tool, spec, cfg.raw.get(name))
            if live is None:
                tp.ops.append(Op("add", name, "source", new=want, new_native=fresh))
            elif _sig(live) != _sig(want):
                fields = [
                    (f, live.value(f), want.value(f))
                    for f in COMPARE_FIELDS
                    if live.value(f) != want.value(f)
                ]
                new_native = patch_native(
                    tool, cfg.raw[name], fresh, getattr(spec, "project", False)
                )
                tp.ops.append(
                    Op(
                        "update",
                        name,
                        "source",
                        fields,
                        old=live,
                        new=want,
                        new_native=new_native,
                        old_native=cfg.raw[name],
                    )
                )
            else:
                tp.same.append(name)
        retired = [
            n for n in source.retired.get(tool, []) if n in cfg.servers and n not in protected
        ]
        for name in retired:
            if only_servers and name not in only_servers:
                continue
            tp.ops.append(
                Op("remove", name, "retired", old=cfg.servers[name], old_native=cfg.raw[name])
            )
        stale = sorted(
            set(ledger.get(tool, []))
            - set(source.servers)
            - set(source.retired.get(tool, []))
            - protected
        )
        for name in stale:
            if name in cfg.servers and not (only_servers and name not in only_servers):
                tp.ops.append(
                    Op("remove", name, "stale", old=cfg.servers[name], old_native=cfg.raw[name])
                )
        known = set(managed) | set(source.retired.get(tool, [])) | set(ledger.get(tool, []))
        tp.unmanaged = sorted(n for n in cfg.servers if n not in known)
    return plans


def plan_data(plans):
    return {
        t: {
            "path": str(p.cfg.path),
            "error": p.errors or None,
            "same": p.same,
            "unmanaged": p.unmanaged,
            "ops": [o.to_dict() for o in p.ops],
        }
        for t, p in plans.items()
    }


def render_plan(plans, title="plan"):
    lines = []
    total = 0
    for tool, p in plans.items():
        head = f"[{tool}] {p.cfg.path}"
        lines.append(head)
        if p.errors:
            for e in p.errors:
                lines.append(f"  ERROR  {e}")
            continue
        for op in p.ops:
            lines.append("  " + op.describe())
        total += len(p.ops)
        for n, why in p.unsupported:
            lines.append(f"  UNSUPPORTED  {n}: {why}")
        if not p.ops:
            lines.append("  no changes")
        lines.append(
            f"  in sync: {len(p.same)}; unmanaged (left alone): {len(p.unmanaged)}"
            + (f" ({', '.join(p.unmanaged)})" if p.unmanaged else "")
        )
    n_tools = sum(1 for p in plans.values() if p.ops)
    lines.append("")
    lines.append(f"{title}: {total} change(s) in {n_tools} tool(s)")
    return "\n".join(lines) + "\n", total
