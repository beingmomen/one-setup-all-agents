"""TEST-ONLY STAND-IN for the engine's apply and undo.  Not part of the product.

The real engine (`src/one_setup_all_agents/engine/`) is still a set of stubs. The adapter
contract suite (`adapter_contract.py`) needs to apply the ops an adapter plans and to undo them,
so this module does a minimal version of that with plain file operations.

What this means, plainly:

- It proves that an adapter's ops are well-formed and reversible *as far as this stand-in can
  tell*. It does NOT prove the `undo` guarantee of docs/decisions/0005: there is no journal, no
  backup store, no drift check, no atomic write, no permission preservation, and links are
  always plain links (no copy fallback).
- The undo guarantee is not proven until `make_applier` below points at the real engine and the
  whole contract suite passes against it.

The ONE SEAM: `make_applier(env, run_cli)`.  When `engine/` exists, change only this function so
that it returns an object with the same two methods:

    apply(plan) -> receipt        run every executable op of the plan, return something undo needs
    undo(receipt) -> None         put every path the receipt covers back to its earlier state

Nothing else in the tests may import `StandInApplier` directly.

`RunCli` ops are never run as real programs. The caller passes `run_cli`, a small in-process fake
(see `example_adapter.FakeExampleCli`); `undo` runs the op's `inverse_argv` through the same fake.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core.fsutil import is_junction
from one_setup_all_agents.model import EditFile, Env, Link, Plan, RunCli

RunCliFake = Callable[[Sequence[str]], int]


class ApplyError(AssertionError):
    """The stand-in refused an op. An AssertionError so that a contract run reports a failure."""


# -- link helpers (no administrator rights on Windows: directory junction, as in ADR 0007) ------


def is_link(path: Path) -> bool:
    return Path(path).is_symlink() or is_junction(path)


def link_target(path: Path) -> Path:
    """Where a link or junction points, fully resolved."""
    return Path(os.path.realpath(path))


def make_link(link: Path, target: Path) -> None:
    if sys.platform.startswith("win"):
        import _winapi  # CPython on Windows only

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def remove_link(link: Path) -> None:
    if is_junction(link) or (sys.platform.startswith("win") and Path(link).is_dir()):
        os.rmdir(link)  # removes the junction, never the target
    else:
        os.unlink(link)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- the stand-in ----------------------------------------------------------------------------


class StandInApplier:
    """Applies the executable ops of a plan and returns a receipt that `undo` reverses."""

    def __init__(self, env: Env, run_cli: RunCliFake | None = None) -> None:
        self.env = env
        self._run_cli = run_cli

    def apply(self, plan: Plan) -> list[Callable[[], None]]:
        receipt: list[Callable[[], None]] = []
        try:
            for op in plan.executable:
                receipt.append(self._apply_one(op))
        except BaseException:
            self.undo(receipt)  # like the real engine: a failed run leaves nothing half done
            raise
        return receipt

    def undo(self, receipt: list[Callable[[], None]]) -> None:
        for reverse in reversed(receipt):
            reverse()

    # -- one op ---------------------------------------------------------------------------

    def _apply_one(self, op) -> Callable[[], None]:
        if isinstance(op, EditFile):
            return self._edit_file(op)
        if isinstance(op, Link):
            return self._link(op)
        if isinstance(op, RunCli):
            return self._cli(op)
        raise ApplyError(f"the stand-in cannot run {type(op).__name__}")

    def _edit_file(self, op: EditFile) -> Callable[[], None]:
        path = Path(op.path)
        old = path.read_bytes() if path.is_file() else None
        current = None if old is None else sha256_of(old)
        if current != op.expect_sha256:
            raise ApplyError(
                f"{path.name}: expect_sha256 does not match the file on disk "
                f"(expected {op.expect_sha256}, found {current})"
            )
        new = op.transform(old)
        if not isinstance(new, bytes):
            raise ApplyError(f"{path.name}: transform must return bytes")
        created = _missing_ancestors(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(new)

        def reverse() -> None:
            if old is None:
                path.unlink()
            else:
                path.write_bytes(old)
            _remove_empty_dirs(created)

        return reverse

    def _link(self, op: Link) -> Callable[[], None]:
        link = Path(op.link)
        prior: Path | None = None
        if os.path.lexists(link):
            if not is_link(link):
                raise ApplyError(f"{link.name}: refusing to replace a real file or folder")
            prior = link_target(link)
            remove_link(link)
        elif op.target is None:
            raise ApplyError(f"{link.name}: nothing to remove")
        created: list[Path] = []
        if op.target is not None:
            created = _missing_ancestors(link)
            link.parent.mkdir(parents=True, exist_ok=True)
            make_link(link, Path(op.target))

        def reverse() -> None:
            if op.target is not None:
                remove_link(link)
            if prior is not None:
                make_link(link, prior)
            _remove_empty_dirs(created)

        return reverse

    def _cli(self, op: RunCli) -> Callable[[], None]:
        if self._run_cli is None:
            raise ApplyError("the plan has a RunCli op but the fixture provides no fake CLI")
        if not op.inverse_argv:
            raise ApplyError(f"RunCli {list(op.argv)} has no inverse_argv")
        self._check(self._run_cli(list(op.argv)), op.argv)

        def reverse() -> None:
            self._check(self._run_cli(list(op.inverse_argv)), op.inverse_argv)

        return reverse

    @staticmethod
    def _check(code: int, argv: Sequence[str]) -> None:
        if code != 0:
            raise ApplyError(f"the fake CLI refused {list(argv)[:4]} (exit code {code})")


def _missing_ancestors(path: Path) -> list[Path]:
    """Folders above `path` that do not exist yet, deepest first."""
    missing = []
    for parent in Path(path).parents:
        if parent.exists():
            break
        missing.append(parent)
    return missing


def _remove_empty_dirs(dirs: list[Path]) -> None:
    for directory in dirs:  # deepest first
        try:
            directory.rmdir()
        except OSError:
            pass


# -- THE SEAM --------------------------------------------------------------------------------


def make_applier(env: Env, run_cli: RunCliFake | None = None) -> StandInApplier:
    """Return the object the contract suite applies and undoes plans with.

    Swap the body of this function for the real engine when `engine/` is implemented.
    """
    return StandInApplier(env, run_cli)
