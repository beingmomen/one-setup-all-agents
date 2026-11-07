"""The engine's errors. Every message is safe to print: it names paths and items, never contents.

Messages that quote a command or a description go through `_core.mask.mask_text` first.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class EngineError(Exception):
    """Base class of every error the engine raises on purpose."""


class EngineLockedError(EngineError):
    """Another engine run holds the lock of this state directory (docs/decisions/0010)."""


class PlanError(EngineError):
    """The plan cannot be executed as given (checked before anything is touched)."""


class BackupError(EngineError):
    """A path is in a state the engine cannot capture or restore (for example a real folder)."""


class LinkError(EngineError):
    """A `Link` op was refused or failed."""


class VerificationError(EngineError):
    """After an op ran, the path or the command result did not match the expected state."""


class JournalError(EngineError):
    """The journal or ledger on disk is missing, unreadable or not in the known format."""


class OpenOperationError(EngineError):
    """An earlier operation did not finish (a crash, or a revert that was interrupted).

    `apply_plan` refuses to start until `recover_operation` has put it right.
    """

    def __init__(self, op_ids: Sequence[str]) -> None:
        self.op_ids = list(op_ids)
        super().__init__(
            f"unfinished operation(s): {', '.join(self.op_ids)}. An earlier run did not complete; "
            "call engine.apply.recover_operation(env) to put its changes back before applying again"
        )


class RevertError(EngineError):
    """A revert was refused (drift) or stopped half way.

    `drifted` lists the paths whose current state differs from what the journal recorded; when it
    is set nothing was changed. `partial` is True when some entries were already put back; the
    journal keeps track, and calling the revert again continues where it stopped.
    """

    def __init__(
        self,
        message: str,
        *,
        op_id: str,
        drifted: Sequence[Path] = (),
        partial: bool = False,
    ) -> None:
        self.op_id = op_id
        self.drifted = list(drifted)
        self.partial = partial
        super().__init__(message)


class ApplyError(EngineError):
    """`apply_plan` failed. By the time this is raised the engine has already tried to revert.

    `reverted` is True when every entry done in the run was put back. When it is False the
    message says why, and `recover_operation` (or `revert_operation`) can finish the job.
    """

    def __init__(self, message: str, *, op_id: str | None, reverted: bool) -> None:
        self.op_id = op_id
        self.reverted = reverted
        super().__init__(message)
