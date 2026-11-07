"""Run a tool's own CLI for a `RunCli` op, through an injectable runner.

A runner is a function `argv -> exit code`. The engine never builds a shell string and never
uses `shell=True`. Tests pass a fake runner, so no real tool ever runs in a test.

What "verified" means for a `RunCli` (docs/decisions/0010): the engine does not know the file
the tool owns, so it cannot re-read it. The command is verified when the runner returns exit
code 0; the same holds for the inverse run on a revert. Any other result, or an exception, is a
failure. Output of the command is captured and discarded: it is never printed or stored, because
it can echo secrets.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence

from ..model import Env

Runner = Callable[[Sequence[str]], int]

DEFAULT_TIMEOUT = 120.0


def default_runner(env: Env | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> Runner:
    """The real runner: `subprocess.run` with an argv list. The program is looked up on PATH.

    `shutil.which` resolves the program first, so on Windows a `tool.cmd` shim is found the way
    a shell would find it. A program that is not found returns 127 (the shell convention).
    An environment given in `env.environ` is used for the child; an empty one means "inherit".
    """
    environ = dict(env.environ) if env is not None and env.environ else None

    def run(argv: Sequence[str]) -> int:
        argv = list(argv)
        program = shutil.which(argv[0], path=(environ or {}).get("PATH")) or argv[0]
        try:
            done = subprocess.run(
                [program, *argv[1:]],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                env=environ,
                check=False,
            )
        except FileNotFoundError:
            return 127
        return done.returncode

    return run
