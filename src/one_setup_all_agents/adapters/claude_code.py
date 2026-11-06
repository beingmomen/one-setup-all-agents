"""Claude Code adapter (stub).

What the port must cover (v0.1, global scope):

- instructions: the global `CLAUDE.md` under the Claude config directory
  (honor `CLAUDE_CONFIG_DIR` when set).
- MCP: servers live in `~/.claude.json`, a file that running sessions also write.
  Writes therefore go through the tool's own CLI (`RunCli` with `claude mcp
  add-json` and `claude mcp remove` as the inverse), not by editing the file.
  If the `claude` binary is not on PATH, `detect` reports it and MCP is skipped
  with a warning instead of failing.
- skills: the Claude skills directory under the config directory.

Known file locations are verified on Windows only; confirm them on Linux and macOS
with a fixture before relying on them.
"""

from __future__ import annotations

from typing import ClassVar

from ..model import Kind
from ._stub import StubAdapter


class ClaudeCodeAdapter(StubAdapter):
    id: ClassVar[str] = "claude"
    label: ClassVar[str] = "Claude Code"
    capabilities: ClassVar[frozenset[Kind]] = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})
