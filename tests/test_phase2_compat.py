"""Phase 2: the fixes that make the ported core behave on Windows, Linux and macOS.

Each test pins one fix from the separation map (section 3): `.cmd` only on Windows, no hardcoded `python`,
`claude` detection that skips instead of failing, path comparison that is not Windows-only.
"""

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import apply as applymod
from one_setup_all_agents._core import projfiles, source
from one_setup_all_agents._core.source import Source, Spec
from support import FAKE_CLAUDE, FIXTURE_SOURCE, WorkDirCase


class Obj:
    """A stand-in for a spec with only the attributes `_command_for` reads."""

    def __init__(self, command, project=False):
        self.command = command
        self.project = project


class CmdShimTests(unittest.TestCase):
    """`.cmd` is appended to npx/npm/pnpm/yarn only on Windows (it does not exist elsewhere)."""

    def test_windows_gets_the_cmd_shim_for_project_specs_outside_claude(self):
        with mock.patch.object(source, "is_windows", return_value=True):
            for cmd in ("npx", "npm", "pnpm", "yarn"):
                self.assertEqual(source._command_for("codex", Obj(cmd, project=True)), cmd + ".cmd")
                self.assertEqual(source._command_for("agy", Obj(cmd, project=True)), cmd + ".cmd")
                self.assertEqual(
                    source._command_for("opencode", Obj(cmd, project=True)), cmd + ".cmd"
                )

    def test_linux_and_macos_never_get_cmd(self):
        with mock.patch.object(source, "is_windows", return_value=False):
            for tool in ("claude", "codex", "opencode", "agy"):
                self.assertEqual(source._command_for(tool, Obj("npx", project=True)), "npx")

    def test_claude_and_other_commands_are_never_rewritten(self):
        with mock.patch.object(source, "is_windows", return_value=True):
            self.assertEqual(source._command_for("claude", Obj("npx", project=True)), "npx")
            self.assertEqual(source._command_for("codex", Obj("node", project=True)), "node")
            self.assertEqual(source._command_for("codex", Obj("npx", project=False)), "npx")

    def test_the_rendered_entries_follow_the_same_rule(self):
        raw = {"transport": "stdio", "command": "npx", "args": ["-y", "demo-pkg"]}
        spec = Spec("demo", raw, {})
        spec.project = True
        with mock.patch.object(source, "is_windows", return_value=False):
            self.assertEqual(source.render_native("codex", spec)["command"], "npx")
            self.assertEqual(
                source.render_native("opencode", spec)["command"], ["npx", "-y", "demo-pkg"]
            )
        with mock.patch.object(source, "is_windows", return_value=True):
            self.assertEqual(source.render_native("codex", spec)["command"], "npx.cmd")


HEADER_SERVER = {
    "version": 1,
    "servers": {
        "demo-remote": {
            "transport": "http",
            "url": "https://demo.example.test/mcp",
            "headers": {"Authorization": "Bearer {{DEMO_TOKEN}}"},
        }
    },
}


def header_spec():
    return Spec("demo-remote", HEADER_SERVER["servers"]["demo-remote"], {})


class NoHardcodedPythonTests(unittest.TestCase):
    """The agy wrapper entry starts the console script, or the current interpreter; never the bare word `python`."""

    def entry(self):
        return source.render_native("agy", header_spec())

    def test_console_script_on_path_is_used_by_name(self):
        with mock.patch.object(source.shutil, "which", return_value="/somewhere/bin/all-agents"):
            e = self.entry()
        self.assertEqual(e["command"], "all-agents")
        self.assertEqual(e["args"][0], "_run")
        self.assertIn("--derive", e["args"])
        self.assertIn("npx", e["args"])

    def test_without_the_console_script_the_current_interpreter_runs_the_module(self):
        with mock.patch.object(source.shutil, "which", return_value=None):
            e = self.entry()
        self.assertEqual(e["command"], source.sys.executable)
        self.assertEqual(e["args"][:4], ["-I", "-m", "one_setup_all_agents", "_run"])

    def test_the_word_python_is_never_written(self):
        for found in ("/x/all-agents", None):
            with mock.patch.object(source.shutil, "which", return_value=found):
                e = self.entry()
            self.assertNotEqual(e["command"], "python")
            self.assertNotEqual(e["command"], "python3")

    def test_no_secret_value_or_reference_value_is_in_the_entry(self):
        with mock.patch.object(source.shutil, "which", return_value="/x/all-agents"):
            text = json.dumps(self.entry())
        self.assertIn("DEMO_TOKEN", text)  # the NAME of the variable is there
        self.assertNotIn("FAKE-", text)

    def test_the_runner_marker_lets_placeholders_through_only_for_runner_entries(self):
        with mock.patch.object(source.shutil, "which", return_value="/x/all-agents"):
            e = self.entry()
        self.assertIsNone(source.placeholder_leak(e))
        stray = {"command": "node", "args": ["server.js", "--header", "X:{{DEMO_TOKEN}}"]}
        self.assertEqual(source.placeholder_leak(stray), "{{DEMO_TOKEN}}")

    def test_the_old_launcher_field_is_not_accepted_any_more(self):
        raw = {"transport": "stdio", "command": "node", "launcher": True}
        from one_setup_all_agents._core.source import SourceError

        with self.assertRaises(SourceError):
            Spec("demo", raw, {})


class ClaudeDetectionTests(WorkDirCase):
    def setUp(self):
        self.home = self.copy_fixture_home()
        self.src = Source(FIXTURE_SOURCE)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(applymod.CLAUDE_BIN_ENV, None)

    def test_find_claude_order_and_typos(self):
        fake = str(FAKE_CLAUDE)
        self.assertEqual(applymod.find_claude(fake), fake)
        self.assertIsNone(
            applymod.find_claude(str(self.home / "no-such-claude"))
        )  # a typo is not replaced by PATH
        with mock.patch.dict(os.environ, {applymod.CLAUDE_BIN_ENV: fake}):
            self.assertEqual(applymod.find_claude(), fake)
        with mock.patch.dict(os.environ, {applymod.CLAUDE_BIN_ENV: str(self.home / "nope")}):
            self.assertIsNone(applymod.find_claude())
        with mock.patch.object(applymod.shutil, "which", return_value="/usr/bin/claude"):
            self.assertEqual(applymod.find_claude(), "/usr/bin/claude")
        with mock.patch.object(applymod.shutil, "which", return_value=None):
            self.assertIsNone(applymod.find_claude())

    def run_apply(self, **kw):
        lines = []
        rc = applymod.run_apply(
            self.home, self.src, kw.pop("tools", None), None, True, out=lines.append, **kw
        )
        return rc, "\n".join(lines)

    def test_a_missing_claude_skips_that_tool_and_applies_the_others(self):
        with mock.patch.object(applymod.shutil, "which", return_value=None):
            rc, out = self.run_apply()
        self.assertEqual(rc, 0, out)
        self.assertIn("[claude] skipped", out)
        self.assertIn("[opencode] applied and verified", out)
        claude_json = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertNotIn("new-stdio", claude_json["mcpServers"])  # untouched
        self.assertIn(
            "new-stdio",
            json.loads(
                (self.home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
            )["mcpServers"],
        )

    def test_a_missing_claude_skipped_tool_is_not_recorded_in_the_ledger(self):
        with mock.patch.object(applymod.shutil, "which", return_value=None):
            self.run_apply()
        self.assertNotIn("claude", applymod.read_ledger(self.home))

    def test_asking_for_claude_by_name_fails_loudly_when_it_is_missing(self):
        with mock.patch.object(applymod.shutil, "which", return_value=None):
            with self.assertRaises(applymod.ApplyError):
                self.run_apply(tools=["claude"])

    def test_an_explicit_path_that_does_not_exist_fails_loudly(self):
        with mock.patch.object(applymod.shutil, "which", return_value="/usr/bin/claude"):
            with self.assertRaises(applymod.ApplyError):
                self.run_apply(claude_bin=str(self.home / "typo"))

    def test_a_working_stand_in_is_used_through_the_cli(self):
        rc, out = self.run_apply(claude_bin=str(FAKE_CLAUDE))
        self.assertEqual(rc, 0, out)
        self.assertIn("[claude] applied and verified", out)
        claude_json = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertIn("new-stdio", claude_json["mcpServers"])


class JunctionDetectionTests(WorkDirCase):
    """`os.path.isjunction` is Python 3.12+; the helper must also work on 3.11 (the minimum, ADR 0004)."""

    def test_a_junction_is_detected_without_os_path_isjunction(self):
        from one_setup_all_agents._core import fsutil, skills

        base = self.make_home()
        target = base / "target"
        target.mkdir()
        link = base / "link"
        skills.make_link(link, target)  # a junction on Windows, a symlink elsewhere
        with mock.patch.object(fsutil.os.path, "isjunction", None, create=True):
            self.assertTrue(skills.is_link(link))
            self.assertEqual(
                fsutil.is_junction(link), os.name == "nt"
            )  # a plain symlink is not a junction
            self.assertFalse(skills.is_link(target))
            self.assertFalse(fsutil.is_junction(base / "missing"))


class PathNormalizationTests(unittest.TestCase):
    """The comparison key of a path: slashes unified, trailing slash gone, case folded only where paths are case-insensitive."""

    def test_backslashes_and_trailing_slashes_unify(self):
        a = projfiles.norm(Path("some") / "dir")
        self.assertEqual(projfiles.norm("some\\dir\\"), a)
        self.assertEqual(projfiles.norm("some/dir/"), a)
        self.assertNotIn("\\", a)
        self.assertFalse(a.endswith("/"))

    def test_case_is_folded_only_on_windows(self):
        folded = projfiles.norm("Some/Dir") == projfiles.norm("some/dir")
        self.assertEqual(folded, os.name == "nt")

    @unittest.skipIf(os.name == "nt", "a path without a drive letter is a POSIX path")
    def test_posix_absolute_paths_have_no_drive_letter_requirement(self):
        self.assertEqual(projfiles.norm("/home/demo/work/"), "/home/demo/work")
        self.assertEqual(projfiles.norm("/"), "/")


class ProjectOwnerTests(unittest.TestCase):
    """Personal or team comes from the user's configuration; the built-in default knows no name."""

    def test_the_default_config_makes_everything_team(self):
        for remote in (
            "github.com:someone/app.git",
            "https://github.com/someone/app",
            None,
            "ssh://host/x.git",
        ):
            kind, why = projfiles.owner_of(remote)
            self.assertEqual(kind, "team", (remote, why))

    def test_a_configured_owner_is_personal_including_ssh_aliases(self):
        cfg = projfiles.OwnerConfig(personal_owners=["Demo-User"])
        for remote in (
            "github.com:demo-user/app.git",
            "https://github.com/DEMO-USER/app",
            "github-work:demo-user/app.git",
        ):
            self.assertEqual(projfiles.owner_of(remote, config=cfg)[0], "personal", remote)
        self.assertEqual(projfiles.owner_of("github.com:other/app.git", config=cfg)[0], "team")

    def test_a_remote_outside_github_is_team(self):
        cfg = projfiles.OwnerConfig(personal_owners=["demo-user"])
        kind, why = projfiles.owner_of("https://git.example.test/demo-user/app.git", config=cfg)
        self.assertEqual(kind, "team")
        self.assertIn("not on github.com", why)

    def test_no_remote_is_personal_only_inside_a_configured_path(self):
        base = Path("work-root") / "mine"
        cfg = projfiles.OwnerConfig(personal_paths=[str(base)])
        self.assertEqual(projfiles.owner_of(None, folder=base / "app", config=cfg)[0], "personal")
        self.assertEqual(projfiles.owner_of(None, folder=base, config=cfg)[0], "personal")
        self.assertEqual(
            projfiles.owner_of(None, folder=Path("work-root") / "mine-other", config=cfg)[0], "team"
        )
        self.assertEqual(projfiles.owner_of(None, folder=None, config=cfg)[0], "team")
        self.assertEqual(
            projfiles.owner_of(None, folder=base / "app")[0], "team"
        )  # no config: team

    def test_the_config_file_is_read_from_the_config_directory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.assertEqual(
                projfiles.load_owner_config(d).personal_owners, ()
            )  # no file: the default
            (d / "config.toml").write_text(
                'version = 1\n[projects]\npersonal_owners = ["Demo-User"]\npersonal_paths = ["work-root/mine"]\n',
                encoding="utf-8",
            )
            cfg = projfiles.load_owner_config(d)
            self.assertEqual(cfg.personal_owners, ("demo-user",))
            self.assertEqual(len(cfg.personal_paths), 1)
            (d / "config.toml").write_text(
                '[projects]\npersonal_owners = "not-a-list"\n', encoding="utf-8"
            )
            with self.assertRaises(projfiles.ProjectError):
                projfiles.load_owner_config(d)
            (d / "config.toml").write_text("not = [valid", encoding="utf-8")
            with self.assertRaises(projfiles.ProjectError):
                projfiles.load_owner_config(d)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitFactsTests(WorkDirCase):
    def test_tracked_files_and_remote(self):
        repo = self.make_home() / "repo"
        repo.mkdir()
        self.assertEqual(projfiles.git(repo, "init", "-q")[0], 0)
        (repo / ".mcp.json").write_text("{}", encoding="utf-8")
        (repo / "notes.txt").write_text("x", encoding="utf-8")
        projfiles.git(repo, "add", ".mcp.json")
        self.assertEqual(projfiles.tracked_files(repo, [".mcp.json", "notes.txt"]), {".mcp.json"})
        self.assertIsNone(projfiles.remote_url(repo))
        projfiles.git(repo, "remote", "add", "origin", "github.com:demo-user/app.git")
        self.assertEqual(projfiles.remote_url(repo), "github.com:demo-user/app.git")
        self.assertEqual(
            Path(projfiles.git_root(repo / "sub" if (repo / "sub").exists() else repo)),
            repo.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
