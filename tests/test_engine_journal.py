"""engine/journal.py: the state layout, operation ids, statuses, entries and the ledger (ADR 0005)."""

import json
import re
import unittest
from datetime import datetime, timezone
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import POSIX, EngineCase
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine.errors import JournalError

ID_FORMAT = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")
CLOCK = datetime(2026, 11, 6, 10, 15, 0, tzinfo=timezone.utc)


class JournalTests(EngineCase):
    def test_begin_creates_the_layout_of_adr_0005(self):
        op = journal.begin(self.state, "first", now=CLOCK)
        self.assertRegex(op.id, ID_FORMAT)
        self.assertTrue(op.id.startswith("20261106T101500Z-"))
        self.assertTrue((self.state / "ledger.json").is_file())
        self.assertTrue((self.state / "ops" / op.id / "journal.json").is_file())
        doc = json.loads((self.state / "ops" / op.id / "journal.json").read_text("utf-8"))
        self.assertEqual(doc["format"], "one-setup-all-agents/journal")
        self.assertEqual(doc["version"], 1)
        self.assertEqual((doc["id"], doc["label"], doc["status"]), (op.id, "first", "open"))
        self.assertEqual(doc["entries"], [])
        self.assertEqual(doc["created"], "2026-11-06T10:15:00Z")

    def test_ledger_format_and_the_operation_index(self):
        op = journal.begin(self.state, "first", now=CLOCK)
        ledger = json.loads((self.state / "ledger.json").read_text("utf-8"))
        self.assertEqual(ledger["format"], "one-setup-all-agents/ledger")
        self.assertEqual(ledger["version"], 1)
        self.assertEqual(ledger["owned"], {})
        self.assertEqual(
            ledger["operations"],
            [{"id": op.id, "label": "first", "created": "2026-11-06T10:15:00Z", "status": "open"}],
        )

    def test_ids_are_unique_even_with_a_frozen_clock(self):
        ids = {journal.begin(self.state, "x", now=CLOCK).id for _ in range(40)}
        self.assertEqual(len(ids), 40)

    def test_ids_sort_by_time_and_the_listing_is_newest_first(self):
        later = datetime(2026, 11, 7, 8, 0, 0, tzinfo=timezone.utc)
        a = journal.begin(self.state, "a", now=CLOCK)
        b = journal.begin(self.state, "b", now=later)
        self.assertLess(a.id, b.id)
        self.assertEqual([o["id"] for o in journal.list_operations(self.state)], [b.id, a.id])

    def test_status_changes_reach_the_journal_and_the_index(self):
        op = journal.begin(self.state, "x")
        for status in ("done", "undone"):
            op.set_status(status)
            self.assertEqual(journal.load(self.state, op.id).status, status)
            self.assertEqual(journal.list_operations(self.state)[0]["status"], status)
        with self.assertRaises(JournalError):
            op.set_status("exploded")

    def test_an_entry_is_on_disk_before_append_returns(self):
        op = journal.begin(self.state, "x")
        entry = {
            "seq": 1,
            "type": "edit_file",
            "status": "planned",
            "before": {"state": "absent"},
            "after": {"state": "file", "sha256": "abc", "mode": None},
        }
        op.append(entry)
        on_disk = journal.load(self.state, op.id).entries
        self.assertEqual(on_disk, [entry])
        entry["status"] = "done"
        op.save()
        self.assertEqual(journal.load(self.state, op.id).entries[0]["status"], "done")

    def test_every_write_goes_through_the_atomic_write(self):
        with mock.patch.object(journal, "atomic_write", wraps=journal.atomic_write) as writer:
            op = journal.begin(self.state, "x")
            op.set_status("done")
        written = {call.args[0].name for call in writer.call_args_list}
        self.assertEqual(written, {"journal.json", "ledger.json"})
        self.assertEqual([p.name for p in self.state.rglob("*.tmp")], [])

    def test_files_are_private_on_posix(self):
        if not POSIX:
            self.skipTest("permission bits are a POSIX concept")
        op = journal.begin(self.state, "x")
        op.set_status("done")
        for directory in (self.state, self.state / "ops", self.state / "ops" / op.id):
            self.assertEqual(self.mode(directory), 0o700, directory.name)
        for name in (self.state / "ledger.json", self.state / "ops" / op.id / "journal.json"):
            self.assertEqual(self.mode(name), 0o600, name.name)

    def test_a_file_in_another_format_is_refused(self):
        journal.begin(self.state, "x")
        (self.state / "ledger.json").write_text('{"format": "something-else"}', "utf-8")
        with self.assertRaises(JournalError):
            journal.read_ledger(self.state)
        (self.state / "ledger.json").write_text("not json", "utf-8")
        with self.assertRaises(JournalError):
            journal.owned(self.state)

    def test_no_ledger_means_nothing_owned(self):
        self.assertEqual(journal.owned(self.state), {})
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), frozenset())
        self.assertFalse(self.state.exists(), "reading must not create the state directory")


class OwnedTests(EngineCase):
    def test_set_and_read_owned_per_tool_and_kind(self):
        journal.set_owned(self.state, "example", "mcp", ["b", "a"])
        journal.set_owned(self.state, "example", "skills", ["s"])
        journal.set_owned(self.state, "other", "mcp", ["z"])
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"a", "b"})
        self.assertEqual(journal.owned_for(self.state, "other", "mcp"), {"z"})
        self.assertEqual(journal.owned_for(self.state, "other", "skills"), frozenset())
        self.assertEqual(
            journal.owned(self.state),
            {"example": {"mcp": ["a", "b"], "skills": ["s"]}, "other": {"mcp": ["z"]}},
        )
        journal.set_owned(self.state, "example", "mcp", [])
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), frozenset())
        self.assertEqual(journal.owned_for(self.state, "example", "skills"), {"s"})

    def test_reverting_a_delta_is_exact_and_safe_to_repeat(self):
        journal.set_owned(self.state, "example", "mcp", ["kept", "added"])
        delta = {
            "added": [["example", "mcp", "added"]],
            "removed": [["example", "mcp", "gone"]],
        }
        journal.revert_owned(self.state, delta)
        journal.revert_owned(self.state, delta)
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"kept", "gone"})
        journal.revert_owned(self.state, None)

    def test_unfinished_operations_are_found_by_scanning_the_folders(self):
        done = journal.begin(self.state, "done")
        done.set_status("done")
        crashed = journal.begin(self.state, "crashed")
        half = journal.begin(self.state, "half-reverted")
        half.set_status("failed", pending_revert=True)
        clean = journal.begin(self.state, "failed-and-reverted")
        clean.set_status("failed", pending_revert=False)
        self.assertEqual(set(journal.find_unfinished(self.state)), {crashed.id, half.id})
        self.assertEqual(journal.find_unfinished(self.root / "nowhere"), [])


if __name__ == "__main__":
    unittest.main()
