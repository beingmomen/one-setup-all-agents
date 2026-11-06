"""Engine entry points are documented stubs in Phase 0 and fail loudly, never silently."""

import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents import paths
from one_setup_all_agents.engine import apply, atomic, backup, journal, lock, plan, undo
from one_setup_all_agents.model import Env, Plan

ENV = Env(
    home=Path("unused"),
    platform="linux",
    environ={},
    config_dir=Path("unused") / "config",
    state_dir=Path("unused") / "state",
)


class EngineStubTests(unittest.TestCase):
    def test_each_stub_raises_not_implemented(self):
        state = Path("unused")
        calls = (
            lambda: plan.build_plan(ENV, []),
            lambda: plan.render_plan(Plan()),
            lambda: apply.apply_plan(ENV, Plan(), label="x"),
            lambda: atomic.atomic_write(state / "f", b""),
            lambda: backup.capture(state, "op", state / "f"),
            lambda: backup.restore(state, {}),
            lambda: journal.begin(state, "x"),
            lambda: journal.record(state, "op", {}),
            lambda: journal.list_operations(state),
            lambda: journal.owned(state),
            lambda: undo.undo(ENV),
            lambda: paths.config_dir({}, "linux", state),
            lambda: paths.state_dir({}, "linux", state),
            lambda: paths.build_env(),
        )
        for call in calls:
            with self.assertRaises(NotImplementedError):
                call()

    def test_lock_stub_raises_on_enter(self):
        with self.assertRaises(NotImplementedError):
            with lock.engine_lock(Path("unused")):
                pass

    def test_journal_format_constants(self):
        self.assertEqual(journal.JOURNAL_FORMAT, "one-setup-all-agents/journal")
        self.assertEqual(journal.JOURNAL_VERSION, 1)

    def test_empty_plan_and_unsupported_only_plan_are_empty(self):
        from one_setup_all_agents.model import Kind, Unsupported

        self.assertTrue(Plan().is_empty)
        self.assertTrue(Plan([Unsupported("x", Kind.HOOKS, "r")]).is_empty)


if __name__ == "__main__":
    unittest.main()
