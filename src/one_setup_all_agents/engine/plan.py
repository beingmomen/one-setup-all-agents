"""Build a reviewable plan from the adapters' ops."""

from __future__ import annotations

from collections.abc import Iterable

from ..adapters.base import ToolAdapter
from ..model import Env, Kind, Plan, Scope


def build_plan(
    env: Env,
    adapters: Iterable[ToolAdapter],
    scope: Scope = Scope.GLOBAL,
    kinds: Iterable[Kind] | None = None,
) -> Plan:
    """Ask each adapter to plan the desired state and merge the results.

    Pure: reads the source config and the tools' current state, writes nothing.
    A second call right after a successful apply must return an empty plan
    (idempotence is part of the adapter contract test).
    """
    raise NotImplementedError("engine.plan.build_plan is not implemented yet (Phase 3).")


def render_plan(plan: Plan) -> str:
    """Render a plan as human-readable text for the dry run. Secrets are masked."""
    raise NotImplementedError("engine.plan.render_plan is not implemented yet (Phase 3).")
