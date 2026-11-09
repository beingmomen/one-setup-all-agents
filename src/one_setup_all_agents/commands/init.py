"""Implementation of the `all-agents init` command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from ..adapters import ADAPTERS, all_adapters
from ..config_source import (
    INSTRUCTIONS_NAME,
    MCP_NAME,
    SOURCE_CONFIG_NAME,
    SourceError,
    desired_all,
    load_source,
    load_source_texts,
)
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
from ..importer import ImportProposal, propose, source_ops
from ..model import Detection, EditFile, ItemRef, Link, Plan
from ..reader import read_all
from ..snapshot import snapshot_ops
from ..ui import confirm, say, warn


def _proposals_differ(p1: ImportProposal, p2: ImportProposal) -> bool:
    """Return True if two proposals differ in instructions, servers, skills, or adopt."""
    # 1. instructions: compare core text, adopt, skipped
    if (p1.instructions is None) != (p2.instructions is None):
        return True
    if p1.instructions is not None and p2.instructions is not None:
        if p1.instructions.core != p2.instructions.core:
            return True
        if p1.instructions.adopt != p2.instructions.adopt:
            return True
        if p1.instructions.skipped != p2.instructions.skipped:
            return True
        if p1.instructions.replace_differing != p2.instructions.replace_differing:
            return True

    # 2. servers: compare servers dict
    if p1.servers != p2.servers:
        return True

    # 3. skills: compare copies and adopt
    if hasattr(p1.skills, "copies") and hasattr(p2.skills, "copies"):
        if p1.skills.copies != p2.skills.copies:
            return True
        if p1.skills.adopt != p2.skills.adopt:
            return True
        if p1.skills.conflicts != p2.skills.conflicts:
            return True
    elif p1.skills != p2.skills:
        return True

    # 4. adopt
    if p1.adopt != p2.adopt:
        return True

    return False


def _check_adopted_replan(
    ctx: CommandContext,
    detections: Sequence[Detection],
    adopted: Sequence[ItemRef],
) -> int:
    if not adopted or not (ctx.env.config_dir / SOURCE_CONFIG_NAME).exists():
        return 0
    try:
        installed_adapters = [
            a for a in all_adapters() if any(d.tool == a.id and d.installed for d in detections)
        ]
        adapter_ids = [a.id for a in installed_adapters]
        source = load_source(ctx.env.config_dir, adapter_ids)
        replan = build_plan(ctx.env, installed_adapters, desired_all(source, adapter_ids))
        adopted_set = {(r.tool, r.kind.value, r.key) for r in adopted}
        for op in replan.executable:
            if isinstance(op, Link):
                continue
            for ref in getattr(op, "claims", ()):
                if (ref.tool, ref.kind.value, ref.key) in adopted_set:
                    warn(
                        ctx,
                        f"warning: adopted item '{ref.key}' for {ref.tool} "
                        "still requires changes during sync",
                    )
        return 0
    except Exception as exc:
        warn(ctx, f"error checking adopted items: {exc}")
        return 1


def run(args: argparse.Namespace, ctx: CommandContext) -> int:
    """Run the init command: detect tools, import configs, and create source files."""
    # 1. detect_all: if no tool is installed, exit 0
    detections = detect_all(ctx.env, probe=ctx.probe)
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
    replace_differing = False if getattr(args, "yes", False) else None
    try:
        proposal = propose(
            readings,
            detections,
            from_tool=from_tool,
            replace_differing=replace_differing,
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
        if proposal.instructions.skipped:
            tools_str = ", ".join(sorted(proposal.instructions.skipped))
            say(
                ctx,
                f"    differing instructions in {tools_str} left unowned "
                "(status will show conflicts; to adopt later: "
                "edit the file to match, or delete it, then sync)",
            )
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

    # 9. build and apply plan with retry
    installed_tool_ids = [d.tool for d in detections if d.installed and d.tool]

    resolved_from_tool = from_tool
    if (
        resolved_from_tool is None
        and proposal.instructions is not None
        and getattr(proposal.instructions, "from_tool", None) is not None
    ):
        resolved_from_tool = proposal.instructions.from_tool

    resolved_replace = (
        proposal.instructions.replace_differing if proposal.instructions is not None else None
    )

    def _validate_source_ops(ops: Sequence[object]) -> None:
        config_text: str | None = None
        mcp_text: str | None = None
        instructions_text: str | None = None
        appendices_map: dict[str, str] = {}
        for op in ops:
            if isinstance(op, EditFile):
                if op.path == ctx.env.config_dir / SOURCE_CONFIG_NAME:
                    config_text = op.transform(None).decode("utf-8")
                elif op.path == ctx.env.config_dir / MCP_NAME:
                    mcp_text = op.transform(None).decode("utf-8")
                elif op.path == ctx.env.config_dir / INSTRUCTIONS_NAME:
                    instructions_text = op.transform(None).decode("utf-8")
                elif (
                    op.path.parent == ctx.env.config_dir
                    and op.path.name.startswith("instructions.")
                    and op.path.name.endswith(".md")
                ):
                    tid = op.path.name[len("instructions.") : -len(".md")]
                    appendices_map[tid] = op.transform(None).decode("utf-8")

        if config_text is None:
            raise SourceError(f"source config file '{SOURCE_CONFIG_NAME}' not found")

        load_source_texts(
            config_toml=config_text,
            mcp_jsonc=mcp_text,
            instructions=instructions_text,
            appendices=appendices_map,
            valid_tool_ids=installed_tool_ids,
        )

    def _prepare_and_apply(proposal_to_use: ImportProposal) -> tuple[str, ImportProposal]:
        curr_src_ops = source_ops(ctx.env, proposal_to_use, installed_tool_ids)
        _validate_source_ops(curr_src_ops)

        sn_ops, sn_messages = snapshot_ops(ctx.env, adapters_by_id, detections)
        for msg in sn_messages:
            warn(ctx, msg)

        combined_ops = list(sn_ops) + list(curr_src_ops)
        plan = Plan(tuple(combined_ops))
        curr_op_id = apply_plan(
            ctx.env,
            plan,
            label="init",
            adopt=tuple(proposal_to_use.adopt),
            run_cli=ctx.run_cli,
            pinned=True,
            warn=lambda msg: warn(ctx, msg),
        )
        return curr_op_id, proposal_to_use

    # 10. apply_plan with snapshot retry
    try:
        try:
            op_id, final_proposal = _prepare_and_apply(proposal)
        except (BackupError, ApplyError) as exc:
            if isinstance(exc, BackupError) or "changed since it was planned" in str(exc):
                warn(ctx, "a file changed while planning; retrying snapshot once...")
                curr_readings = read_all(ctx.env, ADAPTERS, detections)
                try:
                    retried_proposal = propose(
                        curr_readings,
                        detections,
                        from_tool=resolved_from_tool,
                        replace_differing=resolved_replace,
                        ask=None,
                        env=ctx.env,
                        adapters=adapters_by_id,
                    )
                except ValueError as val_exc:
                    warn(ctx, f"files changed during init: {val_exc}")
                    return 1

                if _proposals_differ(proposal, retried_proposal):
                    warn(ctx, "files changed during init")
                    return 1

                op_id, final_proposal = _prepare_and_apply(retried_proposal)
            else:
                raise
    except EngineLockedError as exc:
        warn(ctx, f"engine is locked: {exc}")
        return 1
    except OpenOperationError:
        warn(
            ctx,
            "an earlier run did not finish; `all-agents undo` recovers it",
        )
        return 1
    except SourceError as exc:
        warn(ctx, f"proposed source is invalid: {exc}")
        return 1
    except (ApplyError, PlanError, EngineError, BackupError) as exc:
        warn(ctx, f"engine reverted changes: {exc}")
        return 1

    # 11. report operation
    if op_id:
        say(ctx, f"initialised operation {op_id}")
        say(ctx, "the safety snapshot is kept permanently")

    # 12. re-plan check for adopted items
    return _check_adopted_replan(ctx, detections, final_proposal.adopt)
