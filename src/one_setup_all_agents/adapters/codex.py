"""Codex adapter (stub).

What the port must cover (v0.1, global scope):

- instructions: the global `AGENTS.md` under the Codex home directory
  (honor `CODEX_HOME` when set).
- MCP: servers in the Codex `config.toml`. Edit only a managed region of the TOML
  so the user's other settings and comments are untouched.
- skills: the shared agents skills directory.

Known file locations are verified on Windows only; confirm them on Linux and macOS
with a fixture before relying on them.
"""

from __future__ import annotations

from typing import ClassVar

from ..model import Kind
from ._stub import StubAdapter


class CodexAdapter(StubAdapter):
    id: ClassVar[str] = "codex"
    label: ClassVar[str] = "Codex"
    capabilities: ClassVar[frozenset[Kind]] = frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})
