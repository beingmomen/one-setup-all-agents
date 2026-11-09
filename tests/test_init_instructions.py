"""Tests for instructions import in `all-agents init` (Phase 4 M04-T2.1)."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import FakeRunner, make_env
from one_setup_all_agents.adapters import ADAPTERS, all_adapters
from one_setup_all_agents.cli import build_parser
from one_setup_all_agents.commands import init
from one_setup_all_agents.config_source import desired_all, load_source
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.plan import build_plan
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.importer import (
    InstructionsProposal,
    propose_instructions,
)
from one_setup_all_agents.model import (
    Conflict,
    Detection,
    EditFile,
    Instructions,
    Scope,
)
from one_setup_all_agents.reader import ToolReading


class InitInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = make_env(self.root)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, is_interactive: bool = False, read_line=lambda prompt: "") -> CommandContext:
        return CommandContext(
            env=self.env,
            read_line=read_line,
            is_interactive=is_interactive,
            out=self.out,
            err=self.err,
        )

    def test_equal_in_three_tools_with_crlf_and_bom_selects_core_and_adopts_all_three(self):
        readings = [
            ToolReading(
                "tool-a", "Tool A", Instructions(Path("a"), "shared 1\nshared 2\n"), {}, (), ()
            ),
            ToolReading(
                "tool-b",
                "Tool B",
                Instructions(Path("b"), "shared 1\r\nshared 2\r\n"),
                {},
                (),
                (),
            ),
            ToolReading(
                "tool-c",
                "Tool C",
                Instructions(Path("c"), "\ufeffshared 1\nshared 2\n"),
                {},
                (),
                (),
            ),
        ]
        proposal = propose_instructions(readings)
        self.assertIsNotNone(proposal)
        self.assertIsInstance(proposal, InstructionsProposal)
        self.assertEqual(proposal.core, "shared 1\nshared 2\n")
        self.assertEqual(sorted(proposal.adopt), ["tool-a", "tool-b", "tool-c"])
        self.assertEqual(proposal.skipped, [])

    def test_differ_with_from_tool_selects_chosen_core_and_respects_user_confirmation(self):
        readings = [
            ToolReading(
                "tool-a", "Tool A", Instructions(Path("a"), "instructions A\n"), {}, (), ()
            ),
            ToolReading(
                "tool-b", "Tool B", Instructions(Path("b"), "instructions B\n"), {}, (), ()
            ),
            ToolReading(
                "tool-c", "Tool C", Instructions(Path("c"), "instructions A\n"), {}, (), ()
            ),
        ]

        # 1. User says "yes" to replace other files
        proposal_yes = propose_instructions(
            readings,
            from_tool="tool-a",
            ask=lambda prompt: "y",
        )
        self.assertIsNotNone(proposal_yes)
        self.assertEqual(proposal_yes.core, "instructions A\n")
        self.assertEqual(sorted(proposal_yes.adopt), ["tool-a", "tool-b", "tool-c"])
        self.assertEqual(proposal_yes.skipped, [])

        # 2. User says "no" to replace other files
        proposal_no = propose_instructions(
            readings,
            from_tool="tool-a",
            ask=lambda prompt: "n",
        )
        self.assertIsNotNone(proposal_no)
        self.assertEqual(proposal_no.core, "instructions A\n")
        self.assertEqual(sorted(proposal_no.adopt), ["tool-a", "tool-c"])
        self.assertEqual(proposal_no.skipped, ["tool-b"])

    def test_differ_without_from_tool_in_non_interactive_refuses(self):
        readings = [
            ToolReading("tool-a", "Tool A", Instructions(Path("a"), "first text\n"), {}, (), ()),
            ToolReading("tool-b", "Tool B", Instructions(Path("b"), "second text\n"), {}, (), ()),
        ]
        with self.assertRaises(ValueError) as ctx_err:
            propose_instructions(readings, ask=None)
        self.assertIn("instructions differ between tools; pass --from TOOL", str(ctx_err.exception))

        # Command-level refusal test
        ctx = self._ctx(is_interactive=False)
        detections = [
            Detection(installed=True, tool="tool-a", label="Tool A", binary=Path("/bin/a")),
            Detection(installed=True, tool="tool-b", label="Tool B", binary=Path("/bin/b")),
        ]
        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.init.read_all", return_value=readings),
        ):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 1)
        self.assertIn("instructions differ between tools; pass --from TOOL", self.err.getvalue())
        self.assertFalse((self.env.config_dir / "instructions.md").exists())

    def test_none_exist_returns_none_and_creates_no_instructions_file(self):
        readings = [
            ToolReading("tool-a", "Tool A", None, {}, (), ()),
            ToolReading("tool-b", "Tool B", None, {}, (), ()),
        ]
        proposal = propose_instructions(readings)
        self.assertIsNone(proposal)

        ctx = self._ctx()
        detections = [
            Detection(installed=True, tool="tool-a", label="Tool A", binary=Path("/bin/a")),
        ]
        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.init.read_all", return_value=readings),
        ):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)
        self.assertIn("- Instructions: none", self.out.getvalue())
        self.assertFalse((self.env.config_dir / "instructions.md").exists())

    def test_size_limit_surfaces_as_warning_without_blocking(self):
        # Generically find an adapter whose plan_instructions produces a size conflict
        large_text = "x" * 30_000 + "\n"
        size_limited_adapter = None
        for cls in ADAPTERS:
            adapter = cls()
            ops = adapter.plan_instructions(self.env, Scope.GLOBAL, large_text, frozenset())
            if any(isinstance(op, Conflict) for op in ops):
                size_limited_adapter = adapter
                break

        self.assertIsNotNone(
            size_limited_adapter,
            "at least one registered adapter must have an instructions size limit",
        )

        reading = ToolReading(
            size_limited_adapter.id,
            size_limited_adapter.label,
            Instructions(Path("rules.md"), large_text),
            {},
            (),
            (),
        )
        proposal = propose_instructions(
            [reading],
            env=self.env,
            adapters={size_limited_adapter.id: size_limited_adapter},
        )
        self.assertIsNotNone(proposal)
        self.assertTrue(
            any("warning" in m.lower() for m in proposal.messages),
            "conflict should produce a warning message",
        )

        # Ensure init command surfaces the warning and does not block (exits 0)
        ctx = self._ctx()
        detections = [
            Detection(
                installed=True,
                tool=size_limited_adapter.id,
                label=size_limited_adapter.label,
                binary=Path("/bin/tool"),
            )
        ]
        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.init.read_all", return_value=[reading]),
        ):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)
        self.assertTrue(
            "warning" in self.err.getvalue().lower() or "truncat" in self.err.getvalue().lower()
        )

    def test_after_init_next_plan_has_no_edit_file_for_matching_adopted_file(self):
        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        self.assertIsNotNone(paths.instructions)
        content = "Line one\nLine two\n"
        paths.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths.instructions.write_bytes(content.encode("utf-8"))

        reading = ToolReading(
            adapter.id,
            adapter.label,
            Instructions(paths.instructions, content),
            {},
            (),
            (),
        )
        detection = Detection(
            installed=True,
            tool=adapter.id,
            label=adapter.label,
            binary=Path("/bin/adapter"),
        )

        ctx = self._ctx()
        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=[detection]),
            patch("one_setup_all_agents.commands.init.read_all", return_value=[reading]),
        ):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)

        # Next plan from source
        source = load_source(self.env.config_dir, [adapter.id])
        desired = desired_all(source, [adapter.id])
        plan = build_plan(self.env, [adapter], desired)
        edit_ops = [op for op in plan.executable if isinstance(op, EditFile)]
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(
            edit_ops, [], "adopted file with identical content should have no EditFile"
        )

    def test_adopted_file_differing_only_by_crlf_has_exactly_one_edit_file(self):
        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        self.assertIsNotNone(paths.instructions)
        crlf_content = "Line one\r\nLine two\r\n"
        paths.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths.instructions.write_bytes(crlf_content.encode("utf-8"))

        reading = ToolReading(
            adapter.id,
            adapter.label,
            Instructions(paths.instructions, crlf_content),
            {},
            (),
            (),
        )
        detection = Detection(
            installed=True,
            tool=adapter.id,
            label=adapter.label,
            binary=Path("/bin/adapter"),
        )

        ctx = self._ctx()
        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=[detection]),
            patch("one_setup_all_agents.commands.init.read_all", return_value=[reading]),
        ):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)

        # Next plan has 1 EditFile to normalize CRLF to LF
        source = load_source(self.env.config_dir, [adapter.id])
        desired = desired_all(source, [adapter.id])
        plan = build_plan(self.env, [adapter], desired)
        edit_ops = [op for op in plan.executable if isinstance(op, EditFile)]
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(len(edit_ops), 1)

    def test_undo_restores_tool_file_changed_after_sync_apply(self):
        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        self.assertIsNotNone(paths.instructions)
        original_bytes = b"Line one\r\nLine two\r\n"
        paths.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths.instructions.write_bytes(original_bytes)

        reading = ToolReading(
            adapter.id,
            adapter.label,
            Instructions(paths.instructions, original_bytes.decode("utf-8")),
            {},
            (),
            (),
        )
        detection = Detection(
            installed=True,
            tool=adapter.id,
            label=adapter.label,
            binary=Path("/bin/adapter"),
        )

        ctx = self._ctx()
        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=[detection]),
            patch("one_setup_all_agents.commands.init.read_all", return_value=[reading]),
        ):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)

        # Plan next sync
        source = load_source(self.env.config_dir, [adapter.id])
        desired = desired_all(source, [adapter.id])
        plan = build_plan(self.env, [adapter], desired)
        self.assertEqual(len(plan.executable), 1)

        # Apply plan
        op_id = apply_plan(self.env, plan, label="sync", run_cli=FakeRunner())
        self.assertIsNotNone(op_id)
        self.assertEqual(paths.instructions.read_bytes(), b"Line one\nLine two\n")

        # Revert operation restores original bytes
        report = revert_operation(self.env, op_id, run_cli=FakeRunner())
        self.assertEqual(report.op_id, op_id)
        self.assertEqual(paths.instructions.read_bytes(), original_bytes)

    def test_normalize_instructions_text_enforces_exactly_one_trailing_newline(self):
        from one_setup_all_agents.importer import _normalize_instructions_text

        self.assertEqual(
            _normalize_instructions_text("a\n\n"),
            _normalize_instructions_text("a\n"),
        )
        self.assertEqual(_normalize_instructions_text("a\n\n"), "a\n")

    def test_non_utf8_instructions_skipped_and_reported_and_not_adopted(self):
        bad_path = self.home / "bad.md"
        valid_path = self.home / "valid.md"
        readings = [
            ToolReading(
                "claude", "Claude Code", Instructions(bad_path, "bad\ufffdtext\x00"), {}, (), ()
            ),
            ToolReading("codex", "Codex", Instructions(valid_path, "good text\n"), {}, (), ()),
        ]
        proposal = propose_instructions(readings)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.core, "good text\n")
        self.assertEqual(proposal.adopt, ["codex"])
        self.assertNotIn("claude", proposal.adopt)
        self.assertTrue(any("claude" in m and str(bad_path) in m for m in proposal.messages))

    def test_dry_run_conflict_check_with_crlf_passes_owned_and_does_not_warn_left_alone(self):
        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        paths.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths.instructions.write_bytes(b"hello world\r\n")
        readings = [
            ToolReading(
                adapter.id,
                adapter.label,
                Instructions(paths.instructions, "hello world\r\n"),
                {},
                (),
                (),
            )
        ]
        adapters_map = {adapter.id: adapter}
        proposal = propose_instructions(readings, env=self.env, adapters=adapters_map)
        self.assertIsNotNone(proposal)
        self.assertIn(adapter.id, proposal.adopt)
        self.assertFalse(any("left alone" in m for m in proposal.messages))


if __name__ == "__main__":
    unittest.main()
