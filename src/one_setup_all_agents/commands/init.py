"""Implementation of the `all-agents init` command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from ..adapters import ADAPTERS, all_adapters
from ..config_source import SOURCE_CONFIG_NAME, desired_all, load_source
from ..context import CommandContext
from ..detect import detect_all, render_detections
from ..engine.apply import apply_plan
from ..engine.errors import (
    ApplyError,
    BackupError,
    EngineError,
    EngineLockedError,
    OpenOperationError,
    PlanError,
)
from ..engine.plan import build_plan
from ..importer import propose, source_ops
from ..model import Detection, ItemRef, Plan
from ..reader import read_all
from ..snapshot import snapshot_ops
from ..ui import confirm, say, warn


def _check_adopted_replan(
    ctx: CommandContext,
    detections: Sequence[Detection],
    adopted: Sequence[ItemRef],
) -> None:
    if not adopted or not (ctx.env.config_dir / SOURCE_CONFIG_NAME).exists():
        return
    try:
        installed_adapters = [
            a for a in all_adapters() if any(d.tool == a.id and d.installed for d in detections)
        ]
        adapter_ids = [a.id for a in installed_adapters]
        source = load_source(ctx.env.config_dir, adapter_ids)
        replan = build_plan(ctx.env, installed_adapters, desired_all(source, adapter_ids))
        adopted_set = {(r.tool, r.kind.value, r.key) for r in adopted}
        for op in replan.executable:
            for ref in getattr(op, "claims", ()):
                if (ref.tool, ref.kind.value, ref.key) in adopted_set:
                    warn(
                        ctx,
                        f"warning: adopted item '{ref.key}' for {ref.tool} "
                        "still requires changes during sync",
                    )
    except Exception:
        pass


def run(args: argparse.Namespace, ctx: CommandContext) -> int:
    """Run the init command: detect tools, import configs, and create source files."""
    # 1. detect_all: if no tool is installed, exit 0
    detections = detect_all(ctx.env)
    if not any(d.installed for d in detections):
        say(ctx, "No supported tool was found. Nothing to import.")
        return 0

    installed_detections = [d for d in detections if d.installed]
    say(ctx, render_detections(installed_detections, ctx.env).rstrip("\n"))

    # 2. refuse if already initialised
    if (ctx.env.config_dir / SOURCE_CONFIG_NAME).exists():
        warn(ctx, "already initialised; use `all-agents sync`")
        return 1

    # 3. check --from
    adapters_by_id = {a.id: a for a in all_adapters()}
    from_tool = getattr(args, "from_tool", None)
    if from_tool is not None and from_tool not in adapters_by_id:
        valid = ", ".join(sorted(adapters_by_id.keys()))
        warn(ctx, f"unknown tool '{from_tool}'; valid tools: {valid}")
        return 1

    # 4. read_all and print problems
    readings = read_all(ctx.env, ADAPTERS, detections)
    for r in readings:
        for p in r.problems:
            warn(ctx, f"problem reading {p.tool} {p.kind.value}: {p.message}")

    # 5. propose
    ask_fn = ctx.read_line if (ctx.is_interactive and not getattr(args, "yes", False)) else None
    try:
        proposal = propose(
            readings,
            detections,
            from_tool=from_tool,
            ask=ask_fn,
            env=ctx.env,
            adapters=adapters_by_id,
        )
    except ValueError as exc:
        warn(ctx, str(exc))
        return 1

    for msg in proposal.messages:
        if msg == "no instructions found":
            say(ctx, msg)
        else:
            warn(ctx, msg)

    # 6. print summary
    say(ctx, "Import proposal:")
    if proposal.instructions is not None:
        say(ctx, "  - Instructions: imported")
    else:
        say(ctx, "  - Instructions: none")
    server_names = sorted(proposal.servers.keys())
    servers_summary = ", ".join(server_names) if server_names else "none"
    say(ctx, f"  - MCP servers ({len(server_names)}): {servers_summary}")
    if hasattr(proposal.skills, "copies"):
        copied_cnt = len(proposal.skills.copies)
        adopted_cnt = len(proposal.skills.adopt)
        conflicts_cnt = len(proposal.skills.conflicts)
        names = sorted(c.name for c in proposal.skills.copies)
        skills_summary = ", ".join(names) if names else "none"
        say(
            ctx,
            f"  - Skills ({copied_cnt} copied, {adopted_cnt} adopted, {conflicts_cnt} conflicts): "
            f"{skills_summary}",
        )
    else:
        skill_names = sorted(s.name if hasattr(s, "name") else str(s) for s in proposal.skills)
        skills_summary = ", ".join(skill_names) if skill_names else "none"
        say(ctx, f"  - Skills ({len(skill_names)}): {skills_summary}")
    if proposal.adopt:
        items_str = ", ".join(f"{r.tool}:{r.kind.value}:{r.key}" for r in proposal.adopt)
        say(ctx, f"  - Adopted items ({len(proposal.adopt)}): {items_str}")

    # 7. dry-run check
    if getattr(args, "dry_run", False):
        return 0

    # 8. confirm
    if not getattr(args, "yes", False):
        if not confirm(ctx, "Proceed with initialisation?"):
            return 1

    # 9. build plan
    installed_tool_ids = [d.tool for d in detections if d.installed and d.tool]
    src_ops = source_ops(ctx.env, proposal, installed_tool_ids)

    def _execute_plan(retry: bool = True) -> str | None:
        sn_ops, sn_messages = snapshot_ops(ctx.env, adapters_by_id, detections)
        for msg in sn_messages:
            warn(ctx, msg)
        combined_ops = list(sn_ops) + list(src_ops)
        plan = Plan(tuple(combined_ops))
        try:
            return apply_plan(
                ctx.env,
                plan,
                label="init",
                adopt=tuple(proposal.adopt),
                run_cli=ctx.run_cli,
            )
        except (EngineError, BackupError, ApplyError) as exc:
            msg = str(exc)
            if retry and ("changed since it was planned" in msg or "BackupError" in msg):
                warn(ctx, "a file changed while planning; retrying snapshot once...")
                return _execute_plan(retry=False)
            raise

    # 10. apply_plan with snapshot retry
    try:
        op_id = _execute_plan(retry=True)
    except EngineLockedError as exc:
        warn(ctx, f"engine is locked: {exc}")
        return 1
    except OpenOperationError:
        warn(
            ctx,
            "an earlier run did not finish; `all-agents undo` recovers it (available after M05)",
        )
        return 1
    except (ApplyError, PlanError, EngineError, BackupError) as exc:
        warn(ctx, f"engine reverted changes: {exc}")
        return 1

    # 11. report operation
    if op_id:
        say(ctx, f"initialised operation {op_id}")
        say(ctx, "the safety snapshot is kept for your last 20 operations")

    # 12. re-plan check for adopted items
    _check_adopted_replan(ctx, detections, proposal.adopt)
    return 0
