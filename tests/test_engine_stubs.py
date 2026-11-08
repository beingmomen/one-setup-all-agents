"""What is tested for engine contracts and constants."""

import inspect
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


class EngineContractTests(unittest.TestCase):
    def test_undo_function_has_no_op_id_parameter(self):
        sig = inspect.signature(undo.undo)
        self.assertNotIn("op_id", sig.parameters)
        self.assertIn("force", sig.parameters)
        self.assertIn("run_cli", sig.parameters)
        self.assertIn("lock_timeout", sig.parameters)

    def test_journal_format_constants(self):
        self.assertEqual(journal.JOURNAL_FORMAT, "one-setup-all-agents/journal")
        self.assertEqual(journal.JOURNAL_VERSION, 1)
        self.assertEqual(journal.LEDGER_FORMAT, "one-setup-all-agents/ledger")
        self.assertEqual(journal.LEDGER_VERSION, 1)
        self.assertEqual(journal.STATUSES, ("open", "done", "failed", "undone"))


if __name__ == "__main__":
    unittest.main()
