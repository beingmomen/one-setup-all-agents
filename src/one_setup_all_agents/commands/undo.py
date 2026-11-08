"""Implementation of the `all-agents undo` command."""

from __future__ import annotations

import argparse

from ..context import CommandContext
from ..detect import mask_home
from ..engine import journal
from ..engine import undo as undo_mod
from ..engine.errors import EngineLockedError, OpenOperationError, RevertError
from ..ui import say, warn


def run(args: argparse.Namespace, ctx: CommandContext) -> int:
    """Run the undo command: reverse operations from the write-ahead journal."""
    if getattr(args, "list", False):
        if not ctx.env.state_dir.is_dir():
            say(ctx, "No undoable operations.")
            return 0

        undoable = journal.list_undoable(ctx.env.state_dir)
        if not undoable:
            say(ctx, "No undoable operations.")
            return 0

        lines = []
        for item in undoable:
            op_id = item["id"]
            label = item.get("label", "")
            created = item.get("created", "")
            try:
                op_obj = journal.load(ctx.env.state_dir, op_id)
                entry_count = len(op_obj.entries)
            except Exception:
                entry_count = 0
            lines.append(f"{op_id}: {label} ({created}, {entry_count} entries)")
        say(ctx, "\n".join(lines))
        return 0

    try:
        result = undo_mod.undo(
            ctx.env,
            force=getattr(args, "force", False),
            run_cli=ctx.run_cli,
        )
    except RevertError as exc:
        msg = mask_home(str(exc), ctx.env)
        warn(ctx, f"revert refused due to drift: {msg}; use --force to override")
        return 1
    except (EngineLockedError, OpenOperationError) as exc:
        warn(ctx, str(exc))
        return 1

    if result is None:
        say(ctx, "Nothing to undo.")
        return 0

    if result.recovered:
        say(ctx, f"recovered unfinished operation(s): {', '.join(result.recovered)}")

    say(ctx, f"undone operation {result.op_id} ({result.label})")
    say(ctx, f"{result.remaining} more can be undone")
    return 0
