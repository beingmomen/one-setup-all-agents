"""Prune tests: age, newest per kind, ledger / rollback protection, links never followed, dry-run default,
auto-prune after apply and --no-prune. Everything runs on temp fake homes; the fixed "today" is 2026-11-02."""

import datetime
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import apply as ap
from one_setup_all_agents._core import locations, prune, skills
from support import WorkDirCase, cli_env, digest, legacy_command
from test_core_apply import cli

TODAY = datetime.date(2026, 11, 2)


class PruneCase(WorkDirCase):
    def setUp(self):
        self.home = self.make_home() / "home"
        self.state = locations.state_root(self.home)
        self.agents = locations.backups_dir(self.home)
        self.agents.mkdir(parents=True)
        self.lock = locations.lock_file(self.home)
        self.ledger = ap.ledger_path(self.home)

    def backup(self, name, size=10, manifest=None):
        d = self.agents / name
        (d / "files").mkdir(parents=True)
        (d / "files" / "x.txt").write_bytes(b"x" * size)
        if manifest is not None:
            (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return d

    def prune_cli(self, *args, today="2026-11-02"):
        cmd = [*legacy_command("prune"), "--home", str(self.home), "--today", today, *args]
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, env=cli_env()
        )
        return p.returncode, p.stdout, p.stderr

    def names(self):
        return sorted(x.name for x in self.agents.glob("backup-*"))

    def deleted_by_plan(self, older=14):
        return sorted(b.name for b in prune.plan(self.home, older, TODAY) if b.action == "delete")


class NameParsing(unittest.TestCase):
    def test_kind_date_and_counters(self):
        cases = {
            "backup-2026-11-01-175432-sync-3": (
                "sync",
                datetime.datetime(2026, 11, 1, 17, 54, 32),
                3,
            ),
            "backup-2026-11-01-175432-sync": (
                "sync",
                datetime.datetime(2026, 11, 1, 17, 54, 32),
                1,
            ),
            "backup-2026-10-31-6c2": ("6c2", datetime.datetime(2026, 10, 31), 1),
            "backup-2026-11-02-impeccable-removal": (
                "impeccable-removal",
                datetime.datetime(2026, 11, 2),
                1,
            ),
            "backup-2026-11-01-171306-phase4-baseline": (
                "phase4-baseline",
                datetime.datetime(2026, 11, 1, 17, 13, 6),
                1,
            ),
        }
        for name, (kind, when, counter) in cases.items():
            b = prune.Backup(Path("/nowhere") / name, TODAY)
            self.assertEqual(
                (b.kind, b.when, b.counter, b.dated), (kind, when, counter, True), name
            )


class AgeAndKinds(PruneCase):
    def test_age_threshold(self):
        self.backup("backup-2026-10-10-100000-aa")  # 23 days, but the newest of its kind
        self.backup("backup-2026-10-10-100000-bb")
        self.backup("backup-2026-10-20-100000-bb")  # 13 days: young
        self.backup("backup-2026-10-19-100000-bb")  # exactly 14 days: not older than 14
        self.backup("backup-2026-10-18-100000-bb")  # 15 days: old
        self.assertEqual(
            self.deleted_by_plan(), ["backup-2026-10-10-100000-bb", "backup-2026-10-18-100000-bb"]
        )
        self.assertIn("backup-2026-10-10-100000-aa", self.names())

    def test_newest_of_each_kind_is_kept_even_when_old(self):
        self.backup("backup-2026-09-01-100000-sync")
        self.backup("backup-2026-09-02-100000-sync")
        self.backup("backup-2026-09-02-100000-sync-2")  # counter: newest sync
        self.backup("backup-2026-09-03-100000-hooks")
        self.backup("backup-2026-09-01-6a")
        self.backup("backup-2026-09-01-6c2")
        self.assertEqual(
            self.deleted_by_plan(),
            ["backup-2026-09-01-100000-sync", "backup-2026-09-02-100000-sync"],
        )

    def test_mtime_is_the_fallback_without_a_date(self):
        old = self.backup("backup-manual-old-a")
        older = self.backup("backup-manual-old-a-2")
        os.utime(old, (1_000_000_000, 1_000_000_000))
        os.utime(older, (1_100_000_000, 1_100_000_000))
        self.assertEqual(
            self.deleted_by_plan(), ["backup-manual-old-a"]
        )  # same kind, the newer mtime wins

    def test_files_and_other_folders_are_not_backups(self):
        (self.agents / "backup-2020-01-01-file").write_text("x")
        (self.agents / "other").mkdir()
        (self.agents / "skills").mkdir()
        self.assertEqual(prune.plan(self.home, 0, TODAY), [])


class Protection(PruneCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.home / "config.json"
        self.cfg.write_text("current", encoding="utf-8")
        self.sha = ap.sha(b"current")

    def hooks_manifest(self, sha_after):
        return {
            "kind": "legacy-files",
            "status": "complete",
            "files": [{"tool": "t", "path": str(self.cfg), "sha_after": sha_after}],
            "ledger": {"path": str(self.ledger), "sha_after": None},
        }

    def test_latest_rollback_point_of_a_file_is_kept(self):
        self.backup(
            "backup-2026-09-01-100000-a", manifest=self.hooks_manifest(self.sha)
        )  # matches the file as it is now
        self.backup(
            "backup-2026-09-01-100000-b", manifest=self.hooks_manifest("0" * 64)
        )  # the file changed after it
        self.backup("backup-2026-09-05-100000-a")
        self.backup("backup-2026-09-05-100000-b")
        self.assertEqual(self.deleted_by_plan(), ["backup-2026-09-01-100000-b"])
        kept = {b.name: b.reason for b in prune.plan(self.home, 14, TODAY)}
        self.assertIn("latest rollback point of config.json", kept["backup-2026-09-01-100000-a"])

    def test_only_the_newest_backup_per_file_counts(self):
        self.backup("backup-2026-09-01-100000-a", manifest=self.hooks_manifest(self.sha))
        self.backup("backup-2026-09-02-100000-b", manifest=self.hooks_manifest(self.sha))
        self.backup("backup-2026-09-05-100000-a")
        self.backup("backup-2026-09-05-100000-b")
        self.assertEqual(self.deleted_by_plan(), ["backup-2026-09-01-100000-a"])

    def test_deleted_file_record_matches_a_missing_file(self):
        self.cfg.unlink()
        self.backup("backup-2026-09-01-100000-a", manifest=self.hooks_manifest("deleted"))
        self.backup("backup-2026-09-05-100000-a")
        self.assertEqual(self.deleted_by_plan(), [])

    def test_projects_manifest_nested_records_and_ledger_record(self):
        led = self.ledger
        led.write_bytes(b'{"version": 1, "tools": {}}')
        m = {
            "kind": "nested-records",
            "projects": {
                "p": {"actions": [{"kind": "file", "path": str(self.cfg), "sha_after": self.sha}]}
            },
            "ledger": {"path": str(led), "sha_after": ap.sha(led.read_bytes())},
        }
        self.backup("backup-2026-09-01-100000-projects", manifest=m)
        self.backup("backup-2026-09-05-100000-projects")
        self.assertEqual(self.deleted_by_plan(), [])

    def test_name_still_mentioned_by_the_ledger_or_a_manifest(self):
        self.ledger.write_text(
            json.dumps({"note": "see backup-2026-09-01-100000-a"}), encoding="utf-8"
        )
        self.backup("backup-2026-09-01-100000-a")
        self.backup("backup-2026-09-01-100000-b")
        self.backup("backup-2026-09-01-100000-c")
        self.backup("backup-2026-09-05-100000-a")
        self.backup(
            "backup-2026-09-05-100000-b",
            manifest={"kind": "x", "from": "backup-2026-09-01-100000-b"},
        )
        self.backup("backup-2026-09-05-100000-c")
        self.assertEqual(self.deleted_by_plan(), ["backup-2026-09-01-100000-c"])

    def test_an_unreadable_manifest_is_kept(self):
        d = self.backup("backup-2026-09-01-100000-a")
        (d / "manifest.json").write_text("{not json", encoding="utf-8")
        self.backup("backup-2026-09-05-100000-a")
        self.assertEqual(self.deleted_by_plan(), [])


class DeleteSafely(PruneCase):
    def test_a_link_inside_a_backup_is_removed_but_its_target_is_not_followed(self):
        outside = self.home / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        d = self.backup("backup-2026-09-01-100000-a")
        skills.make_link(d / "files" / "link", outside)
        self.assertTrue(skills.is_link(d / "files" / "link"))
        self.backup("backup-2026-09-05-100000-a")
        self.assertEqual(self.deleted_by_plan(), ["backup-2026-09-01-100000-a"])
        rc, out, err = self.prune_cli("--apply")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertFalse(d.exists())
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertIn("pruned 1 backup folder(s)", out)

    def test_a_backup_that_is_itself_a_link_is_removed_without_touching_the_target(self):
        outside = self.home / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        self.backup("backup-2026-09-05-100000-a")
        skills.make_link(self.agents / "backup-2026-09-01-100000-a", outside)
        rc, out, err = self.prune_cli("--apply")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertFalse(os.path.lexists(self.agents / "backup-2026-09-01-100000-a"))
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_read_only_files_are_deleted_and_manifest_goes_last(self):
        d = self.backup("backup-2026-09-01-100000-a", manifest={"kind": "x"})
        os.chmod(d / "files" / "x.txt", 0o444)
        order = []
        real_unlink = prune._unlink
        with mock.patch.object(
            prune, "_unlink", side_effect=lambda p: (order.append(Path(p).name), real_unlink(p))[1]
        ):
            prune.remove_backup(d, self.agents)
        self.assertFalse(d.exists())
        self.assertEqual(order[-1], "manifest.json")

    def test_refuses_anything_that_is_not_a_backup_folder(self):
        other = self.agents / "skills"
        other.mkdir()
        with self.assertRaises(prune.PruneError):
            prune.remove_backup(other, self.agents)
        with self.assertRaises(prune.PruneError):
            prune.remove_backup(self.home / "backup-2020-01-01-x", self.agents)
        self.assertTrue(other.exists())


class DryRunAndScope(PruneCase):
    def test_dry_run_is_the_default_and_lists_sizes(self):
        self.backup("backup-2026-09-01-100000-a", size=2048)
        self.backup("backup-2026-09-05-100000-a")
        before = digest(self.home)
        rc, out, err = self.prune_cli()
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("would delete  backup-2026-09-01-100000-a  2.0 KB", out)
        self.assertIn("dry-run: nothing was deleted", out)
        self.assertEqual(digest(self.home), before)
        self.assertFalse(self.lock.exists())  # a dry-run does not even take the lock

    def test_nothing_old_means_nothing_deleted(self):
        self.backup("backup-2026-10-31-6a")
        self.backup("backup-2026-11-01-175432-sync")
        rc, out, _ = self.prune_cli()
        self.assertEqual(rc, 0)
        self.assertIn("0 to delete", out)
        self.assertIn("nothing to delete", out)

    def test_older_than_zero_and_negative(self):
        self.backup("backup-2026-10-31-100000-a")
        self.backup("backup-2026-11-01-100000-a")
        self.backup("backup-2026-11-02-100000-a")
        self.assertEqual(
            self.deleted_by_plan(0), ["backup-2026-10-31-100000-a", "backup-2026-11-01-100000-a"]
        )
        rc, _, err = self.prune_cli("--older-than", "-1")
        self.assertEqual(rc, 1)
        self.assertIn("0 or more", err)

    def test_apply_deletes_only_backup_folders(self):
        self.backup("backup-2026-09-01-100000-a")
        self.backup("backup-2026-09-05-100000-a")
        (self.agents / "settings.bak").write_text("bak")
        (self.state / "runner.log").write_text("log")
        (self.agents / "skills").mkdir()
        (self.agents / "archive").mkdir()
        (self.home / ".claude" / "backups").mkdir(parents=True)
        (self.home / ".claude" / "backups" / "b-2020-01-01").write_text("old")
        rc, out, err = self.prune_cli("--apply")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(self.names(), ["backup-2026-09-05-100000-a"])
        for p in (
            self.agents / "settings.bak",
            self.state / "runner.log",
            self.agents / "skills",
            self.agents / "archive",
            self.home / ".claude" / "backups" / "b-2020-01-01",
        ):
            self.assertTrue(p.exists(), p)
        self.assertFalse(self.lock.exists())  # the lock is released

    def test_apply_refuses_while_another_run_holds_the_lock(self):
        self.backup("backup-2026-09-01-100000-a")
        self.backup("backup-2026-09-05-100000-a")
        lock = locations.lock_file(self.home)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("1 1")
        rc, out, err = self.prune_cli("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("another run holds", err)
        self.assertEqual(len(self.names()), 2)

    def test_json_output(self):
        self.backup("backup-2026-09-01-100000-a")
        self.backup("backup-2026-09-05-100000-a")
        rc, out, _ = self.prune_cli("--json")
        data = json.loads(out)
        self.assertEqual(
            [(b["name"], b["action"]) for b in data["backups"]],
            [("backup-2026-09-01-100000-a", "delete"), ("backup-2026-09-05-100000-a", "keep")],
        )


class AutoPrune(WorkDirCase):
    def setUp(self):
        self.home = self.copy_fixture_home()
        self.agents = locations.backups_dir(self.home)
        self.agents.mkdir(parents=True)
        for n in ("backup-2020-01-01-100000-old", "backup-2020-02-01-100000-old"):
            (self.agents / n / "files").mkdir(parents=True)
            (self.agents / n / "files" / "x").write_text("x")

    def old(self):
        return sorted(x.name for x in self.agents.glob("backup-2020-*"))

    def test_apply_prunes_old_backups_quietly_afterwards(self):
        rc, out, err = cli("apply", "--apply", "--tool", "opencode,codex,agy", home=self.home)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(self.old(), ["backup-2020-02-01-100000-old"])  # newest of its kind stays
        self.assertIn("auto-prune: pruned 1 backup folder(s)", out)
        self.assertEqual(
            len(list(self.agents.glob("backup-20*-sync*"))), 1
        )  # the new backup of this run

    def test_no_prune_flag_skips_it(self):
        rc, out, err = cli(
            "apply", "--apply", "--no-prune", "--tool", "opencode,codex,agy", home=self.home
        )
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(len(self.old()), 2)
        self.assertNotIn("auto-prune", out)

    def test_a_dry_run_or_a_failed_apply_prunes_nothing(self):
        rc, out, _ = cli("apply", "--tool", "opencode,codex,agy", home=self.home)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.old()), 2)
        bad = self.home / "bad-source.jsonc"
        bad.write_text("{not json", encoding="utf-8")
        rc, _, _ = cli(
            "apply", "--apply", "--tool", "opencode,codex,agy", home=self.home, source=bad
        )
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.old()), 2)

    def test_quiet_apply_prints_nothing_about_it(self):
        rc, out, err = cli(
            "apply", "--apply", "--quiet", "--tool", "opencode,codex,agy", home=self.home
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.old(), ["backup-2020-02-01-100000-old"])
        self.assertNotIn("auto-prune", out)

    def test_auto_never_raises_and_warns(self):
        warnings = []
        with mock.patch.object(prune, "run_prune", side_effect=prune.PruneError("boom")):
            prune.auto(self.home, out=lambda m: None, err=warnings.append)
        self.assertEqual(len(warnings), 1)
        self.assertIn("auto-prune skipped", warnings[0])
        with mock.patch.object(prune, "run_prune", side_effect=OSError("disk")):
            prune.auto(self.home, out=lambda m: None, err=warnings.append)
        self.assertEqual(len(warnings), 2)

    def test_a_lock_held_by_someone_else_only_warns(self):
        lock = locations.lock_file(self.home)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("1 1")
        warnings = []
        prune.auto(self.home, out=lambda m: None, err=warnings.append)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len(self.old()), 2)


if __name__ == "__main__":
    unittest.main()
