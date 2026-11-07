"""Shared Phase 0 stub behavior for the four adapters.

Every method raises NotImplementedError with a message naming the tool and the
method, so a half-wired call fails loudly. This file disappears as each adapter
is ported (Phase 3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..model import (
    Detection,
    Env,
    Instructions,
    McpServer,
    Op,
    Owned,
    Paths,
    Scope,
    SkillRef,
)
from .base import ToolAdapter


class StubAdapter(ToolAdapter):
    """A `ToolAdapter` whose required methods are not implemented yet."""

    def _todo(self, method: str) -> NotImplementedError:
        return NotImplementedError(
            f"{self.label} adapter: {method}() is not implemented yet (pre-alpha, Phase 3)."
        )

    def detect(self, env: Env) -> Detection:
        raise self._todo("detect")

    def paths(self, env: Env, scope: Scope) -> Paths:
        raise self._todo("paths")

    def read_instructions(self, env: Env, scope: Scope) -> Instructions | None:
        raise self._todo("read_instructions")

    def read_mcp(self, env: Env, scope: Scope) -> dict[str, McpServer]:
        raise self._todo("read_mcp")

    def read_skills(self, env: Env, scope: Scope) -> list[SkillRef]:
        raise self._todo("read_skills")

    def plan_instructions(self, env: Env, scope: Scope, desired: str, owned: Owned) -> list[Op]:
        raise self._todo("plan_instructions")

    def plan_mcp(
        self, env: Env, scope: Scope, desired: Mapping[str, McpServer], owned: Owned
    ) -> list[Op]:
        raise self._todo("plan_mcp")

    def plan_skills(
        self, env: Env, scope: Scope, desired: Sequence[SkillRef], owned: Owned
    ) -> list[Op]:
        raise self._todo("plan_skills")
