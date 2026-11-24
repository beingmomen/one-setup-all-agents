"""scripts/check_personal_data.py: fails on forbidden patterns, never prints the pattern or the matched text.

The patterns used here are made up for the test. The real list lives in a git-ignored local file (or a CI secret).
"""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from support import WorkDirCase

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_personal_data.py"
PRE_COMMIT = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre-commit"
FORBIDDEN = "zzforbiddenwordzz"


def _find_sh() -> str | None:
    sh = shutil.which("sh")
    if sh:
        return sh
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "sh.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def run_guard(root, *args, patterns=None, patterns_file=None):
    env = {k: v for k, v in os.environ.items() if k != "PERSONAL_PATTERNS"}
    if patterns is not None:
        env["PERSONAL_PATTERNS"] = patterns
    cmd = [sys.executable, str(SCRIPT), "--root", str(root), *args]
    if patterns_file:
        cmd += ["--patterns-file", str(patterns_file)]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)


class GuardTests(WorkDirCase):
    def setUp(self):
        self.root = self.make_home() / "repo"
        self.root.mkdir()
        (self.root / "ok.txt").write_text("nothing to see here\n", encoding="utf-8")

    def test_clean_tree_passes(self):
        r = run_guard(self.root, patterns=FORBIDDEN)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("clean", r.stdout)

    def test_a_match_fails_and_reports_the_place_but_not_the_text_or_the_pattern(self):
        (self.root / "bad.txt").write_text(
            f"line one\nthe {FORBIDDEN.upper()} appears\n", encoding="utf-8"
        )
        r = run_guard(self.root, patterns=FORBIDDEN)
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad.txt:2: matches pattern #1", r.stdout)
        for stream in (r.stdout, r.stderr):
            self.assertNotIn(FORBIDDEN, stream.lower())

    def test_file_names_are_checked_too(self):
        (self.root / f"{FORBIDDEN}.md").write_text("x", encoding="utf-8")
        r = run_guard(self.root, patterns=FORBIDDEN)
        self.assertEqual(r.returncode, 1)
        self.assertIn("(file name)", r.stdout)

    def test_no_patterns_is_exit_2_not_a_silent_pass(self):
        r = run_guard(self.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("nothing was checked", r.stderr)

    def test_an_invalid_pattern_is_exit_2_and_is_not_echoed(self):
        r = run_guard(self.root, patterns="ok\n(unclosed-" + FORBIDDEN)
        self.assertEqual(r.returncode, 2)
        self.assertIn("pattern #2", r.stderr)
        self.assertNotIn(FORBIDDEN, r.stderr)

    def test_patterns_come_from_the_local_file_and_comments_are_ignored(self):
        (self.root / ".personal-patterns").write_text(
            f"# a comment\n\n{FORBIDDEN}\n", encoding="utf-8"
        )
        (self.root / "bad.txt").write_text(FORBIDDEN, encoding="utf-8")
        r = run_guard(self.root)
        self.assertEqual(r.returncode, 1)
        # the patterns file itself is never scanned (it holds the very words)
        self.assertNotIn(".personal-patterns", r.stdout)

    def test_a_negative_lookahead_allows_one_specific_use(self):
        (self.root / "a.txt").write_text("acme-corp-public is fine\n", encoding="utf-8")
        self.assertEqual(run_guard(self.root, patterns="acme-corp(?!-public)").returncode, 0)
        (self.root / "b.txt").write_text("acme-corp-private\n", encoding="utf-8")
        self.assertEqual(run_guard(self.root, patterns="acme-corp(?!-public)").returncode, 1)

    def test_binary_files_are_skipped(self):
        (self.root / "blob.bin").write_bytes(b"\0\1\2" + FORBIDDEN.encode() + b"\0")
        self.assertEqual(run_guard(self.root, patterns=FORBIDDEN).returncode, 0)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_mode_scans_tracked_and_untracked_but_not_ignored_files(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True, capture_output=True)
        (self.root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (self.root / "ignored.txt").write_text(FORBIDDEN, encoding="utf-8")
        self.assertEqual(run_guard(self.root, patterns=FORBIDDEN).returncode, 0)
        (self.root / "new.txt").write_text(FORBIDDEN, encoding="utf-8")
        self.assertEqual(run_guard(self.root, patterns=FORBIDDEN).returncode, 1)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_staged_mode_scans_what_is_staged(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True, capture_output=True)
        (self.root / "new.txt").write_text(FORBIDDEN, encoding="utf-8")
        self.assertEqual(
            run_guard(self.root, "--staged", patterns=FORBIDDEN).returncode, 0
        )  # not staged yet
        subprocess.run(
            ["git", "-C", str(self.root), "add", "new.txt"], check=True, capture_output=True
        )
        self.assertEqual(run_guard(self.root, "--staged", patterns=FORBIDDEN).returncode, 1)


class RepositoryTests(unittest.TestCase):
    def test_the_pattern_list_is_not_part_of_the_repository(self):
        root = SCRIPT.parents[1]
        self.assertIn(".personal-patterns", (root / ".gitignore").read_text(encoding="utf-8"))
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("PERSONAL_PATTERNS = [", text)  # no embedded list


class PreCommitHookTests(WorkDirCase):
    def _git_env(self) -> dict[str, str]:
        """The environment for every git and hook call: no user or system git config."""
        env = {k: v for k, v in os.environ.items() if k != "PERSONAL_PATTERNS"}
        home = str(self.make_home())
        env.update(
            HOME=home,
            USERPROFILE=home,
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
        )
        return env

    def _make_repo(self) -> Path:
        repo = self.make_home() / "hook-repo"
        repo.mkdir()
        (repo / "scripts").mkdir()
        shutil.copy2(SCRIPT, repo / "scripts" / "check_personal_data.py")
        env = self._git_env()
        subprocess.run(
            ["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True, env=env
        )
        (repo / "bad.txt").write_text(f"contains {FORBIDDEN}\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "bad.txt"], check=True, capture_output=True, env=env
        )
        return repo

    def _write_shim(self, directory: Path, name: str, body: str) -> None:
        path = directory / name
        path.write_bytes(body.encode("utf-8"))
        path.chmod(0o755)

    def _run_hook(self, sh: str, repo: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
        git_path = shutil.which("git")
        assert git_path is not None
        git_dir = str(Path(git_path).resolve().parent)
        env = self._git_env()
        env["PERSONAL_PATTERNS"] = FORBIDDEN
        env["PATH"] = os.pathsep.join([str(bin_dir), git_dir, env.get("PATH", "")])
        return subprocess.run(
            [sh, str(PRE_COMMIT)],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @unittest.skipUnless(shutil.which("git") and _find_sh(), "git and sh are required")
    def test_pre_commit_skips_failing_python3_stub_and_uses_working_python(self):
        sh = _find_sh()
        assert sh is not None
        repo = self._make_repo()
        bin_dir = self.make_home() / "bin-fallback-python"
        bin_dir.mkdir()
        py_exec = Path(sys.executable).as_posix()
        self._write_shim(bin_dir, "python3", '#!/bin/sh\necho "Python was not found" >&2\nexit 1\n')
        self._write_shim(bin_dir, "python", f'#!/bin/sh\nexec "{py_exec}" "$@"\n')
        r = self._run_hook(sh, repo, bin_dir)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("bad.txt:1: matches pattern #1", r.stdout)
        self.assertNotIn(FORBIDDEN, r.stdout + r.stderr)

    @unittest.skipUnless(shutil.which("git") and _find_sh(), "git and sh are required")
    def test_pre_commit_skips_a_python_2_and_uses_py_dash_3(self):
        sh = _find_sh()
        assert sh is not None
        repo = self._make_repo()
        bin_dir = self.make_home() / "bin-python2"
        bin_dir.mkdir()
        py_exec = Path(sys.executable).as_posix()
        self._write_shim(bin_dir, "python3", "#!/bin/sh\nexit 1\n")
        # a Python 2: runs `import sys`, fails only the version check
        self._write_shim(
            bin_dir, "python", '#!/bin/sh\ncase "$*" in *version_info*) exit 1;; esac\nexit 0\n'
        )
        self._write_shim(
            bin_dir,
            "py",
            f'#!/bin/sh\n[ "$1" = "-3" ] || exit 2\nshift\nexec "{py_exec}" "$@"\n',
        )
        r = self._run_hook(sh, repo, bin_dir)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("bad.txt:1: matches pattern #1", r.stdout)

    @unittest.skipUnless(shutil.which("git") and _find_sh(), "git and sh are required")
    def test_pre_commit_falls_back_to_py_dash_3(self):
        sh = _find_sh()
        assert sh is not None
        repo = self._make_repo()
        bin_dir = self.make_home() / "bin-fallback-py"
        bin_dir.mkdir()
        py_exec = Path(sys.executable).as_posix()
        self._write_shim(bin_dir, "python3", "#!/bin/sh\nexit 1\n")
        self._write_shim(bin_dir, "python", "#!/bin/sh\nexit 1\n")
        self._write_shim(
            bin_dir,
            "py",
            f'#!/bin/sh\n[ "$1" = "-3" ] || exit 2\nshift\nexec "{py_exec}" "$@"\n',
        )
        r = self._run_hook(sh, repo, bin_dir)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("bad.txt:1: matches pattern #1", r.stdout)

    @unittest.skipUnless(shutil.which("git") and _find_sh(), "git and sh are required")
    def test_pre_commit_skips_with_message_when_no_python_runs(self):
        sh = _find_sh()
        assert sh is not None
        repo = self._make_repo()
        bin_dir = self.make_home() / "bin-none"
        bin_dir.mkdir()
        for name in ("python3", "python", "py"):
            self._write_shim(bin_dir, name, "#!/bin/sh\nexit 1\n")
        r = self._run_hook(sh, repo, bin_dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("python not found; guard skipped", r.stderr)


if __name__ == "__main__":
    unittest.main()
