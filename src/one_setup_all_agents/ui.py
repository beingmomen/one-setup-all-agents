"""User interaction helpers for command outputs, prompts, and selections.

Masks sensitive strings and home paths, and respects interactivity flags.
"""

from __future__ import annotations

from collections.abc import Sequence

from ._core.mask import mask_text
from .context import CommandContext
from .detect import mask_home


def _mask_multiline(text: str, ctx: CommandContext) -> str:
    """Mask secrets and home paths line by line to handle anchored tokens."""
    return "\n".join(mask_home(mask_text(line), ctx.env) for line in text.split("\n"))


def say(ctx: CommandContext, text: str) -> None:
    """Print an informational line to ctx.out with secrets and home paths masked."""
    masked = _mask_multiline(text, ctx)
    if not masked.endswith("\n"):
        masked += "\n"
    ctx.out.write(masked)
    ctx.out.flush()


def warn(ctx: CommandContext, text: str) -> None:
    """Print a warning line to ctx.err with secrets and home paths masked."""
    masked = _mask_multiline(text, ctx)
    if not masked.endswith("\n"):
        masked += "\n"
    ctx.err.write(masked)
    ctx.err.flush()


def confirm(ctx: CommandContext, question: str) -> bool:
    """Ask a yes/no question. Returns False when non-interactive."""
    if not ctx.is_interactive:
        say(ctx, "not asked: not interactive; pass --yes")
        return False
    ans = ctx.read_line(f"{question} [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def choose(ctx: CommandContext, question: str, options: Sequence[str]) -> int | None:
    """Ask the user to choose from a 1-based numbered list of options."""
    if not ctx.is_interactive or not options:
        return None
    say(ctx, question)
    for i, opt in enumerate(options, 1):
        say(ctx, f"  {i}. {opt}")
    ans = ctx.read_line("Choose an option: ").strip()
    try:
        choice = int(ans)
        if 1 <= choice <= len(options):
            return choice
    except ValueError:
        pass
    return None
