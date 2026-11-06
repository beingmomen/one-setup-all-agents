"""OpenCode adapter (stub).

Planned to be ported first: it is the simplest of the four, so it proves the
adapter/engine interface before the harder tools.

What the port must cover (v0.1, global scope):

- instructions: the global `AGENTS.md` under the OpenCode config directory
  (honor `XDG_CONFIG_HOME` when set).
- MCP: servers in the OpenCode JSONC config. Edit the file textually so comments
  and formatting survive.
- skills: the shared agents skills directory.

Known file locations are verified on Windows only; confirm them on Linux and macOS
with a fixture before relying on them.
"""

from __future__ import annotations

from typing import ClassVar

from ..model import Kind
from ._stub import StubAdapter


class OpenCodeAdapter(StubAdapter):
    id: ClassVar[str] = "opencode"
    label: ClassVar[str] = "OpenCode"
    capabilities: ClassVar[frozenset[Kind]] = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})
