"""Command-line entry point: `all-agents init|sync|status|undo`.

Phase 0: the argument parser and `--help` work; every command reports that it is
not implemented yet and exits with status 2.

Planned behavior of each command (v0.1, global scope only):

    init    Detect installed tools, import what is already configured (instructions,
            MCP servers, skills), take a full safety snapshot first, and create the
            source config. Writes nothing without confirmation.
    sync    Plan, show, and (after confirmation) apply the source config to every
            detected tool. Shows a dry run by default.
    status  Show, per tool and per kind, whether the tool matches the source config.
    undo    Reverse the last operation, or a chosen one, from the unified journal.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from . import __version__

PROG = "all-agents"

# Exit status used by every stub command in Phase 0.
EXIT_NOT_IMPLEMENTED = 2


def _cmd_init(args: argparse.Namespace) -> int:
    raise NotImplementedError("`all-agents init` is not implemented yet (planned for Phase 4).")


def _cmd_sync(args: argparse.Namespace) -> int:
    raise NotImplementedError("`all-agents sync` is not implemented yet (planned for Phase 3/5).")


def _cmd_status(args: argparse.Namespace) -> int:
    raise NotImplementedError("`all-agents status` is not implemented yet (planned for Phase 5).")


def _cmd_undo(args: argparse.Namespace) -> int:
    raise NotImplementedError("`all-agents undo` is not implemented yet (planned for Phase 5).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "One setup for all your AI coding agents: a shared agent config for "
            "Claude Code, Codex, OpenCode and Antigravity. Write your config once, "
            "every agent gets the same setup, and any change can be undone with one command."
        ),
        epilog="Status: pre-alpha. The commands above are not usable yet.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--config-dir",
        metavar="DIR",
        help="use DIR as the source config directory instead of the default (planned)",
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
    init.add_argument(
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
        "--tool", action="append", metavar="TOOL", help="limit to this tool (repeatable)"
    )
    sync.add_argument(
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
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=_cmd_status)

    undo = sub.add_parser(
        "undo",
        help="reverse the last change (or a chosen one) with one command",
        description="Reverse the last operation, or a chosen one, using the journal.",
    )
    undo.add_argument("--list", action="store_true", help="list operations that can be undone")
    undo.add_argument(
        "--force", action="store_true", help="also revert files changed since the operation"
    )
    undo.set_defaults(func=_cmd_undo)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    try:
        return func(args)
    except NotImplementedError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        print(f"{PROG}: this project is pre-alpha; nothing was changed.", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
