"""Command execution context for all-agents CLI commands.

Holds the environment, I/O streams, runner overrides, and interaction flags.
Injectable by tests to verify commands without touching the real environment.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .detect import ProbeRunner
from .engine.runcli import Runner
from .model import Env
from .paths import build_env


@dataclass
class CommandContext:
    """Execution context injected into every CLI subcommand."""

    env: Env
    run_cli: Runner | None = None
    probe: ProbeRunner | None = None
    read_line: Callable[[str], str] = input
    is_interactive: bool = field(default_factory=lambda: sys.stdin.isatty() and sys.stdout.isatty())
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)


def default_context(args: argparse.Namespace | None = None) -> CommandContext:
    """Build the standard runtime context from OS environment and CLI arguments."""
    env = build_env()
    if args is not None and getattr(args, "config_dir", None):
        env = dataclasses.replace(env, config_dir=Path(args.config_dir).resolve())
    return CommandContext(env=env)
