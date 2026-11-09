"""Helpers shared by the engine tests (`test_engine_*.py`). Not collected as tests itself.

Everything works in a temporary directory; nothing touches the real home. The fake runner never
starts a program.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.engine import fsops
from one_setup_all_agents.model import Env

POSIX = not sys.platform.startswith("win")
WINDOWS = not POSIX


def make_env(root: Path) -> Env:
    return Env(
        home=root / "home",
        platform="windows" if WINDOWS else "linux",
        environ={},
        config_dir=root / "config",
        state_dir=root / "state",
    )


def can_make_dir_link(root: Path) -> bool:
    """True when this machine can create the link kind the engine uses (junction or symlink)."""
    target, link = root / "probe-target", root / "probe-link"
    target.mkdir(exist_ok=True)
    try:
        fsops.create_dir_link(link, target)
    except (OSError, NotImplementedError):
        return False
    fsops.remove_link(link)
    return True


class FakeRunner:
    """A `RunCli` runner that records calls and returns scripted exit codes. Starts nothing."""

    def __init__(
        self,
        fail_on: Sequence[Sequence[str]] = (),
        raise_on: Sequence[str] = (),
        fail_always: bool = False,
    ):
        self.calls: list[list[str]] = []
        self.fail_on = [list(a) for a in fail_on]
        self.raise_on = list(raise_on)
        self.fail_always = fail_always

    def __call__(self, argv: Sequence[str]) -> int:
        argv = list(argv)
        self.calls.append(argv)
        if any(word in argv for word in self.raise_on):
            raise OSError("fake runner could not start")
        return 1 if (self.fail_always or argv in self.fail_on) else 0


class EngineCase(unittest.TestCase):
    """A fresh temporary root with `home`, `config` and `state` folders per test."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="osaa-engine-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = make_env(self.root)
        for directory in (self.env.home, self.env.config_dir):
            directory.mkdir(parents=True)
        self.home = self.env.home
        self.state = self.env.state_dir

    def write(self, path: Path, data: bytes, mode: int | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mode is not None and POSIX:
            os.chmod(path, mode)
        return path

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    def listing(self, base: Path | None = None) -> dict[str, tuple]:
        """Every folder, file (bytes + mode on POSIX) and link under `base` (not the state dir)."""
        base = base or self.root
        out: dict[str, tuple] = {}
        skip = os.path.normcase(str(self.state))

        def walk(directory: Path) -> None:
            for name in sorted(os.listdir(directory)):
                path = directory / name
                if os.path.normcase(str(path)) == skip:
                    continue
                rel = path.relative_to(base).as_posix()
                if fsops.link_kind(path) in ("symlink", "junction"):
                    out[rel] = ("link", os.path.normcase(os.path.realpath(path)))
                elif path.is_dir():
                    out[rel] = ("dir",)
                    walk(path)
                else:
                    out[rel] = ("file", path.read_bytes(), self.mode(path) if POSIX else None)

        walk(base)
        return out
