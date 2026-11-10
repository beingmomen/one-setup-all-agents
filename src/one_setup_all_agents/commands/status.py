"""Implementation of the `all-agents status` command."""

from __future__ import annotations

import argparse
import json

from .._core.mask import mask_text
from ..adapters import all_adapters
from ..config_source import (
    SOURCE_CONFIG_NAME,
    SourceError,
    ToolSettings,
    desired_all,
    load_source,
)
from ..context import CommandContext
from ..detect import detect_all, mask_home, render_detections
from ..engine import journal
from ..engine.plan import build_plan
from ..model import Kind, Plan, Unsupported
from ..reader import read_all
from ..ui import say, warn


def _render_human_status(tools_data: list[dict], undoable_count: int) -> str:
    lines = []
    for t in tools_data:
        ver_str = f" ({t['version']})" if t.get("version") else ""
        lines.append(f"{t['label']} ({t['id']}){ver_str}: {t['state']}")
        if t.get("binary"):
            lines.append(f"  binary: {t['binary']}")
        for w in t.get("warnings", ()):
            lines.append(f"  warning: {w}")
        for k_name in ("instructions", "mcp", "skills"):
            k_info = t["kinds"].get(k_name)
            if not k_info:
                continue
            k_state = k_info["state"]
            details = []
            if k_info.get("items"):
                details.append(f"items: {', '.join(k_info['items'])}")
            if k_info.get("reasons"):
                details.append(f"reasons: {'; '.join(k_info['reasons'])}")
            suffix = f" ({'; '.join(details)})" if details else ""
            lines.append(f"  {k_name}: {k_state}{suffix}")
    lines.append(f"Undoable operations: {undoable_count}")
    return "\n".join(lines)


def run(args: argparse.Namespace, ctx: CommandContext) -> int:
    """Run the status command: inspect whether tools match shared source config."""
    versions = bool(getattr(args, "versions", False))
    is_json = bool(getattr(args, "json", False))
    quiet = bool(getattr(args, "quiet", False))

    # 1. Check if initialised
    config_file = ctx.env.config_dir / SOURCE_CONFIG_NAME
    if not config_file.is_file():
        if is_json:
            data = {
                "schema": 1,
                "initialised": False,
                "tools": [],
                "undoable_operations": 0,
            }
            say(ctx, json.dumps(data, indent=2))
        elif quiet:
            warn(ctx, "not initialised: run `all-agents init`")
        else:
            detections = detect_all(ctx.env, versions=versions, probe=ctx.probe)
            say(ctx, render_detections(detections, ctx.env))
            say(ctx, "not initialised: run `all-agents init`")
        return 1

    adapters_by_id = {a.id: a for a in all_adapters()}

    # 2. Load source configuration
    try:
        source_config = load_source(ctx.env.config_dir, list(adapters_by_id.keys()))
    except SourceError as exc:
        if is_json:
            say(ctx, json.dumps({"schema": 1, "error": str(exc)}, indent=2))
        else:
            warn(ctx, str(exc))
        return 1

    # 3. Detect tools and read configurations
    detections = detect_all(ctx.env, versions=versions, probe=ctx.probe)
    detections_by_tool = {d.tool: d for d in detections}

    readings = read_all(ctx.env, all_adapters(), detections)
    readings_by_tool = {r.tool: r for r in readings}

    # 4. Active adapters: installed and enabled in source config
    active_adapters = [
        adapters_by_id[d.tool]
        for d in detections
        if d.installed
        and d.tool in adapters_by_id
        and source_config.tools.get(d.tool, ToolSettings()).enabled
    ]
    active_ids = {a.id for a in active_adapters}

    desired = desired_all(source_config, sorted(active_ids)) if active_ids else {}
    try:
        plan = build_plan(ctx.env, active_adapters, desired) if active_adapters else Plan()
    except journal.JournalError as exc:
        if is_json:
            say(ctx, json.dumps({"schema": 1, "error": str(exc)}, indent=2))
        else:
            warn(ctx, str(exc))
        return 1

    tools_data: list[dict] = []
    supported_kinds = (Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS)

    for a in all_adapters():
        d = detections_by_tool.get(a.id)
        is_installed = d.installed if d else False
        is_enabled = source_config.tools.get(a.id, ToolSettings()).enabled

        kinds_data: dict[str, dict] = {}

        if not is_installed:
            tool_state = "not installed"
            for k in supported_kinds:
                kinds_data[k.value] = {
                    "state": "not installed",
                    "items": [],
                    "reasons": [],
                }
        elif not is_enabled:
            tool_state = "disabled"
            for k in supported_kinds:
                kinds_data[k.value] = {
                    "state": "disabled",
                    "items": [],
                    "reasons": [],
                }
        else:
            reading = readings_by_tool.get(a.id)
            for k in supported_kinds:
                # 1. Problem reported by reader
                problems = [p for p in (reading.problems if reading else ()) if p.kind == k]
                if problems:
                    kinds_data[k.value] = {
                        "state": "unreadable",
                        "items": [],
                        "reasons": [mask_text(mask_home(p.message, ctx.env)) for p in problems],
                    }
                    continue

                # 2. Conflict in plan
                conflicts = [c for c in plan.conflicts if c.tool == a.id and c.kind == k]
                if conflicts:
                    kinds_data[k.value] = {
                        "state": "conflict",
                        "items": sorted({mask_text(mask_home(c.key, ctx.env)) for c in conflicts}),
                        "reasons": [mask_text(mask_home(c.reason, ctx.env)) for c in conflicts],
                    }
                    continue

                # 3. Executable ops in plan
                exec_ops = [
                    op
                    for op in plan.executable
                    if any(
                        r.tool == a.id and r.kind == k
                        for r in (*getattr(op, "claims", ()), *getattr(op, "releases", ()))
                    )
                ]
                if exec_ops:
                    items = sorted(
                        {
                            mask_text(mask_home(r.key, ctx.env))
                            for op in exec_ops
                            for r in (*getattr(op, "claims", ()), *getattr(op, "releases", ()))
                            if r.tool == a.id and r.kind == k
                        }
                    )
                    kinds_data[k.value] = {
                        "state": "differs",
                        "items": items,
                        "reasons": [],
                    }
                    continue

                # 4. Unsupported op in plan
                unsupported_ops = [
                    op
                    for op in plan.ops
                    if isinstance(op, Unsupported) and op.tool == a.id and op.kind == k
                ]
                if unsupported_ops:
                    kinds_data[k.value] = {
                        "state": "unsupported",
                        "items": [],
                        "reasons": [
                            mask_text(mask_home(op.reason, ctx.env)) for op in unsupported_ops
                        ],
                    }
                    continue

                # 5. In sync
                kinds_data[k.value] = {
                    "state": "in sync",
                    "items": [],
                    "reasons": [],
                }

            # Overall tool state
            kind_states = {kd["state"] for kd in kinds_data.values()}
            if "unreadable" in kind_states:
                tool_state = "unreadable"
            elif "conflict" in kind_states:
                tool_state = "conflict"
            elif "differs" in kind_states:
                tool_state = "differs"
            else:
                tool_state = "in sync"

        binary_str = (
            mask_text(mask_home(str(d.binary), ctx.env)) if d and d.binary is not None else None
        )
        warnings = (
            [mask_text(mask_home(w, ctx.env)) for w in d.warnings] if d and d.warnings else []
        )

        tools_data.append(
            {
                "id": a.id,
                "label": a.label,
                "state": tool_state,
                "version": d.version if d else None,
                "binary": binary_str,
                "warnings": warnings,
                "kinds": kinds_data,
            }
        )

    undoable_count = 0
    if ctx.env.state_dir.is_dir():
        try:
            undoable_count = len(journal.list_undoable(ctx.env.state_dir))
        except journal.JournalError as exc:
            if is_json:
                say(ctx, json.dumps({"schema": 1, "error": str(exc)}, indent=2))
            else:
                warn(ctx, str(exc))
            return 1

    has_issues = any(
        kd["state"] in ("differs", "conflict", "unreadable")
        for td in tools_data
        for kd in td["kinds"].values()
    )

    if quiet:
        if has_issues:
            drifting = [
                td["id"]
                for td in tools_data
                if any(
                    kd["state"] in ("differs", "conflict", "unreadable")
                    for kd in td["kinds"].values()
                )
            ]
            warn(
                ctx,
                f"drift detected ({', '.join(drifting)}): run `all-agents sync`",
            )
        return 0

    if is_json:
        data = {
            "schema": 1,
            "initialised": True,
            "tools": tools_data,
            "undoable_operations": undoable_count,
        }
        say(ctx, json.dumps(data, indent=2))
    else:
        say(ctx, _render_human_status(tools_data, undoable_count))

    return 2 if has_issues else 0
