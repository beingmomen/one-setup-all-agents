"""Tests of the ported core: the skills junction chain, check / --safe / --quiet.

Everything runs on a FAKE home with real junctions or symlinks (nothing touches the real home).
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import locations, skills
from support import WorkDirCase, cli_env, legacy_command

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "fixtures" / "servers.jsonc"


def mk_skill(root, name, body="x"):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return d


def run(*args, home, repo=None, source=SOURCE):
    cmd = [*legacy_command(*args), "--home", str(home), "--source", str(source)]
    if repo:
        cmd += ["--skills-dir", str(repo)]
    p = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, env=cli_env()
    )
    return p.returncode, p.stdout, p.stderr


class SkillsCase(WorkDirCase):
    def setUp(self):
        base = self.make_home()
        self.home = base / "home"
        self.repo = base / "repo" / "skills"
        self.a = self.home / ".agents" / "skills"
        self.c = self.home / ".claude" / "skills"
        for d in (self.repo, self.a, self.c):
            d.mkdir(parents=True)

    def plan(self, **kw):
        return skills.build_plan(self.home, self.repo, **kw)

    def apply(self, **kw):
        p = self.plan(**{k: v for k, v in kw.items() if k in ("claude_exclude", "adopt")})
        mp = skills.apply_plan(p, self.home, kw.get("safe", False))
        return p, mp

    def target_of(self, link):
        return skills.link_target(link)

    def assertChain(self, name, repo_backed=True):
        self.assertTrue(skills.is_link(self.c / name), f"{name}: claude link missing")
        self.assertEqual(skills.norm(self.target_of(self.c / name)), skills.norm(self.a / name))
        self.assertTrue((self.c / name / "SKILL.md").is_file())
        if repo_backed:
            self.assertTrue(skills.is_link(self.a / name))
            self.assertEqual(
                skills.norm(self.target_of(self.a / name)), skills.norm(self.repo / name)
            )


class ChainTests(SkillsCase):
    def test_empty_home_gets_both_layers_and_is_idempotent(self):
        for n in ("alpha", "beta"):
            mk_skill(self.repo, n)
        p, _ = self.apply()
        self.assertEqual(len(p.ops), 4)
        self.assertChain("alpha")
        self.assertChain("beta")
        self.assertEqual(self.plan().ops, [])
        self.assertEqual(self.plan().conflicts, [])

    def test_skill_installed_only_in_agents_gets_a_claude_link_and_stays_real(self):
        mk_skill(self.a, "third-party")
        mk_skill(self.repo, "mine")
        self.apply()
        self.assertFalse(skills.is_link(self.a / "third-party"))  # still a real folder
        self.assertChain("third-party", repo_backed=False)
        self.assertChain("mine")

    def test_folder_without_skill_md_in_repo_is_not_published(self):
        (self.repo / "notes").mkdir()
        p = self.plan()
        self.assertEqual(p.ops, [])
        self.assertTrue(any("notes" in i for i in p.info))

    def test_dead_junctions_are_removed_and_target_content_is_not_touched(self):
        keep = mk_skill(self.repo, "gone")
        self.apply()
        shutil.rmtree(keep)  # the repo skill disappears: both links are now dead
        self.assertEqual(
            sorted(o.layer for o in self.plan().ops if o.kind == "remove_dead"),
            ["agents", "claude"],
        )
        self.apply()
        self.assertFalse(os.path.lexists(self.a / "gone"))
        self.assertFalse(os.path.lexists(self.c / "gone"))
        self.assertEqual(self.plan().ops, [])

    def test_removing_a_live_junction_leaves_the_target_folder_intact(self):
        t = mk_skill(self.home / "elsewhere", "payload")
        (t / "extra.txt").write_text("keep me", encoding="utf-8")
        link = self.home / "lnk"
        skills.make_link(link, t)
        skills.remove_link(link)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual((t / "extra.txt").read_text(encoding="utf-8"), "keep me")

    def test_remove_link_refuses_a_real_directory(self):
        d = mk_skill(self.home / "real", "x")
        with self.assertRaises(skills.SkillsError):
            skills.remove_link(d)
        self.assertTrue((d / "SKILL.md").exists())

    def test_dead_claude_link_to_a_live_agents_skill_is_replaced(self):
        mk_skill(self.a, "tp")
        tmp = mk_skill(self.home / "tmp", "tp")
        skills.make_link(self.c / "tp", tmp)
        shutil.rmtree(tmp)
        self.apply()
        self.assertChain("tp", repo_backed=False)

    def test_junction_pointing_elsewhere_is_reported_not_changed(self):
        other = mk_skill(self.home / "other", "x")
        mk_skill(self.repo, "x")
        skills.make_link(self.a / "x", other)
        p = self.plan()
        self.assertTrue(any(n == "x" for n, _ in p.conflicts))
        self.assertFalse(any(o.layer == "agents" and o.name == "x" for o in p.ops))

    def test_reserved_claude_names_are_never_touched(self):
        for n in (".trash", "synced"):
            mk_skill(self.c, n)
        mk_skill(self.a, ".trash")
        p = self.plan()
        self.assertEqual(p.ops, [])
        self.assertEqual(p.conflicts, [])

    def test_claude_exclude(self):
        mk_skill(self.a, "hidden")
        mk_skill(self.a, "shown")
        self.apply(claude_exclude=["hidden"])
        self.assertFalse(os.path.lexists(self.c / "hidden"))
        self.assertChain("shown", repo_backed=False)

    def test_claude_junction_to_a_foreign_target_is_left_alone(self):
        foreign = mk_skill(self.home / "foreign", "f")
        skills.make_link(self.c / "f", foreign)
        p = self.plan()
        self.assertEqual(p.ops, [])
        self.assertTrue(any("f" in i for i in p.info))

    def test_a_failing_step_reverses_the_steps_that_ran(self):
        for n in ("a1", "a2", "a3"):
            mk_skill(self.repo, n)
        real = skills.make_link
        calls = {"n": 0}

        def flaky(link, target):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("boom")
            return real(link, target)

        skills.make_link = flaky
        try:
            with self.assertRaises(skills.SkillsError):
                self.apply()
        finally:
            skills.make_link = real
        self.assertEqual([n for n in os.listdir(self.a)], [])
        self.assertEqual([n for n in os.listdir(self.c)], [])

    def test_precondition_is_rechecked_before_each_step(self):
        mk_skill(self.repo, "late")
        p = self.plan()
        mk_skill(self.a, "late")  # appears after the plan was made
        with self.assertRaises(skills.SkillsError):
            skills.apply_plan(p, self.home, False)
        self.assertFalse(skills.is_link(self.a / "late"))
        self.assertTrue((self.a / "late" / "SKILL.md").exists())


class RealDirectoryTests(SkillsCase):
    def test_real_directory_in_claude_that_agents_also_has_is_reported_and_kept(self):
        mk_skill(self.a, "dup", "agents copy")
        d = mk_skill(self.c, "dup", "claude copy")
        (d / "mine.txt").write_text("precious", encoding="utf-8")
        p = self.plan()
        self.assertTrue(any(n == "dup" and "real directory" in t for n, t in p.conflicts))
        self.assertEqual(p.ops, [])
        self.apply()
        self.assertEqual((d / "mine.txt").read_text(encoding="utf-8"), "precious")
        self.assertFalse(skills.is_link(d))

    def test_real_directory_only_in_claude_is_reported_with_the_adopt_hint(self):
        mk_skill(self.c, "solo")
        p = self.plan()
        self.assertTrue(any(n == "solo" and "--adopt solo" in t for n, t in p.conflicts))
        self.assertEqual(p.ops, [])

    def test_real_directory_in_agents_where_the_repo_has_the_skill_is_a_conflict(self):
        mk_skill(self.repo, "both")
        mk_skill(self.a, "both")
        p = self.plan()
        self.assertTrue(any(n == "both" for n, _ in p.conflicts))
        self.assertTrue(skills.is_link(self.a / "both") is False)

    def test_adopt_from_claude_moves_into_repo_with_a_verified_backup(self):
        d = mk_skill(self.c, "solo", "body")
        (d / "sub").mkdir()
        (d / "sub" / "f.txt").write_text("data", encoding="utf-8")
        p, mp = self.apply(adopt=["solo"])
        self.assertEqual([o.kind for o in p.ops][:1], ["adopt"])
        self.assertTrue((self.repo / "solo" / "sub" / "f.txt").is_file())
        self.assertChain("solo")
        bdir = mp.parent
        self.assertEqual(
            (bdir / "files" / "adopted" / "solo" / "sub" / "f.txt").read_text(encoding="utf-8"),
            "data",
        )
        self.assertEqual(self.plan().ops, [])

    def test_adopt_from_agents(self):
        mk_skill(self.a, "tp", "body")
        self.apply(adopt=["tp"])
        self.assertTrue((self.repo / "tp" / "SKILL.md").is_file())
        self.assertChain("tp")

    def test_adopt_rollback_restores_the_real_directory(self):
        d = mk_skill(self.c, "solo", "body")
        (d / "f.txt").write_text("data", encoding="utf-8")
        _, mp = self.apply(adopt=["solo"])
        skills.rollback(mp, out=lambda *_: None)
        self.assertFalse(skills.is_link(self.c / "solo"))
        self.assertEqual((self.c / "solo" / "f.txt").read_text(encoding="utf-8"), "data")
        self.assertFalse(os.path.lexists(self.repo / "solo"))
        self.assertFalse(os.path.lexists(self.a / "solo"))

    def test_adopt_refuses_when_the_repo_already_has_the_name(self):
        mk_skill(self.repo, "solo", "repo")
        mk_skill(self.c, "solo", "claude")
        p = self.plan(adopt=["solo"])
        self.assertTrue(any(n == "solo" and "already has" in t for n, t in p.conflicts))
        self.assertFalse(any(o.kind == "adopt" for o in p.ops))

    def test_adopt_rejects_path_like_names_and_unknown_skills(self):
        p = self.plan(adopt=["../x", "nothing"])
        self.assertEqual(len([c for c in p.conflicts if c[0] in ("../x", "nothing")]), 2)

    def test_safe_mode_cannot_adopt(self):
        mk_skill(self.c, "solo")
        p = self.plan(adopt=["solo"])
        with self.assertRaises(skills.SkillsError):
            skills.apply_plan(p, self.home, True)
        self.assertFalse(skills.is_link(self.c / "solo"))
        self.assertTrue((self.c / "solo" / "SKILL.md").exists())


class SafeAndJournalTests(SkillsCase):
    def test_safe_apply_uses_the_state_journal_not_a_backup_folder(self):
        mk_skill(self.repo, "s")
        _, mp = self.apply(safe=True)
        self.assertEqual(mp.parent.parent, locations.skills_journal_dir(self.home))
        self.assertEqual(json.loads(mp.read_text(encoding="utf-8"))["status"], "complete")
        self.assertEqual([p.name for p in locations.backups_dir(self.home).glob("backup-*")], [])

    def test_rollback_removes_created_links_and_recreates_removed_dead_ones(self):
        t = mk_skill(self.home / "t", "old")
        skills.make_link(self.a / "old", t)
        skills.make_link(self.c / "old", self.a / "old")
        shutil.rmtree(t)  # dead chain
        mk_skill(self.repo, "new")
        _, mp = self.apply()
        self.assertChain("new")
        self.assertFalse(os.path.lexists(self.a / "old"))
        skills.rollback(mp, out=lambda *_: None)
        self.assertFalse(os.path.lexists(self.a / "new"))
        self.assertFalse(os.path.lexists(self.c / "new"))
        self.assertTrue(skills.is_link(self.a / "old"))  # the dead link is back, as it was


class CliTests(SkillsCase):
    def cli(self, *args, **kw):
        return run(*args, home=self.home, repo=self.repo, **kw)

    def test_dry_run_writes_nothing_and_apply_does(self):
        mk_skill(self.repo, "s")
        rc, out, _ = self.cli("apply", "--only", "skills")
        self.assertEqual(rc, 0)
        self.assertIn("create", out)
        self.assertIn("dry-run", out)
        self.assertEqual(os.listdir(self.a), [])
        rc, out, _ = self.cli("apply", "--only", "skills", "--apply")
        self.assertEqual(rc, 0)
        self.assertChain("s")
        self.assertIn("applied and verified", out)
        self.assertEqual(self.cli("verify", "--only", "skills")[0], 0)

    def test_cli_rollback_dispatches_to_the_skills_manifest(self):
        mk_skill(self.repo, "s")
        _, out, _ = self.cli("apply", "--only", "skills", "--apply")
        bdir = (
            next(l for l in out.splitlines() if "To undo" in l).split('rollback "')[1].rstrip('"')
        )
        rc, out, _ = self.cli("rollback", bdir)
        self.assertEqual(rc, 0)
        self.assertEqual(os.listdir(self.c), [])

    def test_safe_implies_apply_and_is_quiet_when_asked(self):
        mk_skill(self.repo, "s")
        rc, out, err = self.cli("apply", "--only", "skills", "--safe", "--quiet")
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertChain("s")
        rc, out, err = self.cli(
            "apply", "--only", "skills", "--safe", "--quiet"
        )  # nothing to do: still silent
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_safe_alone_means_skills_only_and_rejects_other_combinations(self):
        mk_skill(self.repo, "s")
        self.assertEqual(self.cli("apply", "--safe", "--quiet")[0], 0)
        self.assertChain("s")
        self.assertEqual(self.cli("apply", "--safe", "--only", "mcp")[0], 1)
        self.assertEqual(self.cli("apply", "--safe", "--adopt", "x")[0], 1)

    def test_quiet_turns_errors_into_one_stderr_line_and_exit_zero(self):
        bad = self.home.parent / "bad-source.jsonc"
        bad.write_text("{not json", encoding="utf-8")
        rc, out, err = self.cli("apply", "--only", "mcp", "--quiet", source=bad)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("all-agents:"))
        rc, _, err = self.cli("apply", "--only", "mcp", source=bad)  # not quiet: a real error
        self.assertEqual(rc, 1)

    def test_check_is_silent_when_in_sync_and_one_line_on_drift(self):
        shutil.copytree(Path(__file__).parent / "fixtures" / "home", self.home, dirs_exist_ok=True)
        mk_skill(self.repo, "s")
        rc, out, err = self.cli("check", "--quiet")
        self.assertEqual(rc, 0)  # exit 0 even with drift
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("skills(", out)
        self.assertEqual(self.cli("check", "--strict", "--quiet")[0], 2)

    def test_check_quiet_prints_nothing_when_skills_are_in_sync(self):
        # an empty MCP source and a home without tool configs would be "unreadable"; use the fixture home fully applied
        home = self.home
        shutil.copytree(Path(__file__).parent / "fixtures" / "home", home, dirs_exist_ok=True)
        rc, _, _ = run(
            "apply",
            "--apply",
            "--only",
            "mcp",
            "--tool",
            "opencode,codex,agy",
            home=home,
            repo=self.repo,
        )
        self.assertEqual(rc, 0)
        mk_skill(self.repo, "s")
        self.cli("apply", "--only", "skills", "--apply")
        rc, out, err = run("check", "--quiet", home=home, repo=self.repo)
        self.assertEqual(rc, 0)
        self.assertNotIn("skills", out)  # nothing to do for skills ...
        self.assertEqual(
            err, ""
        )  # ... (the fixture home still has MCP drift for claude, which is not what this checks)

    def test_check_never_fails_a_hook_on_a_missing_repo_folder(self):
        shutil.copytree(Path(__file__).parent / "fixtures" / "home", self.home, dirs_exist_ok=True)
        rc, out, err = run(
            "check",
            "--quiet",
            home=self.home,
            repo=self.home / "no-such-dir",
        )
        self.assertEqual(rc, 0)

    def test_bad_only_value(self):
        self.assertEqual(self.cli("apply", "--only", "nope")[0], 1)

    def test_a_full_apply_runs_mcp_then_skills_in_one_command(self):
        shutil.copytree(Path(__file__).parent / "fixtures" / "home", self.home, dirs_exist_ok=True)
        mk_skill(self.repo, "s")
        rc, out, _ = self.cli("apply", "--tool", "agy")
        self.assertEqual(rc, 0)
        self.assertLess(out.index("[agy]"), out.index("[skills]"))


if __name__ == "__main__":
    unittest.main()
