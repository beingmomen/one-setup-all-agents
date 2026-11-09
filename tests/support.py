"""Shared test helpers for the ported core.

Every test works in a temporary directory and a fake home built from `tests/fixtures/home`; nothing here
reads or writes the real home directory, and nothing reads files from outside this repository.
"""

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)

HERE = Path(__file__).resolve().parent
SRC = _bootstrap.SRC
FIXTURE_HOME = HERE / "fixtures" / "home"

# One scratch folder per test PROCESS, inside the system temp directory (never inside the repository).
WORK = Path(tempfile.gettempdir()) / "one-setup-all-agents-tests" / f"p{os.getpid()}"

# Every one of these must never appear in any output.
FIXTURE_SECRETS = [
    "FAKE-ENV-SECRET-aaa111",
    "FAKE-ARG-SECRET-bbb222",
    "FAKE-URL-SECRET-ccc333",
    "FAKE-HEADER-SECRET-ddd444",
]
FIXTURE_ENV_VALUES = ["FAKE-UA-VALUE", "FAKE-PATH-MARKER", "%NVM_HOME%"]


def cli_env(extra=None):
    """Environment for a child process: the package importable from src/, no user overrides leaking in."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ALL_AGENTS_")}
    env["PYTHONPATH"] = os.pathsep.join([str(SRC), *filter(None, [env.get("PYTHONPATH")])])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def digest(root):
    h = {}
    for f in sorted(Path(root).rglob("*")):
        if f.is_file():
            h[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return h


class WorkDirCase(unittest.TestCase):
    def make_home(self):
        WORK.mkdir(parents=True, exist_ok=True)
        d = Path(tempfile.mkdtemp(dir=WORK))
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def copy_fixture_home(self):
        d = self.make_home() / "home"
        shutil.copytree(FIXTURE_HOME, d)
        return d


def cleanup_work():
    shutil.rmtree(WORK, ignore_errors=True)
    try:
        WORK.parent.rmdir()
    except OSError:
        pass
