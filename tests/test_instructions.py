"""Instructions from one source: core + per-tool appendix, lint, dry-run, apply with backup, verify, rollback."""

import json
import subprocess
import unittest

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import instructions as ins
from one_setup_all_agents._core import locations
from support import WorkDirCase, cli_env, legacy_command

CORE = "# Shared rules\n\n- be kind\n- write tests\n"


class InstructionsCase(WorkDirCase):
    def setUp(self):
        base = self.make_home()
        self.home = base / "home"
        self.cfg = base / "config"
        self.home.mkdir()
        self.cfg.mkdir()
        (self.cfg / "instructions.md").write_text(CORE, encoding="utf-8")
        (self.cfg / "instructions.codex.md").write_text(
            "# Codex notes\n\n- use the sandbox\n", encoding="utf-8"
        )
        self.out = []

    def run_apply(self, do_apply, tools=None):
        return ins.run_apply(self.home, self.cfg, do_apply, tools, out=self.out.append)

    def target(self, tool):
        return ins.target_path(tool, self.home)


class BuildAndLintTests(unittest.TestCase):
    def test_build_is_core_plus_appendix_with_a_generated_header(self):
        text = ins.build("codex", CORE, "# Codex notes\n")
        first, rest = text.split("\n", 1)
        self.assertIn("GENERATED FILE", first)
        self.assertIn("instructions.codex.md", first)
        self.assertEqual(rest, CORE + "\n# Codex notes\n")

    def test_a_tool_without_an_appendix_gets_the_core_only(self):
        text = ins.build("claude", CORE, "")
        self.assertTrue(text.endswith(CORE))
        self.assertNotIn("instructions.claude.md", text.split("\n", 1)[0])

    def test_the_header_changes_when_the_inputs_change(self):
        self.assertNotEqual(
            ins.build("codex", CORE, "a").split("\n")[0],
            ins.build("codex", CORE, "b").split("\n")[0],
        )

    def test_lint_flags_hidden_bidi_characters_and_carriage_returns(self):
        bidi = chr(0x202E)
        self.assertEqual([lv for lv, _ in ins.lint("claude", "ok" + bidi)], ["ERROR"])
        self.assertEqual([lv for lv, _ in ins.lint("claude", "a\r\nb")], ["ERROR"])
        self.assertEqual(ins.lint("claude", "fine\n"), [])

    def test_the_antigravity_size_limit_is_a_hard_error(self):
        big = "x" * (ins.LIMITS["agy"] + 1)
        self.assertEqual([lv for lv, _ in ins.lint("agy", big)], ["ERROR"])
        self.assertEqual(ins.lint("codex", big), [])

    def test_the_claude_line_count_is_only_a_warning(self):
        levels = [lv for lv, _ in ins.lint("claude", "x\n" * 250)]
        self.assertEqual(levels, ["WARN"])


class ApplyTests(InstructionsCase):
    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_apply(False), 0)
        self.assertIn("dry-run", "\n".join(self.out))
        for tool in ins.TARGETS:
            self.assertFalse(self.target(tool).exists())
        self.assertFalse(locations.state_root(self.home).exists())

    def test_apply_writes_every_target_with_the_right_content(self):
        self.assertEqual(self.run_apply(True), 0)
        for tool in ins.TARGETS:
            text = self.target(tool).read_text(encoding="utf-8")
            self.assertIn("GENERATED FILE", text.split("\n", 1)[0])
            self.assertIn("be kind", text)
            self.assertNotIn("\r", text)
        self.assertIn("use the sandbox", self.target("codex").read_text(encoding="utf-8"))
        self.assertNotIn("use the sandbox", self.target("claude").read_text(encoding="utf-8"))
        self.assertEqual(ins.verify(self.home, self.cfg, out=lambda *_: None), 0)

    def test_a_second_apply_has_nothing_to_do(self):
        self.run_apply(True)
        self.out.clear()
        self.assertEqual(self.run_apply(True), 0)
        self.assertIn("no changes", "\n".join(self.out))

    def test_apply_refuses_when_one_target_has_a_lint_error_and_writes_nothing(self):
        (self.cfg / "instructions.agy.md").write_text(
            "x" * (ins.LIMITS["agy"] + 10), encoding="utf-8"
        )
        self.assertEqual(self.run_apply(True), 1)
        self.assertIn("REFUSED", "\n".join(self.out))
        for tool in ins.TARGETS:
            self.assertFalse(self.target(tool).exists())

    def test_a_missing_core_is_an_error(self):
        (self.cfg / "instructions.md").unlink()
        with self.assertRaises(ins.InstructionsError):
            self.run_apply(False)

    def test_tools_filter(self):
        self.assertEqual(self.run_apply(True, ["codex"]), 0)
        self.assertTrue(self.target("codex").exists())
        self.assertFalse(self.target("claude").exists())


class RollbackTests(InstructionsCase):
    def backup_dir(self):
        (b,) = list(locations.backups_dir(self.home).glob("backup-*-instructions"))
        return b

    def test_rollback_restores_old_content_and_removes_new_files(self):
        old = self.target("claude")
        old.parent.mkdir(parents=True)
        old.write_text("my hand-written rules\n", encoding="utf-8")
        self.run_apply(True)
        self.assertNotEqual(old.read_text(encoding="utf-8"), "my hand-written rules\n")
        self.assertEqual(ins.rollback(self.backup_dir(), out=lambda *_: None), 0)
        self.assertEqual(old.read_text(encoding="utf-8"), "my hand-written rules\n")
        self.assertFalse(self.target("codex").exists())  # did not exist before: removed again

    def test_rollback_refuses_a_file_changed_afterwards_unless_forced(self):
        self.run_apply(True)
        self.target("codex").write_text("edited later\n", encoding="utf-8")
        lines = []
        self.assertEqual(ins.rollback(self.backup_dir(), out=lines.append), 1)
        self.assertIn("changed after the apply", "\n".join(lines))
        self.assertEqual(self.target("codex").read_text(encoding="utf-8"), "edited later\n")
        self.assertEqual(ins.rollback(self.backup_dir(), force=True, out=lambda *_: None), 0)
        self.assertFalse(self.target("codex").exists())

    def test_the_backup_manifest_records_what_was_touched(self):
        self.run_apply(True)
        m = json.loads((self.backup_dir() / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["kind"], ins.MANIFEST_KIND)
        self.assertEqual({e["tool"] for e in m["entries"]}, set(ins.TARGETS))
        self.assertTrue(all(e["sha_after"] for e in m["entries"]))


class VerifyTests(InstructionsCase):
    def test_stale_or_hand_edited_files_are_drift(self):
        self.run_apply(True)
        self.target("opencode").write_text("hand edited\n", encoding="utf-8")
        self.assertEqual(ins.verify(self.home, self.cfg, out=lambda *_: None), 2)


class CliTests(InstructionsCase):
    def legacy(self, *args):
        cmd = [*legacy_command(*args), "--home", str(self.home)]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=cli_env({"ALL_AGENTS_CONFIG_DIR": str(self.cfg)}),
            timeout=120,
        )

    def test_apply_only_instructions_through_the_legacy_cli_and_rollback(self):
        r = self.legacy("apply", "--only", "instructions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.target("claude").exists())  # dry-run
        r = self.legacy("apply", "--only", "instructions", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.target("claude").exists())
        self.assertEqual(self.legacy("verify", "--only", "instructions").returncode, 0)
        bdir = (
            next(ln for ln in r.stdout.splitlines() if "To undo" in ln)
            .split('rollback "')[1]
            .rstrip('"')
        )
        r = self.legacy("rollback", bdir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.target("claude").exists())

    def test_a_bare_apply_never_touches_the_instruction_files(self):
        r = self.legacy("apply", "--only", "mcp")
        self.assertNotIn("instructions", r.stdout)
        self.assertFalse(self.target("claude").exists())


if __name__ == "__main__":
    unittest.main()
