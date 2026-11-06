"""`status` and `diff`: compute and render. Plain ASCII output so any console code page is safe."""

import json
from collections import Counter

from .model import ALL_FIELDS, COMPARE_FIELDS
from .parsers import LABELS, TOOLS

_MAX_VALUE = 160


# ---------------------------------------------------------------- helpers
def _sig(srv):
    return json.dumps([srv.value(f) for f in COMPARE_FIELDS], sort_keys=True, ensure_ascii=False)


def fmt(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, dict)):
        if not value:
            return "(none)"
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    else:
        text = str(value)
    if len(text) > _MAX_VALUE:
        text = text[: _MAX_VALUE - 20] + f"...({len(text)} chars)"
    return text


def _loaded(configs):
    return [t for t in TOOLS if configs[t].exists and not configs[t].error]


def server_names(configs):
    names = set()
    for t in TOOLS:
        names.update(configs[t].servers)
    return sorted(names)


def reference_tools(name, configs):
    """Tools whose entry is the majority form of this server (ties go to the earliest tool)."""
    present = [
        t for t in TOOLS if name in configs[t].servers and not configs[t].servers[name].internal
    ]
    if not present:
        present = [t for t in TOOLS if name in configs[t].servers]
    sigs = Counter(_sig(configs[t].servers[name]) for t in present)
    best = max(sigs.values())
    for t in present:
        if sigs[_sig(configs[t].servers[name])] == best:
            ref_sig = _sig(configs[t].servers[name])
            return [x for x in present if _sig(configs[x].servers[name]) == ref_sig]
    return []


def cell_states(name, configs):
    refs = reference_tools(name, configs)
    ref_sig = _sig(configs[refs[0]].servers[name]) if refs else None
    internal_somewhere = any(
        name in cfg.servers and cfg.servers[name].internal for cfg in configs.values()
    )
    states = {}
    for t in TOOLS:
        cfg = configs[t]
        if cfg.error:
            states[t] = "error"
        elif name not in cfg.servers:
            states[t] = "n/a" if internal_somewhere else "missing"
        elif cfg.servers[name].internal:
            states[t] = "internal"
        elif _sig(cfg.servers[name]) == ref_sig:
            states[t] = "present"
        else:
            states[t] = "differs"
    return states


def differences(configs):
    """Group field-level differences vs the majority form: [{servers, tool, field, reference, value}]."""
    groups = {}
    for name in server_names(configs):
        refs = reference_tools(name, configs)
        if not refs:
            continue
        ref = configs[refs[0]].servers[name]
        for t in TOOLS:
            srv = configs[t].servers.get(name)
            if srv is None or srv.internal or t in refs:
                continue
            for f in COMPARE_FIELDS:
                if srv.value(f) != ref.value(f):
                    key = (
                        t,
                        f,
                        json.dumps(ref.value(f), ensure_ascii=False),
                        json.dumps(srv.value(f), ensure_ascii=False),
                    )
                    groups.setdefault(key, {"reference_tools": refs, "servers": []})[
                        "servers"
                    ].append(name)
    out = []
    for (t, f, ref_v, v), g in sorted(groups.items()):
        out.append(
            {
                "servers": g["servers"],
                "tool": t,
                "field": f,
                "reference_tools": g["reference_tools"],
                "reference": json.loads(ref_v),
                "value": json.loads(v),
            }
        )
    return out


def counts_explained(configs):
    ok = _loaded(configs)
    shared = None
    for t in ok:
        names = {n for n, s in configs[t].servers.items() if not s.internal}
        shared = names if shared is None else shared & names
    shared = shared or set()
    union = {n for t in ok for n, s in configs[t].servers.items() if not s.internal}
    res = {}
    for t in TOOLS:
        cfg = configs[t]
        names = set(cfg.servers)
        res[t] = {
            "count": cfg.count,
            "shared": len(names & shared),
            "internal": sorted(n for n, s in cfg.servers.items() if s.internal),
            "extra": sorted(n for n in names - shared if not cfg.servers[n].internal),
            "missing": sorted(union - names) if not cfg.error else [],
        }
    return res


def warnings_of(configs):
    out = []
    for t in TOOLS:
        for n, s in sorted(configs[t].servers.items()):
            for w in s.warnings:
                out.append({"tool": t, "server": n, "warning": w})
    return out


# ---------------------------------------------------------------- status
def status_data(configs):
    names = server_names(configs)
    data = {
        "tools": {
            t: {
                "label": LABELS[t],
                "path": str(configs[t].path),
                "exists": configs[t].exists,
                "error": configs[t].error,
                "count": configs[t].count,
            }
            for t in TOOLS
        },
        "servers": {n: cell_states(n, configs) for n in names},
        "counts": counts_explained(configs),
        "differences": differences(configs),
        "warnings": warnings_of(configs),
    }
    return data


def render_status(configs):
    data = status_data(configs)
    names = list(data["servers"])
    w0 = max([len("server")] + [len(n) for n in names])
    cols = {t: max(len(t) + 0, 8) for t in TOOLS}
    lines = ["server".ljust(w0) + "  " + "  ".join(t.ljust(cols[t]) for t in TOOLS)]
    lines.append("-" * len(lines[0]))
    for n in names:
        row = [n.ljust(w0)]
        for t in TOOLS:
            st = data["servers"][n][t]
            label = {
                "present": "present",
                "missing": "missing",
                "differs": "DIFFERS",
                "internal": "internal",
                "error": "ERROR",
                "n/a": "n/a",
            }[st]
            row.append(label.ljust(cols[t]))
        lines.append("  ".join(row))
    lines.append("-" * len(lines[0]))
    lines.append(
        "count".ljust(w0)
        + "  "
        + "  ".join(str(data["tools"][t]["count"]).ljust(cols[t]) for t in TOOLS)
    )
    lines.append("")
    for t in TOOLS:
        info, c = data["tools"][t], data["counts"][t]
        if info["error"]:
            lines.append(f"{t}: {info['error']} ({info['path']})")
            continue
        parts = [f"{c['shared']} shared"]
        if c["internal"]:
            parts.append("+ " + ", ".join(c["internal"]) + " (tool-internal, never managed)")
        if c["extra"]:
            parts.append("+ " + ", ".join(c["extra"]) + " (not in every tool)")
        line = f"{t}: {c['count']} = " + " ".join(parts)
        if c["missing"]:
            line += f"; missing: {', '.join(c['missing'])}"
        lines.append(line)
    if data["differences"]:
        lines.append("")
        lines.append("differences vs the majority form (fields that decide DIFFERS):")
        for d in data["differences"]:
            srvs = d["servers"]
            who = srvs[0] if len(srvs) == 1 else f"{len(srvs)} servers ({srvs[0]}, ... {srvs[-1]})"
            lines.append(
                f"  {d['tool']}: {d['field']} = {fmt(d['value'])} (majority: {fmt(d['reference'])}) [{who}]"
            )
    if data["warnings"]:
        lines.append("")
        lines.append("warnings (values are never printed):")
        for w in data["warnings"]:
            lines.append(f"  {w['tool']}/{w['server']}: {w['warning']}")
    return "\n".join(lines) + "\n", data


# ---------------------------------------------------------------- diff
def diff_data(configs, name):
    present = {t: configs[t].servers[name] for t in TOOLS if name in configs[t].servers}
    fields = {}
    for f in ALL_FIELDS:
        per_tool = {t: present[t].value(f) for t in present}
        compared = {t: v for t, v in per_tool.items() if not present[t].internal}
        distinct = {json.dumps(v, ensure_ascii=False, sort_keys=True) for v in compared.values()}
        fields[f] = {
            "values": per_tool,
            "differs": len(distinct) > 1,
            "compared": f in COMPARE_FIELDS,
        }
    return {
        "server": name,
        "tools": {t: (present[t].to_dict() if t in present else None) for t in TOOLS},
        "not_configured_in": [t for t in TOOLS if t not in present and not configs[t].error],
        "unreadable_in": [t for t in TOOLS if configs[t].error],
        "fields": fields,
    }


def render_diff(configs, name):
    d = diff_data(configs, name)
    lines = [f"server: {name}"]
    if d["not_configured_in"]:
        lines.append("not configured in: " + ", ".join(d["not_configured_in"]))
    if d["unreadable_in"]:
        lines.append("unreadable in: " + ", ".join(d["unreadable_in"]))
    lines.append("")
    for f, info in d["fields"].items():
        mark = "DIFFERS" if info["differs"] else "same"
        if not info["compared"]:
            mark = "info (tool-specific)" if info["differs"] else "info, same"
        lines.append(f"{f}  [{mark}]")
        groups = {}
        for t, v in info["values"].items():
            groups.setdefault(json.dumps(v, ensure_ascii=False, sort_keys=True), []).append(t)
        for key, tools in groups.items():
            label = "all tools" if len(groups) == 1 and len(tools) > 1 else ", ".join(tools)
            lines.append(f"    {label:<28} {fmt(json.loads(key))}")
    notes = [(t, w) for t in TOOLS if d["tools"][t] for w in d["tools"][t]["warnings"]]
    if notes:
        lines.append("")
        lines.append("warnings (values are never printed):")
        lines.extend(f"  {t}: {w}" for t, w in notes)
    internal = [t for t in TOOLS if d["tools"][t] and d["tools"][t]["internal"]]
    if internal:
        lines.append("")
        lines.append(
            "tool-internal in: " + ", ".join(internal) + " (excluded from the DIFFERS decision)"
        )
    return "\n".join(lines) + "\n", d
