"""agy does not follow junctions, so the skills it cannot see are listed in agy's own skills.json manifest
(global ~/.gemini/config/skills.json), by resolved path, merged with foreign entries.
Fake home and temp repos only; nothing real is touched."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import agymanifest as am
from one_setup_all_agents._core import apply as ap
from one_setup_all_agents._core import locations, skills
from support import WORK
from test_core_skills import SkillsCase, mk_skill, run

REL = "REL"


def read_json(path):
    return json.loads(Path(path).read_bytes().decode("utf-8-sig"))


def paths(path):
    return [e["path"] for e in read_json(path)["entries"]]


def agy_plan(case, **kw):
    return skills.build_plan(case.home, case.repo, agy_manifest=True, **kw)


# ---------------------------------------------------------------- the merge itself
class MergeTests(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(dir=WORK))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.f = self.dir / "skills.json"

    def put(self, data, raw=None):
        self.f.write_bytes(raw if raw is not None else json.dumps(data, indent=2).encode("utf-8"))

    def merge(self, wanted, owned=(), base=None, blocked=None):
        return am.plan_merge(self.f, base, wanted, list(owned), blocked)

    def test_a_new_file_is_created_with_the_wanted_entries(self):
        mp = self.merge(["C:/x/a", "C:/x/b"])
        self.assertEqual(mp.kind, "create")
        self.assertEqual(
            json.loads(mp.new_bytes), {"entries": [{"path": "C:/x/a"}, {"path": "C:/x/b"}]}
        )
        self.assertEqual(mp.owned, ["C:/x/a", "C:/x/b"])

    def test_foreign_entries_and_other_keys_stay_in_place_and_ours_go_last(self):
        self.put(
            {
                "note": "keep",
                "entries": [
                    {"path": "C:/foreign/one"},
                    {"path": "C:/foreign/two", "exclude": [".trash"]},
                ],
            }
        )
        mp = self.merge(["C:/x/a"])
        data = json.loads(mp.new_bytes)
        self.assertEqual(data["note"], "keep")
        self.assertEqual(data["entries"][:2], read_json(self.f)["entries"])
        self.assertEqual(data["entries"][2], {"path": "C:/x/a"})
        self.assertEqual(list(data), ["note", "entries"])

    def test_nothing_to_do_is_a_no_op_and_ownership_comes_from_the_ledger_only(self):
        self.put({"entries": [{"path": "C:/x/a"}]})
        self.assertEqual(
            self.merge(["C:/x/a"]).kind, "none"
        )  # already listed by someone else: not ours, not added
        self.assertEqual(self.merge(["C:/x/a"]).owned, [])
        mp = self.merge(["C:/x/a"], owned=["C:/x/a"])
        self.assertEqual(mp.kind, "none")
        self.assertEqual(mp.owned, ["C:/x/a"])

    def test_only_owned_plain_entries_are_removed(self):
        self.put(
            {
                "entries": [
                    {"path": "C:/x/a"},
                    {"path": "C:/x/b"},
                    {"path": "C:/x/c", "exclude": ["z"]},
                    {"path": "C:/f/keep"},
                ]
            }
        )
        mp = self.merge(["C:/x/a"], owned=["C:/x/a", "C:/x/b", "C:/x/c"])
        self.assertEqual(
            mp.remove, ["C:/x/b"]
        )  # c was customised by the user: treated as theirs; keep is theirs too
        self.assertEqual(
            [e["path"] for e in json.loads(mp.new_bytes)["entries"]],
            ["C:/x/a", "C:/x/c", "C:/f/keep"],
        )

    def test_a_skill_already_covered_by_a_foreign_folder_entry_is_not_added(self):
        self.put({"entries": [{"path": "C:/lib/skills"}]})
        mp = self.merge(["C:/lib/skills/inside", "C:/other/solo"])
        self.assertEqual(mp.add, ["C:/other/solo"])

    def test_an_owned_entry_that_a_new_foreign_folder_covers_is_dropped(self):
        self.put({"entries": [{"path": "C:/lib/skills/inside"}, {"path": "C:/lib/skills"}]})
        mp = self.merge(["C:/lib/skills/inside"], owned=["C:/lib/skills/inside"])
        self.assertEqual(mp.remove, ["C:/lib/skills/inside"])

    def test_relative_entries_are_compared_against_the_project_folder(self):
        self.put({"entries": [{"path": ".claude/skills"}]})
        mp = self.merge([".claude/skills/x"], base=self.dir)
        self.assertEqual(mp.kind, "none")  # covered by the foreign relative folder entry
        mp = self.merge([".claude/skills/x", "../elsewhere/y"], base=self.dir)
        self.assertEqual(mp.add, ["../elsewhere/y"])

    def test_slash_style_does_not_make_duplicates(self):
        self.put({"entries": [{"path": "C:\\x\\a"}]})
        self.assertEqual(self.merge(["C:/x/a"]).kind, "none")

    @unittest.skipUnless(os.name == "nt", "paths are case-insensitive on Windows only")
    def test_case_does_not_make_duplicates_on_windows(self):
        self.put({"entries": [{"path": "c:\\X\\A"}]})
        self.assertEqual(self.merge(["C:/x/a"]).kind, "none")

    def test_an_unreadable_file_is_blocked_and_never_rewritten(self):
        self.put(None, raw=b"{ not json")
        mp = self.merge(["C:/x/a"])
        self.assertEqual(mp.kind, "blocked")
        self.assertIsNone(mp.new_bytes)
        self.put(None, raw=b'{"entries": "nope"}')
        self.assertEqual(self.merge(["C:/x/a"]).kind, "blocked")

    def test_blocked_reason_forbids_the_write_but_still_reports(self):
        self.put({"entries": []})
        mp = self.merge(["C:/x/a"], blocked="tracked team file")
        self.assertEqual((mp.kind, mp.reason, mp.add), ("blocked", "tracked team file", ["C:/x/a"]))
        self.assertFalse(mp.actionable)

    def test_crlf_and_bom_are_kept(self):
        raw = b"\xef\xbb\xbf" + json.dumps({"entries": [{"path": "C:/f/one"}]}, indent=2).replace(
            "\n", "\r\n"
        ).encode("utf-8")
        self.put(None, raw=raw)
        out = self.merge(["C:/x/a"]).new_bytes
        self.assertTrue(out.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", out)
        self.assertNotIn(b"\n", out.replace(b"\r\n", b""))

    def test_write_then_undo_restores_the_exact_bytes_or_deletes_a_new_file(self):
        self.put({"entries": [{"path": "C:/f/one"}]})
        before = self.f.read_bytes()
        mp = self.merge(["C:/x/a"])
        bk = self.dir / "bk"
        rec = am.write(mp, bk)
        self.assertEqual(paths(self.f), ["C:/f/one", "C:/x/a"])
        am.undo(rec, bk)
        self.assertEqual(self.f.read_bytes(), before)
        self.f.unlink()
        rec = am.write(self.merge(["C:/x/a"]), bk)
        am.undo(rec, bk)
        self.assertFalse(self.f.exists())

    def test_undo_after_a_foreign_edit_removes_only_ours_and_puts_removed_ones_back(self):
        self.put({"entries": [{"path": "C:/x/old"}, {"path": "C:/f/one"}]})
        mp = self.merge(["C:/x/new"], owned=["C:/x/old"])
        rec = am.write(mp, self.dir / "bk")
        data = read_json(self.f)
        data["entries"].append({"path": "C:/f/added-later"})
        self.f.write_bytes(json.dumps(data).encode("utf-8"))  # someone edits the file meanwhile
        am.undo(rec, self.dir / "bk")
        self.assertEqual(
            sorted(paths(self.f)), sorted(["C:/x/old", "C:/f/one", "C:/f/added-later"])
        )

    def test_write_refuses_a_file_that_changed_since_the_plan(self):
        self.put({"entries": []})
        mp = self.merge(["C:/x/a"])
        self.put({"entries": [{"path": "C:/f/other"}]})
        with self.assertRaises(am.ManifestError):
            am.write(mp)
        self.assertEqual(paths(self.f), ["C:/f/other"])


# ---------------------------------------------------------------- global manifest
class GlobalManifest(SkillsCase):
    def setUp(self):
        super().setUp()
        self.mf = am.global_path(self.home)

    def apply_manifest(self, safe=False, **kw):
        plan = agy_plan(self, **kw)
        return plan, skills.apply_plan(plan, self.home, safe)

    def test_junction_skills_are_listed_by_resolved_path_and_real_folders_are_not(self):
        mk_skill(self.repo, "mine")
        mk_skill(self.repo, "other")
        mk_skill(self.a, "third-party")  # a real folder in ~/.agents/skills: agy reads it by itself
        plan, _ = self.apply_manifest()
        self.assertEqual([o.kind for o in plan.ops if o.layer == "agy"], ["manifest"])
        got = paths(self.mf)
        self.assertEqual(
            sorted(got),
            sorted(
                [
                    am.fwd(os.path.realpath(self.repo / "mine")),
                    am.fwd(os.path.realpath(self.repo / "other")),
                ]
            ),
        )
        self.assertFalse(any(p.endswith("third-party") for p in got))
        self.assertFalse(
            any(skills.norm(p) == skills.norm(self.a / "mine") for p in got)
        )  # never the junction path itself
        self.assertEqual(ap.read_ledger_agy_skills(self.home), sorted(got, key=str.lower))
        self.assertEqual(agy_plan(self).ops, [])  # idempotent
        self.assertEqual(
            skills.build_plan(self.home, self.repo).ops, []
        )  # and the junction chain is untouched

    def test_a_junction_installed_by_other_means_is_listed_by_its_real_target(self):
        other = self.home / "elsewhere" / "tp"
        mk_skill(other.parent, "tp")
        skills.make_link(self.a / "tp", other)
        self.apply_manifest()
        self.assertEqual(paths(self.mf), [am.fwd(os.path.realpath(other))])

    def test_foreign_entries_are_preserved_in_place_and_never_owned(self):
        self.mf.parent.mkdir(parents=True)
        self.mf.write_text(
            json.dumps({"entries": [{"path": am.fwd(self.a)}, {"path": "D:/mine/own"}]}, indent=2),
            encoding="utf-8",
        )
        mk_skill(self.repo, "mine")
        self.apply_manifest()
        got = paths(self.mf)
        self.assertEqual(got[:2], [am.fwd(self.a), "D:/mine/own"])
        self.assertEqual(len(got), 3)
        self.assertNotIn(am.fwd(self.a), ap.read_ledger_agy_skills(self.home))
        # the skill goes away: only OUR entry is dropped
        shutil.rmtree(self.repo / "mine")
        plan = agy_plan(self)
        self.assertIn("drop 1 entry", skills.render_plan(plan))
        skills.apply_plan(plan, self.home, False)
        self.assertEqual(paths(self.mf), [am.fwd(self.a), "D:/mine/own"])
        self.assertEqual(ap.read_ledger_agy_skills(self.home), [])

    def test_agy_exclude(self):
        mk_skill(self.repo, "mine")
        mk_skill(self.repo, "other")
        self.apply_manifest(agy_exclude=["other"])
        self.assertEqual([os.path.basename(p) for p in paths(self.mf)], ["mine"])
        self.assertEqual(
            [o for o in skills.build_plan(self.home, self.repo).ops if o.layer == "agy"], []
        )  # off by default

    def test_a_skill_that_became_a_real_folder_is_dropped_from_the_manifest(self):
        mk_skill(self.repo, "mine")
        self.apply_manifest()
        skills.remove_link(self.a / "mine")
        shutil.rmtree(self.repo / "mine")
        mk_skill(self.a, "mine", "real now")
        self.apply_manifest()
        self.assertEqual(paths(self.mf), [])

    def test_a_broken_manifest_is_a_conflict_and_is_not_touched(self):
        mk_skill(self.repo, "mine")
        self.mf.parent.mkdir(parents=True)
        self.mf.write_text("{ broken", encoding="utf-8")
        plan = agy_plan(self)
        self.assertEqual([o for o in plan.ops if o.layer == "agy"], [])
        self.assertTrue(any("BLOCKED" in t for _, t in plan.conflicts), plan.conflicts)
        self.assertEqual(self.mf.read_text(encoding="utf-8"), "{ broken")

    def test_rollback_restores_the_file_and_the_ledger(self):
        self.mf.parent.mkdir(parents=True)
        original = json.dumps({"entries": [{"path": "D:/foreign"}]}, indent=2).encode("utf-8")
        self.mf.write_bytes(original)
        mk_skill(self.repo, "mine")
        _, mp = self.apply_manifest()
        self.assertEqual(len(paths(self.mf)), 2)
        skills.rollback(mp, out=lambda *_: None)
        self.assertEqual(self.mf.read_bytes(), original)
        self.assertEqual(ap.read_ledger_agy_skills(self.home), [])

    def test_rollback_deletes_a_file_the_tool_created(self):
        mk_skill(self.repo, "mine")
        _, mp = self.apply_manifest()
        self.assertTrue(self.mf.is_file())
        skills.rollback(mp, out=lambda *_: None)
        self.assertFalse(self.mf.exists())

    def test_adopt_then_manifest_in_one_plan(self):
        mk_skill(self.a, "adoptme", "body")
        plan = skills.build_plan(self.home, self.repo, adopt=["adoptme"], agy_manifest=True)
        self.assertEqual([o.kind for o in plan.ops if o.layer == "agy"], ["manifest"])
        skills.apply_plan(plan, self.home, False)
        self.assertEqual(paths(self.mf), [am.fwd(os.path.realpath(self.repo / "adoptme"))])

    def test_a_held_lock_fails_the_write_without_changing_anything(self):
        mk_skill(self.repo, "mine")
        plan = agy_plan(self)
        with ap.Lock(self.home):
            with self.assertRaises(skills.SkillsError):
                skills.apply_plan(plan, self.home, True)
        self.assertFalse(self.mf.exists())


class GlobalManifestCli(SkillsCase):
    def setUp(self):
        super().setUp()
        self.mf = am.global_path(self.home)

    def cli(self, *args):
        return run(*args, home=self.home, repo=self.repo)

    def test_dry_run_apply_verify_and_rollback(self):
        for i in range(3):
            mk_skill(self.repo, f"s{i}")
        rc, out, err = self.cli("apply", "--only", "skills")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("list 3 skill(s) agy cannot see on its own", out)
        self.assertFalse(self.mf.exists())  # dry-run wrote nothing
        self.assertEqual(self.cli("verify", "--only", "skills")[0], 2)
        rc, out, err = self.cli("apply", "--only", "skills", "--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(paths(self.mf)), 3)
        self.assertEqual(self.cli("verify", "--only", "skills")[0], 0)
        # drift: a listed skill vanishes
        shutil.rmtree(self.repo / "s1")
        rc, out2, _ = self.cli("verify", "--only", "skills")
        self.assertEqual(rc, 2)
        self.assertIn("drop 1 entry", out2)
        line = next(
            x
            for x in self.cli("apply", "--only", "skills", "--apply")[1].splitlines()
            if "To undo" in x
        )
        folder = line.split('rollback "', 1)[1].rsplit('"', 1)[0]
        rc, out4, err4 = self.cli("rollback", folder)
        self.assertEqual(rc, 0, out4 + err4)

    def test_hook_mode_is_fast_quiet_and_fails_open(self):
        for i in range(20):
            mk_skill(self.repo, f"skill-{i:02d}")
            (self.repo / f"skill-{i:02d}" / "ref.md").write_text("x" * 5000, encoding="utf-8")
        rc, out, err = self.cli("apply", "--only", "skills", "--safe", "--quiet")
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(len(paths(self.mf)), 20)
        before = self.mf.read_bytes()
        rc, out, err = self.cli("apply", "--only", "skills", "--safe", "--quiet")
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(
            self.mf.read_bytes(), before
        )  # the second run is a no-op (no journal, no rewrite)
        self.assertEqual(len(os.listdir(locations.skills_journal_dir(self.home))), 1)
        # an unreadable manifest must not fail the session start
        self.mf.write_text("{ broken", encoding="utf-8")
        rc, out, err = self.cli("apply", "--only", "skills", "--safe", "--quiet")
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.mf.read_text(encoding="utf-8"), "{ broken")

    def test_check_reports_a_missing_entry_as_skills_drift(self):
        mk_skill(self.repo, "mine")
        rc, out, err = self.cli("check")
        self.assertEqual(rc, 0)
        self.assertIn("skills(", out)


# ---------------------------------------------------------------- rollback of the journal of the first design (marked copies)


class HermeticWorkFolder(unittest.TestCase):
    def test_every_test_process_has_its_own_scratch_folder(self):
        # two suites (or a suite and one test file) running together used to share tests/_work and delete each other's temp homes
        self.assertEqual(WORK.name, f"p{os.getpid()}")
        self.assertEqual(WORK.parent.name, "one-setup-all-agents-tests")


# ---------------------------------------------------------------- project manifests
SK = "---\nname: {0}\ndescription: d\n---\n{1}\n"


if __name__ == "__main__":
    unittest.main()
