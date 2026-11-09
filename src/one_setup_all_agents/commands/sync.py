"""Implementation of the `all-agents sync` command."""

from __future__ import annotations

import argparse

from ..adapters import all_adapters
from ..config_source import SOURCE_CONFIG_NAME, SourceError, desired_all, load_source
from ..context import CommandContext
from ..detect import detect_all
from ..engine.apply import apply_plan
from ..engine.errors import (
    ApplyError,
    EngineLockedError,
    OpenOperationError,
    PlanError,
)
from ..engine.plan import build_plan, render_plan
from ..model import Conflict, Kind
from ..ui import confirm, say, warn


def run(args: argparse.Namespace, ctx: CommandContext) -> int:
    """Run the sync command: reconcile shared source config with detected tools."""
    # (1) Check if initialised
    config_file = ctx.env.config_dir / SOURCE_CONFIG_NAME
    if not config_file.is_file():
        warn(ctx, "not initialised; run `all-agents init`")
        return 1

    # (2) Load source
    adapters_by_id = {a.id: a for a in all_adapters()}
    try:
        source_config = load_source(ctx.env.config_dir, list(adapters_by_id.keys()))
    except SourceError as exc:
        warn(ctx, str(exc))
        return 1

    # (3) Detections and tool filtering
    detections = detect_all(ctx.env, versions=False)
    installed_ids = {d.tool for d in detections if d.installed}
    enabled_ids = {tid for tid, s in source_config.tools.items() if s.enabled}
    candidate_ids = installed_ids & enabled_ids

    if getattr(args, "tool", None):
        for t in args.tool:
            if t not in adapters_by_id:
                warn(
                    ctx,
                    f"unknown tool '{t}'; valid tools: {', '.join(sorted(adapters_by_id.keys()))}",
                )
                return 1
        candidate_ids &= set(args.tool)

    if not candidate_ids:
        say(ctx, "nothing to sync")
        return 0

    # (4) Print detection warnings of chosen tools
    for d in detections:
        if d.tool in candidate_ids:
            for w in d.warnings:
                warn(ctx, w)

    # (5) Kinds filter
    if getattr(args, "only", None):
        kinds = frozenset(k if isinstance(k, Kind) else Kind(k) for k in args.only)
    else:
        kinds = None

    # (6) Desired state and plan generation
    chosen_ids = sorted(candidate_ids)
    desired = desired_all(source_config, chosen_ids)
    active_adapters = [adapters_by_id[tid] for tid in chosen_ids]
    plan = build_plan(ctx.env, active_adapters, desired, kinds=kinds)

    # (7) Print plan
    say(ctx, render_plan(plan))

    # (8) Has conflicts
    has_conflicts = any(
        isinstance(op, Conflict) for op in (getattr(plan, "items", None) or plan.ops)
    )

    # (9) Executable check
    if not plan.executable:
        if has_conflicts:
            say(ctx, "Nothing to apply; conflicts exist.")
            return 3
        say(ctx, "Nothing to do: all tools are in sync.")
        return 0

    # (10) Dry-run check
    if getattr(args, "dry_run", False):
        say(ctx, "Dry run: nothing was written.")
        return 3 if has_conflicts else 0

    # (11) Confirmation
    if not getattr(args, "yes", False):
        if not ctx.is_interactive:
            say(ctx, "Dry run only: not interactive; pass --yes to apply")
            return 0
        if not confirm(ctx, f"Apply {len(plan.executable)} change(s)?"):
            return 0

    # (12) Apply plan
    try:
        op_id = apply_plan(
            ctx.env,
            plan,
            label="sync",
            run_cli=ctx.run_cli,
            warn=lambda m: warn(ctx, m),
        )
    except EngineLockedError as exc:
        warn(ctx, str(exc))
        return 1
    except OpenOperationError:
        warn(ctx, "an earlier run did not finish; run `all-agents undo` to recover")
        return 1
    except (PlanError, ApplyError) as exc:
        warn(ctx, str(exc))
        return 1

    if op_id:
        say(ctx, f"applied operation {op_id}")

    # (13) Re-plan verification
    re_plan = build_plan(ctx.env, active_adapters, desired, kinds=kinds)
    if re_plan.executable:
        warn(ctx, f"warning: {len(re_plan.executable)} change(s) still pending after apply")

    re_has_conflicts = has_conflicts or any(
        isinstance(op, Conflict) for op in (getattr(re_plan, "items", None) or re_plan.ops)
    )
    if re_has_conflicts:
        return 3
    return 0
