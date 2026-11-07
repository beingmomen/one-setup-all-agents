"""Tests of tool detection (docs/decisions/0014): finding binaries, reading versions, the report.

Every test runs in a temporary home. The platform is always passed explicitly, so the lookup rules
(PATH, PATHEXT, `.cmd` shims, known install folders) are tested the same way on every host. No tool
binary is ever started: the version command goes through an in-process fake. The few tests of the
real runner start the Python interpreter running this test, never a tool.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import all_adapters, get_adapter
from one_setup_all_agents.adapters import antigravity as agy_module
from one_setup_all_agents.adapters import claude_code as claude_module
from one_setup_all_agents.adapters import codex as codex_module
from one_setup_all_agents.adapters import opencode as opencode_module
from one_setup_all_agents.adapters._common import (
    find_binary,
    known_bin_dirs,
    parse_version,
    path_dirs,
)
from one_setup_all_agents.adapters.base import ToolAdapter
from one_setup_all_agents.detect import (
    DEFAULT_PROBE_TIMEOUT,
    MAX_OUTPUT_BYTES,
    ProbeResult,
    default_probe_runner,
    detect_all,
    detect_one,
    mask_home,
    render_detections,
)
from one_setup_all_agents.model import (
    SOURCE_KNOWN_LOCATION,
    SOURCE_OVERRIDE,
    SOURCE_PATH,
    STATUS_ABSENT,
    STATUS_CONFIG_ONLY,
    STATUS_INSTALLED,
    Detection,
    Env,
    Kind,
    Paths,
    VersionProbe,
)

POSIX_HOST = os.name != "nt"
needs_posix_host = unittest.skipUnless(
    POSIX_HOST, "a PATH of drive-letter folders cannot be written with the POSIX ':' separator"
)

#: The binary name of each registered adapter, and the launcher a Windows install leaves.
NAMES = {
    "opencode": (opencode_module.BINARY, ".cmd"),
    "claude": (claude_module.CLI_NAME, ".exe"),
    "codex": (codex_module.BINARY, ".exe"),
    "agy": (agy_module.BINARY, ".exe"),
}

#: Output shaped like the real `--version` of each tool.
VERSION_OUTPUT = {
    "opencode": "v2.2.5\n",
    "claude": "2.1.310 (Claude Code)\n",
    "codex": "codex-cli 0.146.0\n",
    "agy": "1.4.12\n",
}
EXPECTED_VERSION = {"opencode": "2.2.5", "claude": "2.1.310", "codex": "0.146.0", "agy": "1.4.12"}


def touch(path: Path, *, executable: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(0o755 if executable else 0o644)
    return path


class FakeProbe:
    """In-process stand-in for the version command. Records every call; never starts anything.

    `results` maps the binary's file stem to a `ProbeResult` or to an exception to raise.
    """

    def __init__(self, results=None):
        self.results = dict(results or {})
        self.calls = []

    def __call__(self, argv, environ, timeout):
        self.calls.append((tuple(argv), dict(environ), timeout))
        stem = Path(argv[0]).stem.lower()
        result = self.results.get(stem, ProbeResult(0, VERSION_OUTPUT.get(stem, "1.0.0\n")))
        if isinstance(result, BaseException):
            raise result
        return result


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osaa-detect-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"

    def make_env(self, platform, environ=None, home=None):
        return Env(
            home=home or self.home,
            platform=platform,
            environ=dict(environ or {}),
            config_dir=self.root / "config",
            state_dir=self.root / "state",
        )

    def windows_env(self, extra=None, path=None):
        environ = {"PATH": str(path if path is not None else self.bin)}
        environ.update(extra or {})
        return self.make_env("windows", environ)

    def install_all(self, directory=None, windows=True):
        """One launcher per tool in `directory`, named as an install on `windows` would."""
        directory = directory or self.bin
        for name, ext in NAMES.values():
            touch(directory / (name + (ext if windows else "")))


# -- finding a binary ------------------------------------------------------------------------------


class FindBinaryWindowsTests(Base):
    def test_a_cmd_shim_is_found_through_the_default_pathext(self):
        shim = touch(self.bin / "demo.cmd")
        found = find_binary(self.windows_env(), "demo")
        self.assertEqual(found.path, shim)
        self.assertEqual(found.source, SOURCE_PATH)

    def test_pathext_comes_from_the_given_environment(self):
        touch(self.bin / "demo.cmd")
        exe = touch(self.bin / "demo.exe")
        # the order of PATHEXT decides between two launchers in one folder
        self.assertEqual(
            find_binary(self.windows_env({"PATHEXT": ".CMD;.EXE"}), "demo").path.name, "demo.cmd"
        )
        self.assertEqual(find_binary(self.windows_env({"PATHEXT": ".EXE;.CMD"}), "demo").path, exe)
        # an extension that PATHEXT does not list is not tried
        self.assertIsNone(find_binary(self.windows_env({"PATHEXT": ".EXE"}), "only-cmd"))
        touch(self.bin / "only-cmd.cmd")
        self.assertIsNone(find_binary(self.windows_env({"PATHEXT": ".EXE"}), "only-cmd"))
        self.assertIsNotNone(find_binary(self.windows_env({"PATHEXT": ".EXE;.CMD"}), "only-cmd"))

    def test_the_process_environment_is_never_consulted(self):
        touch(self.bin / "demo.cmd")
        env = self.make_env("windows", {})
        with mock.patch.dict(os.environ, {"PATH": str(self.bin), "PATHEXT": ".CMD"}):
            self.assertIsNone(find_binary(env, "demo"))

    def test_variable_names_are_case_insensitive_on_windows(self):
        shim = touch(self.bin / "demo.cmd")
        env = self.make_env("windows", {"Path": str(self.bin), "pathext": ".CMD"})
        self.assertEqual(find_binary(env, "demo").path, shim)

    def test_the_extensionless_script_beside_a_shim_is_not_the_binary(self):
        touch(self.bin / "demo")  # what an npm install leaves next to demo.cmd: a sh script
        self.assertIsNone(find_binary(self.windows_env(), "demo"))
        shim = touch(self.bin / "demo.cmd")
        self.assertEqual(find_binary(self.windows_env(), "demo").path, shim)

    def test_the_first_path_folder_wins_and_quotes_are_stripped(self):
        first = self.root / "first"
        second = self.root / "second"
        touch(second / "demo.cmd")
        wanted = touch(first / "demo.cmd")
        env = self.make_env("windows", {"PATH": f'"{first}";{second}'})
        self.assertEqual(find_binary(env, "demo").path, wanted)

    def test_empty_and_relative_entries_are_ignored(self):
        env = self.make_env("windows", {"PATH": f";relative;.;{self.bin};"})
        self.assertEqual(path_dirs(env), [self.bin])

    def test_known_folders_are_searched_after_path_and_say_so(self):
        for folder, expected in (
            (self.home / ".local" / "bin", self.home),
            (self.root / "roaming" / "npm", None),
            (self.root / "local" / "Microsoft" / "WindowsApps", None),
        ):
            with self.subTest(folder=str(folder)):
                shim = touch(folder / "demo.cmd")
                environ = {"PATH": str(self.root / "empty")}
                if expected is None:
                    environ.update(
                        APPDATA=str(self.root / "roaming"), LOCALAPPDATA=str(self.root / "local")
                    )
                found = find_binary(self.make_env("windows", environ), "demo")
                self.assertEqual(found.path, shim)
                self.assertEqual(found.source, SOURCE_KNOWN_LOCATION)
                shim.unlink()

    def test_a_known_folder_that_is_also_on_path_counts_as_path(self):
        local_bin = self.home / ".local" / "bin"
        shim = touch(local_bin / "demo.cmd")
        found = find_binary(self.windows_env(path=local_bin), "demo")
        self.assertEqual((found.path, found.source), (shim, SOURCE_PATH))

    def test_appdata_defaults_to_the_home_when_the_variables_are_missing(self):
        dirs = known_bin_dirs(self.make_env("windows", {}))
        self.assertIn(self.home / "AppData" / "Roaming" / "npm", dirs)
        self.assertIn(self.home / "AppData" / "Local" / "Microsoft" / "WindowsApps", dirs)

    def test_nothing_is_found_on_an_empty_path(self):
        self.assertIsNone(find_binary(self.make_env("windows", {}), "demo"))


class FindBinaryPosixTests(Base):
    @needs_posix_host
    def test_an_executable_file_on_path_is_found_for_linux_and_macos(self):
        for platform in ("linux", "macos"):
            with self.subTest(platform=platform):
                tool = touch(self.bin / "demo")
                env = self.make_env(platform, {"PATH": str(self.bin)})
                found = find_binary(env, "demo")
                self.assertEqual((found.path, found.source), (tool, SOURCE_PATH))
                tool.unlink()

    @needs_posix_host
    def test_a_file_without_the_execute_bit_is_not_a_binary(self):
        touch(self.bin / "demo", executable=False)
        self.assertIsNone(find_binary(self.make_env("linux", {"PATH": str(self.bin)}), "demo"))

    @needs_posix_host
    def test_pathext_means_nothing_off_windows(self):
        touch(self.bin / "demo.cmd")
        env = self.make_env("linux", {"PATH": str(self.bin), "PATHEXT": ".CMD"})
        self.assertIsNone(find_binary(env, "demo"))

    @needs_posix_host
    def test_path_order_and_relative_entries(self):
        first, second = self.root / "first", self.root / "second"
        touch(second / "demo")
        wanted = touch(first / "demo")
        env = self.make_env("linux", {"PATH": f"relative::{first}:{second}"})
        self.assertEqual(path_dirs(env), [first, second])
        self.assertEqual(find_binary(env, "demo").path, wanted)

    @needs_posix_host
    def test_known_folders_under_the_home(self):
        for platform in ("linux", "macos"):
            with self.subTest(platform=platform):
                tool = touch(self.home / ".local" / "bin" / "demo")
                env = self.make_env(platform, {"PATH": str(self.root / "empty")})
                found = find_binary(env, "demo")
                self.assertEqual((found.path, found.source), (tool, SOURCE_KNOWN_LOCATION))
                tool.unlink()

    def test_the_known_folders_differ_by_os_and_follow_the_home(self):
        linux = known_bin_dirs(self.make_env("linux", {}))
        macos = known_bin_dirs(self.make_env("macos", {}))
        self.assertIn(self.home / ".local" / "bin", linux)
        self.assertNotIn(Path("/opt/homebrew/bin"), linux)
        self.assertIn(Path("/opt/homebrew/bin"), macos)
        other_home = self.root / "elsewhere"
        self.assertIn(
            other_home / ".local" / "bin",
            known_bin_dirs(self.make_env("linux", {}, home=other_home)),
        )

    def test_an_extra_folder_is_searched_last(self):
        extra = self.root / "extra"
        tool = touch(extra / "demo.cmd")
        found = find_binary(self.make_env("windows", {}), "demo", extra_dirs=[extra])
        self.assertEqual((found.path, found.source), (tool, SOURCE_KNOWN_LOCATION))


# -- detection with the real adapters ----------------------------------------------------------------


class AdapterDetectionTests(Base):
    def test_every_adapter_finds_its_windows_launcher_on_path(self):
        self.install_all()
        for adapter in all_adapters():
            with self.subTest(tool=adapter.id):
                found = adapter.detect(self.windows_env())
                name, ext = NAMES[adapter.id]
                self.assertEqual(found.binary, self.bin / (name + ext))
                self.assertEqual(found.binary_source, SOURCE_PATH)
                self.assertEqual(found.status, STATUS_INSTALLED)
                self.assertIsNone(found.version)  # detect() never starts a program

    @needs_posix_host
    def test_every_adapter_finds_its_binary_on_linux_and_macos(self):
        self.install_all(windows=False)
        for platform in ("linux", "macos"):
            for adapter in all_adapters():
                with self.subTest(platform=platform, tool=adapter.id):
                    env = self.make_env(platform, {"PATH": str(self.bin)})
                    found = adapter.detect(env)
                    self.assertEqual(found.binary, self.bin / NAMES[adapter.id][0])
                    self.assertEqual(found.binary_source, SOURCE_PATH)

    def test_a_tool_with_config_but_no_binary_is_config_only(self):
        for adapter in all_adapters():
            with self.subTest(tool=adapter.id):
                env = self.make_env("windows", {})
                root = adapter.detect(env).config_roots[0]
                root.mkdir(parents=True, exist_ok=True)
                found = adapter.detect(env)
                self.assertTrue(found.installed)  # there is something to manage
                self.assertIsNone(found.binary)
                self.assertIsNone(found.binary_source)
                self.assertEqual(found.status, STATUS_CONFIG_ONLY)
                shutil.rmtree(root)

    def test_an_absent_tool_is_absent(self):
        for adapter in all_adapters():
            with self.subTest(tool=adapter.id):
                found = adapter.detect(self.make_env("windows", {"PATH": str(self.bin)}))
                self.assertFalse(found.installed)
                self.assertEqual(found.status, STATUS_ABSENT)

    def test_the_default_windows_install_of_agy_is_known(self):
        local = self.root / "local"
        exe = touch(local / "agy" / "bin" / "agy.exe")
        found = get_adapter("agy").detect(self.make_env("windows", {"LOCALAPPDATA": str(local)}))
        self.assertEqual((found.binary, found.binary_source), (exe, SOURCE_KNOWN_LOCATION))
        # without the variable the same place is derived from the home
        exe2 = touch(self.home / "AppData" / "Local" / "agy" / "bin" / "agy.exe")
        found = get_adapter("agy").detect(self.make_env("windows", {}))
        self.assertEqual(found.binary, exe2)
        # and it is not looked for on other systems
        self.assertEqual(agy_module.install_dirs(self.make_env("linux", {})), [])

    def test_opencode_is_found_in_the_local_bin_of_the_home(self):
        shim = touch(self.home / ".local" / "bin" / "opencode.cmd")
        found = get_adapter("opencode").detect(self.make_env("windows", {}))
        self.assertEqual((found.binary, found.binary_source), (shim, SOURCE_KNOWN_LOCATION))

    def test_claude_override_wins_over_path_and_a_typo_is_not_replaced(self):
        on_path = touch(self.bin / "claude.cmd")
        chosen = touch(self.root / "chosen" / "claude.exe")
        adapter = get_adapter("claude")
        found = adapter.detect(self.windows_env({claude_module.BIN_ENV: str(chosen)}))
        self.assertEqual((found.binary, found.binary_source), (chosen, SOURCE_OVERRIDE))
        typo = adapter.detect(self.windows_env({claude_module.BIN_ENV: str(self.root / "nope")}))
        self.assertIsNone(typo.binary)
        self.assertTrue(any(claude_module.BIN_ENV in w for w in typo.warnings))
        self.assertTrue(on_path.is_file())  # the install on PATH was not substituted

    def test_claude_known_folder_is_called_by_full_path_but_path_by_bare_name(self):
        env_known = self.make_env("windows", {})
        local = touch(self.home / ".claude" / "local" / "claude.exe")
        argv0, binary, _ = claude_module.find_cli(env_known)
        self.assertEqual((argv0, binary), (str(local), local))
        shim = touch(self.bin / "claude.cmd")
        argv0, binary, _ = claude_module.find_cli(self.windows_env())
        self.assertEqual((argv0, binary), ("claude", shim))


# -- reading versions ----------------------------------------------------------------------------------


class DetectAllTests(Base):
    def test_every_installed_tool_gets_its_version_from_one_command(self):
        self.install_all()
        env = self.windows_env({"SOME_VARIABLE": "kept"})
        probe = FakeProbe()
        found = detect_all(env, probe=probe)
        self.assertEqual([d.tool for d in found], [a.id for a in all_adapters()])
        self.assertEqual({d.tool: d.version for d in found}, EXPECTED_VERSION)
        self.assertEqual(len(probe.calls), 4)
        for (argv, environ, timeout), detection in zip(probe.calls, found, strict=True):
            self.assertEqual(argv, (str(detection.binary), "--version"))
            self.assertEqual(environ, env.environ)  # the environment of Env, nothing else
            self.assertEqual(timeout, DEFAULT_PROBE_TIMEOUT)
        self.assertTrue(all(d.label for d in found))
        self.assertFalse([w for d in found for w in d.warnings])

    def test_the_timeout_is_passed_on(self):
        self.install_all()
        probe = FakeProbe()
        detect_all(self.windows_env(), probe=probe, timeout=1.5)
        self.assertEqual({t for _, _, t in probe.calls}, {1.5})

    def test_a_tool_without_a_binary_is_never_probed(self):
        for adapter in all_adapters():
            adapter.detect(self.make_env("windows", {})).config_roots[0].mkdir(parents=True)
        probe = FakeProbe()
        found = detect_all(self.make_env("windows", {}), probe=probe)
        self.assertEqual(probe.calls, [])
        self.assertEqual({d.status for d in found}, {STATUS_CONFIG_ONLY})
        self.assertEqual({d.version for d in found}, {None})
        self.assertTrue(all(d.config_found == d.config_roots for d in found))

    def test_versions_can_be_skipped(self):
        self.install_all()
        probe = FakeProbe()
        found = detect_all(self.windows_env(), probe=probe, versions=False)
        self.assertEqual(probe.calls, [])
        self.assertEqual({d.version for d in found}, {None})
        self.assertTrue(all(d.binary for d in found))

    def test_a_given_list_of_adapters_is_used(self):
        self.install_all()
        probe = FakeProbe()
        found = detect_all(self.windows_env(), [get_adapter("codex")], probe=probe)
        self.assertEqual([d.tool for d in found], ["codex"])
        self.assertEqual(len(probe.calls), 1)

    def test_the_version_may_be_printed_on_stderr(self):
        self.install_all()
        probe = FakeProbe({"codex": ProbeResult(0, "", "codex-cli 0.146.0\n")})
        self.assertEqual(
            detect_one(get_adapter("codex"), self.windows_env(), probe=probe).version, "0.146.0"
        )

    def test_a_non_zero_exit_gives_no_version_and_a_warning(self):
        self.install_all()
        probe = FakeProbe({"claude": ProbeResult(2, "2.1.310 (Claude Code)", "usage")})
        found = {d.tool: d for d in detect_all(self.windows_env(), probe=probe)}
        self.assertIsNone(found["claude"].version)
        self.assertTrue(any("status 2" in w for w in found["claude"].warnings))
        self.assertEqual(found["claude"].status, STATUS_INSTALLED)  # still installed
        self.assertEqual(found["codex"].version, "0.146.0")  # the others are untouched

    def test_a_timeout_gives_no_version_and_a_warning(self):
        self.install_all()
        probe = FakeProbe({"opencode": TimeoutError("slow")})
        found = {d.tool: d for d in detect_all(self.windows_env(), probe=probe, timeout=3)}
        self.assertIsNone(found["opencode"].version)
        self.assertTrue(any("3 seconds" in w for w in found["opencode"].warnings))
        self.assertEqual(found["agy"].version, "1.4.12")

    def test_a_binary_that_vanished_at_probe_time(self):
        self.install_all()
        probe = FakeProbe({"agy": FileNotFoundError(2, "No such file")})
        found = {d.tool: d for d in detect_all(self.windows_env(), probe=probe)}
        self.assertIsNone(found["agy"].version)
        self.assertTrue(any("could not be started" in w for w in found["agy"].warnings))
        self.assertIsNotNone(found["agy"].binary)

    def test_other_start_failures_and_runner_crashes_become_warnings(self):
        self.install_all()
        for error, text in (
            (PermissionError(13, "Permission denied"), "Permission denied"),
            (RuntimeError("boom"), "RuntimeError"),
            (ValueError("bad"), "ValueError"),
        ):
            with self.subTest(error=type(error).__name__):
                probe = FakeProbe({"codex": error})
                found = detect_one(get_adapter("codex"), self.windows_env(), probe=probe)
                self.assertIsNone(found.version)
                self.assertTrue(any(text in w for w in found.warnings), found.warnings)
                self.assertNotIn("boom", " ".join(found.warnings))  # a message is not echoed

    def test_unreadable_output_gives_a_warning(self):
        self.install_all()
        for out in ("", "no digits here\n", "usage: tool [options]\n"):
            with self.subTest(out=out):
                probe = FakeProbe({"codex": ProbeResult(0, out, "")})
                found = detect_one(get_adapter("codex"), self.windows_env(), probe=probe)
                self.assertIsNone(found.version)
                self.assertTrue(any("no version number" in w for w in found.warnings))

    def test_an_adapter_without_a_probe_is_not_run_and_not_blamed(self):
        class NoProbe(ToolAdapter):
            id = "no-probe"
            label = "No Probe"
            capabilities = frozenset({Kind.INSTRUCTIONS})

            def detect(self, env):
                return Detection(installed=True, binary=self.binary_path)

            def version_probe(self, env, binary):
                return None

            def paths(self, env, scope):
                return Paths()

            read_instructions = read_mcp = read_skills = paths
            plan_instructions = plan_mcp = plan_skills = paths

        adapter = NoProbe()
        adapter.binary_path = touch(self.bin / "x.cmd")
        probe = FakeProbe()
        found = detect_one(adapter, self.windows_env(), probe=probe)
        self.assertEqual((found.version, found.warnings, probe.calls), (None, (), []))

    def test_an_adapter_that_raises_does_not_stop_the_run(self):
        self.install_all()

        class Broken(ToolAdapter):
            id = "broken"
            label = "Broken"
            capabilities = frozenset()

            def detect(self, env):
                raise OSError("disk exploded")

            def paths(self, env, scope):
                return Paths()

            read_instructions = read_mcp = read_skills = paths
            plan_instructions = plan_mcp = plan_skills = paths

        adapters = [Broken(), get_adapter("codex")]
        found = detect_all(self.windows_env(), adapters, probe=FakeProbe())
        self.assertEqual([d.tool for d in found], ["broken", "codex"])
        self.assertFalse(found[0].installed)
        self.assertEqual(found[0].status, STATUS_ABSENT)
        self.assertTrue(any("OSError" in w for w in found[0].warnings))
        self.assertEqual(found[1].version, "0.146.0")

    def test_the_adapters_warnings_are_kept(self):
        env = self.windows_env({claude_module.BIN_ENV: str(self.root / "nope")})
        found = detect_one(get_adapter("claude"), env, probe=FakeProbe())
        self.assertTrue(any(claude_module.BIN_ENV in w for w in found.warnings))

    def test_detect_all_does_not_start_anything_by_default_when_nothing_is_installed(self):
        with mock.patch("subprocess.Popen") as popen:
            detect_all(self.make_env("windows", {"PATH": str(self.bin)}))
        popen.assert_not_called()

    def test_the_process_environment_is_not_used_to_find_tools(self):
        self.install_all()
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            found = detect_all(self.make_env("windows", {}), probe=FakeProbe())
        self.assertEqual({d.binary for d in found}, {None})


# -- the version declarations of the adapters ---------------------------------------------------------------


class VersionParsingTests(unittest.TestCase):
    def test_generic_formats(self):
        cases = {
            "v2.2.6": "2.2.6",
            "opencode v2.2.6": "2.2.6",
            "codex-cli 0.146.0": "0.146.0",
            "2.1.3 (Some Tool)": "2.1.3",
            "1.4.12\n": "1.4.12",
            "tool 1.2.3-beta.1+build.5": "1.2.3-beta.1+build.5",
            "\x1b[1m3.0.1\x1b[0m": "3.0.1",
            "Brave Browser Nightly 157.1.101.8 nightly": "157.1.101.8",
            "release 2026-11-07 version 4.5": "4.5",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_version(text), expected)

    def test_no_version(self):
        for text in ("", "   \n", "usage: tool [options]", "build 7", "2026-11-07"):
            with self.subTest(text=text):
                self.assertIsNone(parse_version(text))

    def test_a_preferred_shape_beats_an_earlier_number(self):
        self.assertEqual(parse_version("node 20.1.0\ndemo 1.2.3", prefer=r"demo\s+(\S+)"), "1.2.3")
        self.assertEqual(parse_version("node 20.1.0", prefer=r"demo\s+(\S+)"), "20.1.0")


class AdapterVersionProbeTests(Base):
    """Each adapter declares its command and how to read the answer. Nothing is run here."""

    CASES = {
        "opencode": {
            "v2.2.5": "2.2.5",
            "v2.2.5\n": "2.2.5",
            "opencode v2.2.6": "2.2.6",
            "opencode 2.3.0-beta.1": "2.3.0-beta.1",
            "update available 9.9.9\nopencode v2.2.6\n": "2.2.6",
        },
        "claude": {
            "2.1.310 (Claude Code)\n": "2.1.310",
            "Claude Code 2.1.3": "2.1.3",
            "Warning: node 20.1.0 is old\n2.1.310 (Claude Code)": "2.1.310",
        },
        "codex": {
            "codex-cli 0.146.0\n": "0.146.0",
            "codex-cli 0.147.0-alpha.2": "0.147.0-alpha.2",
            "WARNING: proxy 1.2.3 ignored\ncodex-cli 0.146.0": "0.146.0",
        },
        "agy": {
            "1.4.12": "1.4.12",
            "agy 1.4.12\n": "1.4.12",
            "agy v1.4.13": "1.4.13",
            "note 3.3.3\nagy 1.4.12": "1.4.12",
        },
    }

    def test_the_command_is_the_found_binary_and_version_flag(self):
        binary = self.bin / "tool.exe"
        for adapter in all_adapters():
            with self.subTest(tool=adapter.id):
                probe = adapter.version_probe(self.windows_env(), binary)
                self.assertIsInstance(probe, VersionProbe)
                self.assertEqual(probe.argv, (str(binary), "--version"))

    def test_each_adapter_reads_its_own_format(self):
        self.assertEqual(set(self.CASES), {a.id for a in all_adapters()})
        for tool, cases in self.CASES.items():
            probe = get_adapter(tool).version_probe(self.windows_env(), self.bin / "tool.exe")
            for text, expected in cases.items():
                with self.subTest(tool=tool, text=text):
                    self.assertEqual(probe.parse(text), expected)

    def test_each_adapter_returns_none_for_text_without_a_version(self):
        for adapter in all_adapters():
            probe = adapter.version_probe(self.windows_env(), self.bin / "tool.exe")
            for text in ("", "error: unknown option", "command not found"):
                with self.subTest(tool=adapter.id, text=text):
                    self.assertIsNone(probe.parse(text))

    def test_the_default_probe_of_the_base_class(self):
        class Plain(ToolAdapter):
            id = "plain"
            label = "Plain"
            capabilities = frozenset()

        probe = ToolAdapter.version_probe(Plain, self.windows_env(), Path("x"))
        self.assertEqual(probe.argv, ("x", "--version"))
        self.assertEqual(probe.parse("plain v1.2.3"), "1.2.3")

    def test_declaring_a_probe_starts_nothing(self):
        with mock.patch("subprocess.Popen") as popen, mock.patch("subprocess.run") as run:
            for adapter in all_adapters():
                adapter.version_probe(self.windows_env(), self.bin / "tool.exe")
        popen.assert_not_called()
        run.assert_not_called()


# -- the real runner (starts only the interpreter that runs this test) -------------------------------------


def _child_env(**extra):
    environ = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "PATH")}
    environ.update(extra)
    return environ


class DefaultRunnerTests(unittest.TestCase):
    def run_code(self, code, *, timeout=20.0, **extra):
        return default_probe_runner([sys.executable, "-c", code], _child_env(**extra), timeout)

    def test_output_and_exit_status_are_captured(self):
        result = self.run_code(
            "import sys; print('demo 1.2.3'); print('warn', file=sys.stderr); sys.exit(3)"
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout.strip(), "demo 1.2.3")
        self.assertEqual(result.stderr.strip(), "warn")

    def test_stdin_is_closed(self):
        result = self.run_code("import sys; print(repr(sys.stdin.read()))")
        self.assertEqual(result.stdout.strip(), "''")

    def test_only_the_given_environment_reaches_the_child(self):
        code = "import os; print(sorted(k for k in os.environ if k.startswith('OSAA_')))"
        with mock.patch.dict(os.environ, {"OSAA_LEAK": "1"}):
            result = self.run_code(code, OSAA_GIVEN="1")
        self.assertEqual(result.stdout.strip(), "['OSAA_GIVEN']")

    def test_a_command_that_hangs_is_killed_at_the_timeout(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.run_code("import time; time.sleep(60)", timeout=0.5)
        self.assertLess(time.monotonic() - started, 15)

    def test_output_is_bounded(self):
        result = self.run_code("print('x' * 100000)")
        self.assertLessEqual(len(result.stdout), MAX_OUTPUT_BYTES)

    def test_a_missing_binary_raises_file_not_found(self):
        missing = Path(tempfile.gettempdir()) / "osaa-no-such-binary-xyz"
        with self.assertRaises(OSError):
            default_probe_runner([str(missing), "--version"], {}, 5)

    def test_no_shell_is_used(self):
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.returncode = 0
            popen.return_value.wait.return_value = 0
            default_probe_runner(["tool", "--version"], {"A": "b"}, 1)
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["env"], {"A": "b"})
        self.assertEqual(popen.call_args.args[0], ["tool", "--version"])


# -- the report ---------------------------------------------------------------------------------------------


class RenderTests(Base):
    def test_paths_under_the_home_are_shown_as_tilde(self):
        self.install_all(self.home / ".local" / "bin")
        env = self.windows_env(
            {"MY_TOKEN": "s3cr3t-token-value"}, path=self.home / ".local" / "bin"
        )
        (self.home / ".config" / "opencode").mkdir(parents=True)
        text = render_detections(detect_all(env, probe=FakeProbe()), env)
        self.assertNotIn(str(self.home), text)
        self.assertNotIn(self.home.as_posix(), text)
        self.assertIn("~/.local/bin/opencode.cmd (found on PATH)", text)
        self.assertIn("~/.config/opencode (found)", text)
        self.assertIn("~/.claude (not found)", text)
        self.assertIn("version: 2.1.310", text)
        self.assertNotIn("s3cr3t-token-value", text)
        self.assertNotIn("MY_TOKEN", text)

    def test_a_path_outside_the_home_is_kept(self):
        self.install_all()
        env = self.windows_env()
        text = render_detections(detect_all(env, probe=FakeProbe()), env)
        self.assertIn(str(self.bin / "codex.exe"), text)

    def test_the_three_states_and_the_source_are_spelled_out(self):
        env = self.windows_env()
        touch(self.bin / "opencode.cmd")
        (self.home / ".codex").mkdir()
        text = render_detections(detect_all(env, probe=FakeProbe()), env)
        self.assertIn("OpenCode (opencode): installed", text)
        self.assertIn("Codex (codex): config only (no binary found)", text)
        self.assertIn("Antigravity (agy): not installed", text)
        known = self.make_env("windows", {})
        touch(self.home / ".local" / "bin" / "opencode.cmd")
        text = render_detections(detect_all(known, probe=FakeProbe()), known)
        self.assertIn("(found in a known install folder)", text)

    def test_an_unreadable_version_is_reported_as_unknown_with_its_warning(self):
        self.install_all()
        env = self.windows_env()
        probe = FakeProbe({"opencode": TimeoutError()})
        text = render_detections(detect_all(env, [get_adapter("opencode")], probe=probe), env)
        self.assertIn("version: unknown", text)
        self.assertIn("warning: the version command did not answer", text)

    def test_warnings_are_masked_too(self):
        detection = Detection(
            installed=True,
            tool="demo",
            label="Demo",
            warnings=(f"cannot read {self.home}/x and {self.home.as_posix()}/y",),
        )
        text = render_detections([detection], self.windows_env())
        self.assertIn("warning: cannot read ~", text)
        self.assertNotIn(self.home.as_posix(), text)
        self.assertNotIn(str(self.home), text)

    def test_mask_home_is_case_insensitive_only_on_windows(self):
        windows = self.make_env("windows", {}, home=Path("/Users/Demo"))
        linux = self.make_env("linux", {}, home=Path("/Users/Demo"))
        self.assertEqual(mask_home("/users/demo/x", windows), "~/x")
        self.assertEqual(mask_home("/users/demo/x", linux), "/users/demo/x")
        self.assertEqual(mask_home("/Users/Demo/x", linux), "~/x")

    def test_a_degenerate_home_masks_nothing(self):
        env = self.make_env("linux", {}, home=Path("/"))
        self.assertEqual(mask_home("/usr/bin/x", env), "/usr/bin/x")

    def test_the_home_itself_is_a_tilde(self):
        detection = Detection(installed=True, tool="demo", label="Demo", config_roots=(self.home,))
        self.assertIn("config:  ~ (not found)", render_detections([detection], self.windows_env()))


class DetectionModelTests(unittest.TestCase):
    def test_status_and_defaults_keep_existing_callers_working(self):
        plain = Detection(installed=False)
        self.assertEqual(
            (plain.status, plain.binary_source, plain.tool, plain.label, plain.config_found),
            (STATUS_ABSENT, None, "", "", ()),
        )
        self.assertEqual(Detection(installed=True).status, STATUS_CONFIG_ONLY)
        self.assertEqual(Detection(installed=True, binary=Path("x")).status, STATUS_INSTALLED)


if __name__ == "__main__":
    unittest.main()
