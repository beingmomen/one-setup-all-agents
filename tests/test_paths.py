"""ADR 0002: config and state locations per OS, overrides, and isolation of the fake home."""

import os
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents import paths
from one_setup_all_agents._core import locations
from support import WorkDirCase

HOME = Path("/h") if os.name != "nt" else Path("C:/h")


class PathsTests(unittest.TestCase):
    def test_windows_defaults_use_appdata(self):
        self.assertEqual(
            paths.config_dir({}, "windows", HOME),
            HOME / "AppData" / "Roaming" / "one-setup-all-agents",
        )
        self.assertEqual(
            paths.state_dir({}, "windows", HOME),
            HOME / "AppData" / "Local" / "one-setup-all-agents",
        )

    def test_windows_honors_appdata_variables(self):
        appdata = HOME / "custom-roaming"
        local = HOME / "custom-local"
        env = {"APPDATA": str(appdata), "LOCALAPPDATA": str(local)}
        self.assertEqual(paths.config_dir(env, "windows", HOME), appdata / "one-setup-all-agents")
        self.assertEqual(paths.state_dir(env, "windows", HOME), local / "one-setup-all-agents")

    def test_linux_and_macos_share_the_xdg_defaults(self):
        for platform in ("linux", "macos"):
            self.assertEqual(
                paths.config_dir({}, platform, HOME), HOME / ".config" / "one-setup-all-agents"
            )
            self.assertEqual(
                paths.state_dir({}, platform, HOME),
                HOME / ".local" / "state" / "one-setup-all-agents",
            )

    def test_xdg_variables_are_honored_when_absolute(self):
        cfg, state = HOME / "xc", HOME / "xs"
        env = {"XDG_CONFIG_HOME": str(cfg), "XDG_STATE_HOME": str(state)}
        self.assertEqual(paths.config_dir(env, "linux", HOME), cfg / "one-setup-all-agents")
        self.assertEqual(paths.state_dir(env, "linux", HOME), state / "one-setup-all-agents")

    def test_relative_xdg_values_are_ignored(self):
        env = {"XDG_CONFIG_HOME": "relative/dir", "XDG_STATE_HOME": ""}
        self.assertEqual(
            paths.config_dir(env, "linux", HOME), HOME / ".config" / "one-setup-all-agents"
        )
        self.assertEqual(
            paths.state_dir(env, "linux", HOME), HOME / ".local" / "state" / "one-setup-all-agents"
        )

    def test_explicit_overrides_beat_everything(self):
        env = {
            paths.ENV_CONFIG_DIR: str(HOME / "mine-c"),
            paths.ENV_STATE_DIR: str(HOME / "mine-s"),
            "XDG_CONFIG_HOME": str(HOME / "xc"),
            "APPDATA": str(HOME / "ad"),
        }
        for platform in ("windows", "linux", "macos"):
            self.assertEqual(paths.config_dir(env, platform, HOME), HOME / "mine-c")
            self.assertEqual(paths.state_dir(env, platform, HOME), HOME / "mine-s")

    def test_build_env_collects_both_directories(self):
        env = paths.build_env({}, HOME)
        self.assertEqual(env.home, HOME)
        self.assertEqual(env.config_dir, paths.config_dir({}, env.platform, HOME))
        self.assertEqual(env.state_dir, paths.state_dir({}, env.platform, HOME))
        self.assertIn(env.platform, ("windows", "linux", "macos"))

    def test_detect_platform(self):
        for sys_platform, expected in (
            ("win32", "windows"),
            ("darwin", "macos"),
            ("linux", "linux"),
            ("freebsd14", "linux"),
        ):
            with mock.patch.object(paths.sys, "platform", sys_platform):
                self.assertEqual(paths.detect_platform(), expected)


class IsolationTests(WorkDirCase):
    """A fake home must never reach the real state or config directory, whatever the real environment says."""

    def test_a_fake_home_ignores_the_real_xdg_and_appdata_variables(self):
        fake = self.make_home() / "home"
        fake.mkdir()
        real_looking = self.make_home() / "somewhere-else"
        env = {
            "XDG_STATE_HOME": str(real_looking),
            "XDG_CONFIG_HOME": str(real_looking),
            "LOCALAPPDATA": str(real_looking),
            "APPDATA": str(real_looking),
        }
        with mock.patch.dict(os.environ, env):
            for d in (
                locations.state_root(fake),
                locations.config_root(fake),
                locations.backups_dir(fake),
            ):
                self.assertTrue(str(d).startswith(str(fake)), d)

    def test_the_explicit_state_override_is_still_honored(self):
        fake = self.make_home() / "home"
        fake.mkdir()
        target = self.make_home() / "redirected"
        with mock.patch.dict(os.environ, {paths.ENV_STATE_DIR: str(target)}):
            self.assertEqual(locations.state_root(fake), target)


if __name__ == "__main__":
    unittest.main()
