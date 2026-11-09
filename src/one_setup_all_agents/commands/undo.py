"""Implementation of the `all-agents undo` command."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..context import CommandContext
from ..detect import mask_home
from ..engine import journal
from ..engine import undo as undo_mod
from ..engine.errors import BackupError, EngineLockedError, OpenOperationError, RevertError
from ..ui import say, warn


def _has_journal(state_dir: Path) -> bool:
    if not state_dir.is_dir():
        return False
    ledger_file = state_dir / "ledger.json"
    if ledger_file.is_file():
        try:
            ledger = journal.read_ledger(state_dir)
            if ledger.get("operations"):
                return True
        except Exception:
            return True
    ops_base = state_dir / "ops"
    if ops_base.is_dir():
        try:
            if any(ops_base.iterdir()):
                return True
        except Exception:
            return True
    return False


def run(args: argparse.Namespace, ctx: CommandContext) -> int:
    """Run the undo command: reverse operations from the write-ahead journal."""
    if not _has_journal(ctx.env.state_dir):
        if getattr(args, "list", False):
            say(ctx, "No undoable operations.")
        else:
            say(ctx, "Nothing to undo.")
        return 0

    if getattr(args, "list", False):
        try:
            undoable = journal.list_undoable(ctx.env.state_dir)
        except journal.JournalError as exc:
            warn(ctx, f"cannot read ledger.json ({exc}); restore from backup or reinitialise")
            return 1
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
    except BackupError as exc:
        msg = mask_home(str(exc), ctx.env)
        warn(ctx, f"{msg} (nothing changed)")
        return 1
    except RevertError as exc:
        msg = mask_home(str(exc), ctx.env)
        if getattr(exc, "drifted", None):
            warn(ctx, f"revert refused due to drift: {msg}; use --force to override")
        else:
            warn(ctx, f"revert failed: {msg}")
        return 1
    except (EngineLockedError, OpenOperationError) as exc:
        warn(ctx, str(exc))
        return 1
    except journal.JournalError as exc:
        warn(ctx, f"cannot read ledger.json ({exc}); restore from backup or reinitialise")
        return 1

    if result is not None and result.warnings:
        for w in result.warnings:
            warn(ctx, w)

    if result is not None and result.recovered:
        say(ctx, f"recovered unfinished operation(s): {', '.join(result.recovered)}")

    if result is None or not result.op_id:
        say(ctx, "Nothing to undo.")
        return 0

    for desc in result.reverted_descriptions:
        say(ctx, f"  {desc}")

    say(ctx, f"undone operation {result.op_id} ({result.label})")
    say(ctx, f"{result.remaining} more can be undone")

    if result.skipped_cli:
        for entry in result.skipped_cli:
            desc = entry.get("description") or "inverse command"
            warn(ctx, f"inverse command failed: {desc}")
        if len(result.skipped_cli) == 1:
            warn(ctx, "one change must be removed by hand")
        else:
            warn(ctx, f"{len(result.skipped_cli)} changes must be removed by hand")
        return 1

    return 0
