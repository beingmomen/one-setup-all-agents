"""ADR 0006: atomic writes keep permissions, write through symlinks, refuse stale writes and leave no temp files."""

import hashlib
import os
import stat
import sys
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import apply as applymod
from one_setup_all_agents.engine import atomic
from support import WorkDirCase

POSIX = not sys.platform.startswith("win")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def can_symlink(tmp):
    probe = tmp / "probe-link"
    try:
        os.symlink(tmp, probe)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


class AtomicWriteTests(WorkDirCase):
    def setUp(self):
        self.dir = self.make_home()

    def leftovers(self):
        return [p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")]

    def test_creates_and_replaces_content(self):
        f = self.dir / "a" / "b.txt"
        atomic.atomic_write(f, b"one")
        self.assertEqual(f.read_bytes(), b"one")
        atomic.atomic_write(f, b"two")
        self.assertEqual(f.read_bytes(), b"two")
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(POSIX, "permission bits are a POSIX concept")
    def test_existing_permissions_are_preserved(self):
        f = self.dir / "private.json"
        f.write_bytes(b"old")
        os.chmod(f, 0o600)
        atomic.atomic_write(f, b"new")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        g = self.dir / "script.sh"
        g.write_bytes(b"old")
        os.chmod(g, 0o755)
        atomic.atomic_write(g, b"new")
        self.assertEqual(stat.S_IMODE(g.stat().st_mode), 0o755)

    @unittest.skipUnless(POSIX, "permission bits are a POSIX concept")
    def test_new_file_uses_the_umask_default_and_sensitive_is_0600(self):
        old = os.umask(0o022)
        try:
            plain = self.dir / "plain.txt"
            atomic.atomic_write(plain, b"x")
            secret = self.dir / "secret.json"
            atomic.atomic_write(secret, b"x", sensitive=True)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(plain.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)

    def test_a_symlink_is_written_through_and_stays_a_symlink(self):
        if not can_symlink(self.dir):
            self.skipTest("symlinks are not available here")
        real_dir = self.dir / "dotfiles"
        real_dir.mkdir()
        real = real_dir / "config.json"
        real.write_bytes(b"old")
        link = self.dir / "config.json"
        os.symlink(real, link)
        atomic.atomic_write(link, b"new")
        self.assertTrue(link.is_symlink(), "the link was replaced by a regular file")
        self.assertEqual(real.read_bytes(), b"new")
        self.assertEqual(os.path.realpath(link), os.path.realpath(real))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual([p.name for p in real_dir.iterdir()], ["config.json"])

    def test_a_dangling_symlink_is_an_error(self):
        if not can_symlink(self.dir):
            self.skipTest("symlinks are not available here")
        link = self.dir / "dangling.json"
        os.symlink(self.dir / "missing-target.json", link)
        with self.assertRaises(atomic.AtomicWriteError):
            atomic.atomic_write(link, b"x")
        self.assertFalse((self.dir / "missing-target.json").exists())

    def test_expect_sha256_refuses_a_file_that_changed(self):
        f = self.dir / "c.txt"
        f.write_bytes(b"planned")
        f.write_bytes(b"someone else wrote this")
        with self.assertRaises(atomic.AtomicWriteError):
            atomic.atomic_write(f, b"new", expect_sha256=sha(b"planned"))
        self.assertEqual(f.read_bytes(), b"someone else wrote this")
        atomic.atomic_write(f, b"new", expect_sha256=sha(b"someone else wrote this"))
        self.assertEqual(f.read_bytes(), b"new")

    def test_expect_none_means_the_file_must_not_exist(self):
        f = self.dir / "d.txt"
        atomic.atomic_write(f, b"first", expect_sha256=None)
        with self.assertRaises(atomic.AtomicWriteError):
            atomic.atomic_write(f, b"second", expect_sha256=None)
        self.assertEqual(f.read_bytes(), b"first")

    def test_a_failed_replace_leaves_the_target_and_no_temp_file(self):
        f = self.dir / "e.txt"
        f.write_bytes(b"keep me")
        with mock.patch.object(atomic.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic.atomic_write(f, b"lost")
        self.assertEqual(f.read_bytes(), b"keep me")
        self.assertEqual(self.leftovers(), [])

    def test_windows_style_retry_on_a_busy_file(self):
        f = self.dir / "f.txt"
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("busy")
            return real_replace(src, dst)

        with (
            mock.patch.object(atomic.sys, "platform", "win32"),
            mock.patch.object(atomic.os, "replace", side_effect=flaky),
            mock.patch.object(atomic.time, "sleep"),
        ):
            atomic.atomic_write(f, b"done")
        self.assertEqual(len(calls), 3)
        self.assertEqual(f.read_bytes(), b"done")

    def test_giving_up_after_the_retries_is_an_error_not_a_partial_write(self):
        f = self.dir / "g.txt"
        f.write_bytes(b"old")
        with (
            mock.patch.object(atomic.sys, "platform", "win32"),
            mock.patch.object(atomic.os, "replace", side_effect=PermissionError("busy")),
            mock.patch.object(atomic.time, "sleep"),
        ):
            with self.assertRaises(PermissionError):
                atomic.atomic_write(f, b"new")
        self.assertEqual(f.read_bytes(), b"old")
        self.assertEqual(self.leftovers(), [])


class CoreUsesTheAtomicWriteTests(WorkDirCase):
    """The ported core's own `atomic_write` is the ADR 0006 implementation (permissions kept, links followed)."""

    @unittest.skipUnless(POSIX, "permission bits are a POSIX concept")
    def test_core_write_keeps_permissions(self):
        f = self.make_home() / "x.json"
        f.write_bytes(b"{}")
        os.chmod(f, 0o600)
        applymod.atomic_write(f, b'{"a": 1}')
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)

    def test_core_write_follows_a_symlink(self):
        d = self.make_home()
        if not can_symlink(d):
            self.skipTest("symlinks are not available here")
        real = d / "real.json"
        real.write_bytes(b"{}")
        link = d / "link.json"
        os.symlink(real, link)
        applymod.atomic_write(link, b'{"a": 1}')
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_bytes(), b'{"a": 1}')

    def test_core_write_reports_a_dangling_link_as_an_apply_error(self):
        d = self.make_home()
        if not can_symlink(d):
            self.skipTest("symlinks are not available here")
        link = d / "dangling.json"
        os.symlink(d / "nope.json", link)
        with self.assertRaises(applymod.ApplyError):
            applymod.atomic_write(link, b"x")


if __name__ == "__main__":
    unittest.main()
