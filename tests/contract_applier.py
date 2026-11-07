"""How the adapter contract suite applies and undoes a plan: through the REAL engine.

`make_applier(env, run_cli)` is the one seam. It returns an `EngineApplier`, which calls
`engine.apply.apply_plan` and `engine.revert.revert_operation` (docs/decisions/0005, 0010). There
is no stand-in any more: ops are applied with the journal, the backups, the atomic write, the
drift preflight and the ledger, so the contract suite exercises the real `undo` machinery.

What passing the suite against this seam proves, and what it does not, is written down in
docs/decisions/0010-engine-v1.md ("What the contract suite proves"). In short: for the neutral
example adapters, in a temporary home, on the OS the tests run on, every apply is undone to the
exact files, folders, links and ownership it started from. It does not prove anything about a
real tool's own behavior, nor the `undo` COMMAND (Phase 5).

`RunCli` ops are never run as real programs. The caller passes `run_cli`, a small in-process fake
(see `example_adapter.FakeExampleCli`); the engine runs the op's `inverse_argv` through the same
fake on a revert.

This module also holds three small link helpers the tests use to look at the disk. They do not use
the engine on purpose: a test that inspects the engine's result must not rely on the engine.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core.fsutil import is_junction
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import Env, Plan

RunCliFake = Callable[[Sequence[str]], int]


# -- helpers that inspect the disk without the engine ------------------------------------------


def is_link(path: Path) -> bool:
    return Path(path).is_symlink() or is_junction(path)


def link_target(path: Path) -> Path:
    """Where a link or junction points, fully resolved."""
    return Path(os.path.realpath(path))


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- THE SEAM --------------------------------------------------------------------------------


class EngineApplier:
    """Applies a plan with the real engine; the receipt is the id of the journal operation."""

    def __init__(self, env: Env, run_cli: RunCliFake | None = None) -> None:
        self.env = env
        self.run_cli = run_cli

    def apply(self, plan: Plan) -> str | None:
        """Run the plan. Returns the operation id, or None if the plan had nothing to execute."""
        return apply_plan(self.env, plan, label="contract", run_cli=self._runner())

    def undo(self, receipt: str | None) -> None:
        """Put back everything the operation changed (a no-op for a plan with nothing to run)."""
        if receipt is not None:
            revert_operation(self.env, receipt, run_cli=self._runner())

    def _runner(self) -> RunCliFake:
        # A plan with RunCli ops and no fake CLI must fail loudly, never run a real program.
        return self.run_cli or _no_cli


def _no_cli(argv: Sequence[str]) -> int:
    raise AssertionError(f"the plan runs {list(argv)[:3]} but the fixture provides no fake CLI")


def make_applier(env: Env, run_cli: RunCliFake | None = None) -> EngineApplier:
    """Return the object the contract suite applies and undoes plans with: the real engine."""
    return EngineApplier(env, run_cli)
