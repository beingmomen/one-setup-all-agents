"""What is still a stub in `engine/`: only the user-facing `undo` command (Phase 5).

Everything else in the engine is implemented and has its own tests (`test_engine_*.py`). The
format constants are checked here because other modules and the ADRs name them.
"""

import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.engine import journal, undo
from one_setup_all_agents.model import Env

ENV = Env(
    home=Path("unused"),
    platform="linux",
    environ={},
    config_dir=Path("unused") / "config",
    state_dir=Path("unused") / "state",
)


class EngineStubTests(unittest.TestCase):
    def test_the_undo_command_is_still_a_stub_and_fails_loudly(self):
        with self.assertRaises(NotImplementedError):
            undo.undo(ENV)

    def test_the_stub_points_at_the_revert_it_will_call(self):
        from one_setup_all_agents.engine import revert

        self.assertTrue(callable(revert.revert_operation))
        self.assertIn("revert_operation", undo.__doc__)

    def test_journal_format_constants(self):
        self.assertEqual(journal.JOURNAL_FORMAT, "one-setup-all-agents/journal")
        self.assertEqual(journal.JOURNAL_VERSION, 1)
        self.assertEqual(journal.LEDGER_FORMAT, "one-setup-all-agents/ledger")
        self.assertEqual(journal.LEDGER_VERSION, 1)
        self.assertEqual(journal.STATUSES, ("open", "done", "failed", "undone"))


if __name__ == "__main__":
    unittest.main()
