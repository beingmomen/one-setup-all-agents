"""Antigravity adapter (stub). The tool's command is `agy`; its adapter id is `agy`.

The most tool-specific of the four. What the port must cover (v0.1, global scope):

- instructions: Antigravity enforces a size limit on its instructions, so the
  planner must check the rendered size and report an overflow instead of writing.
- MCP: servers in the Antigravity MCP config file (needs environment-variable
  expansion at launch time because the tool does not expand them itself).
- skills: Antigravity does not follow Windows junctions. On Windows the adapter
  must use the copy-plus-manifest path (docs/decisions/0007). Behavior of
  symlinks on Linux and macOS is unverified.
- notes(): no memory key; hooks use different event names.

Known file locations are verified on Windows only; confirm them on Linux and macOS
with a fixture before relying on them.
"""

from __future__ import annotations

from typing import ClassVar

from ..model import Kind
from ._stub import StubAdapter


class AntigravityAdapter(StubAdapter):
    id: ClassVar[str] = "agy"
    label: ClassVar[str] = "Antigravity"
    capabilities: ClassVar[frozenset[Kind]] = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})
