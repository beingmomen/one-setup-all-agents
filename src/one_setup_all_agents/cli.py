"""Command-line entry point: `all-agents init|sync|status|undo`.

The four public commands (`init`, `sync`, `status`, `undo`) are implemented.

Exit codes (ADR 0018 §3):

- General (argparse):
  - 2: Usage error (unrecognized command or invalid arguments parsed by argparse).
- all-agents init:
  - 0: Success, dry-run completion, or no supported tools found.
  - 1: Refusal or error (already initialised, unknown tool, declined confirmation,
       engine locked, unfinished operation, or plan/apply failure).
- all-agents sync:
  - 0: Success, no-op (already in sync), dry-run without conflicts, or user-declined confirmation.
  - 1: Refusal, error, lock contention, unfinished operation, invalid flag, or failed apply.
  - 3: Unresolved conflicts remain in the plan (both during dry-run and after apply).
- all-agents status:
  - 0: In sync (no pending operations or differences).
  - 1: Error (uninitialised, corrupted source config / syntax error, or unreadable
       ledger / state error).
  - 2: Differences, conflicts, or unreadable tool configuration files exist.
- all-agents undo:
  - 0: Successful revert or nothing to undo.
  - 1: Refusal, error, unforced drift detected, or inverse command was skipped
       during `undo --force`.

Behavior of each command (v0.1, global scope only):

    init    Detect installed tools, import what is already configured (instructions,
            MCP servers, skills), take a full safety snapshot first, and create the
            source config. Writes nothing without confirmation.
    sync    Plan, show, and (after confirmation) apply the source config to every
            detected tool. Shows a dry run by default.
    status  Show, per tool and per kind, whether the tool matches the source config.
    undo    Reverse the last operation from the unified journal.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from . import __version__
from .context import CommandContext, default_context

PROG = "all-agents"


def _cmd_init(args: argparse.Namespace, ctx: CommandContext) -> int:
    from .commands import init

    return init.run(args, ctx)


def _cmd_sync(args: argparse.Namespace, ctx: CommandContext) -> int:
    from .commands import sync

    return sync.run(args, ctx)


def _cmd_status(args: argparse.Namespace, ctx: CommandContext) -> int:
    from .commands import status

    return status.run(args, ctx)


def _cmd_undo(args: argparse.Namespace, ctx: CommandContext) -> int:
    from .commands import undo

    return undo.run(args, ctx)


def _hidden_run(argv: list[str]) -> int:
    """`all-agents _run ... -- <command>`: the runner that generated MCP entries call."""
    from ._core import runner

    return runner.main(argv)


# Internal subcommands: not listed in --help and not part of the four public commands.
HIDDEN: dict[str, Callable[[list[str]], int]] = {"_run": _hidden_run}


def _tool_names() -> str:
    """The labels of the registered tools, for the help text ("A, B and C").

    Read from the registry, so a new adapter shows up here with no edit to this file. Imported
    on use, so the hidden `_run` command does not load every adapter.
    """
    from .adapters import ADAPTERS

    labels = [cls.label for cls in ADAPTERS]
    return labels[0] if len(labels) < 2 else f"{', '.join(labels[:-1])} and {labels[-1]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "One setup for all your AI coding agents: a shared agent config for "
            f"{_tool_names()}. Write your config once, "
            "every agent gets the same setup, and any change can be undone with one command."
        ),
        epilog="Status: pre-alpha.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--config-dir",
        metavar="DIR",
        help="use DIR as the source config directory instead of the default",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    init = sub.add_parser(
        "init",
        help="detect your tools and create the shared config from what you already have",
        description="Detect installed tools and create the shared config from what exists.",
    )
    init.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    init.add_argument(
        "--dry-run", action="store_true", help="show what would happen, write nothing"
    )
    adv_init = init.add_argument_group("advanced options")
    adv_init.add_argument(
        "--from", dest="from_tool", metavar="TOOL", help="import from this tool first"
    )
    init.set_defaults(func=_cmd_init)

    sync = sub.add_parser(
        "sync",
        help="apply the shared config to every tool (shows a dry run first)",
        description="Plan, show, and apply the shared config to every detected tool.",
    )
    sync.add_argument("--yes", action="store_true", help="apply without asking for confirmation")
    sync.add_argument(
        "--dry-run", action="store_true", help="show what would be done without writing anything"
    )
    adv_sync = sync.add_argument_group("advanced options")
    adv_sync.add_argument(
        "--tool", action="append", metavar="TOOL", help="limit to this tool (repeatable)"
    )
    adv_sync.add_argument(
        "--only",
        action="append",
        choices=["instructions", "mcp", "skills"],
        help="limit to this kind of config (repeatable)",
    )
    sync.set_defaults(func=_cmd_sync)

    status = sub.add_parser(
        "status",
        help="show whether each tool matches the shared config",
        description="Show, per tool and per kind, whether the tool matches the shared config.",
    )
    adv_status = status.add_argument_group("advanced options")
    adv_status.add_argument("--json", action="store_true", help="machine-readable output")
    adv_status.add_argument("--versions", action="store_true", help="probe tool versions (slower)")
    adv_status.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing when in sync, one summary line to stderr on drift, exit 0",
    )
    status.set_defaults(func=_cmd_status)


    undo = sub.add_parser(
        "undo",
        help="reverse the last change with one command",
        description="Reverse the last operation using the journal.",
    )
    adv_undo = undo.add_argument_group("advanced options")
    adv_undo.add_argument("--list", action="store_true", help="list operations that can be undone")
    adv_undo.add_argument(
        "--force", action="store_true", help="also revert files changed since the operation"
    )
    undo.set_defaults(func=_cmd_undo)

    return parser


def main(argv: Sequence[str] | None = None, *, context: CommandContext | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in HIDDEN:
        return HIDDEN[argv[0]](argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace, CommandContext], int] | None = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    ctx = context or default_context(args)
    return func(args, ctx)


if __name__ == "__main__":
    sys.exit(main())
