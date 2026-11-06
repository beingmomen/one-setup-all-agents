"""Tool adapters, one module per tool, plus the registry.

To add a tool: write `adapters/<tool>.py` (a `ToolAdapter` subclass), add it to
`ADAPTERS` below, and add a fixture and the contract test. See CONTRIBUTING.md.
"""

from __future__ import annotations

from .antigravity import AntigravityAdapter
from .base import ToolAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .opencode import OpenCodeAdapter

#: Registry in porting order: OpenCode first (simplest), Antigravity last (most specific).
ADAPTERS: tuple[type[ToolAdapter], ...] = (
    OpenCodeAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    AntigravityAdapter,
)


def all_adapters() -> list[ToolAdapter]:
    """One instance of every registered adapter."""
    return [cls() for cls in ADAPTERS]


def get_adapter(tool_id: str) -> ToolAdapter:
    """Return the adapter whose `id` is `tool_id`, or raise KeyError."""
    for cls in ADAPTERS:
        if cls.id == tool_id:
            return cls()
    raise KeyError(tool_id)


__all__ = ["ADAPTERS", "ToolAdapter", "all_adapters", "get_adapter"]
