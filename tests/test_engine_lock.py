"""engine/lock.py: one run at a time, fail fast, no stale lock (ADR 0010)."""

import os
import subprocess
import sys
import time
import unittest

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import POSIX, EngineCase
from one_setup_all_agents.engine.errors import EngineLockedError
from one_setup_all_agents.engine.lock import engine_lock

HOLDER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from one_setup_all_agents.engine.lock import engine_lock
with engine_lock(Path(sys.argv[2])):
    print("held", flush=True)
    sys.stdin.readline()
"""


class LockTests(EngineCase):
    def test_a_second_concurrent_acquire_fails_fast_with_a_clear_error(self):
        with engine_lock(self.state):
            started = time.monotonic()
            with self.assertRaises(EngineLockedError) as ctx:
                with engine_lock(self.state):
                    self.fail("the second lock must not be granted")
            self.assertLess(time.monotonic() - started, 2.0, "it must not wait")
        message = str(ctx.exception)
        self.assertIn("another all-agents run holds the lock", message)
        self.assertIn(f"pid {os.getpid()}", message)

    def test_the_lock_is_free_again_after_the_block_even_after_an_error(self):
        with self.assertRaises(RuntimeError):
            with engine_lock(self.state):
                raise RuntimeError("boom")
        with engine_lock(self.state):
            pass

    def test_a_timeout_waits_that_long_and_then_fails(self):
        with engine_lock(self.state):
            started = time.monotonic()
            with self.assertRaises(EngineLockedError):
                with engine_lock(self.state, timeout=0.3):
                    pass
            self.assertGreaterEqual(time.monotonic() - started, 0.25)

    def test_a_leftover_lock_file_with_a_dead_holder_is_not_a_stale_lock(self):
        self.state.mkdir(parents=True)
        (self.state / "lock").write_bytes(
            b" " * 16 + b'{"pid": 999999, "started": "2020-01-01T00:00:00Z"}'
        )
        with engine_lock(self.state):  # nothing holds the OS lock, so it is granted
            pass

    def test_another_process_holding_the_lock_blocks_this_one(self):
        src = str(_bootstrap.SRC)
        child = subprocess.Popen(
            [sys.executable, "-c", HOLDER, src, str(self.state)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

        def reap():
            child.kill()
            child.wait()
            child.stdin.close()
            child.stdout.close()

        self.addCleanup(reap)
        self.assertEqual(child.stdout.readline().strip(), "held")
        with self.assertRaises(EngineLockedError) as ctx:
            with engine_lock(self.state):
                pass
        self.assertIn(f"pid {child.pid}", str(ctx.exception))
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=30)
        with engine_lock(self.state):  # the dead process left nothing behind
            pass

    def test_the_lock_file_is_private_on_posix(self):
        if not POSIX:
            self.skipTest("permission bits are a POSIX concept")
        with engine_lock(self.state):
            self.assertEqual(self.mode(self.state / "lock"), 0o600)
            self.assertEqual(self.mode(self.state), 0o700)


if __name__ == "__main__":
    unittest.main()
