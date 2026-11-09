"""Tests for the `all-agents init` command (Phase 4 M04-T2)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import FakeRunner, make_env
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import main
from one_setup_all_agents.config_source import SOURCE_CONFIG_NAME, load_source
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import Detection, Kind, Scope
from one_setup_all_agents.reader import Problem, ToolReading

PLANTED_SECRET = "sk-" + "a1b2c3d4" * 6


class CmdInitTests(unittest.TestCase):
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

    def _tree_digest(self, root: Path) -> dict[str, str]:
        digest: dict[str, str] = {}
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if p.is_file():
                digest[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            elif p.is_dir():
                digest[rel] = "<dir>"
            elif p.is_symlink():
                digest[rel] = f"<symlink:{os.readlink(p)}>"
        return digest

    def _installed_detection(self) -> Detection:
        return Detection(
            installed=True,
            tool="example-tool",
            label="Example Tool",
            binary=Path("/bin/example-tool"),
        )

    def test_no_tool_installed_exits_zero_and_writes_nothing(self):
        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=[]):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("No supported tool was found. Nothing to import.", self.out.getvalue())
        self.assertFalse(self.env.config_dir.exists())
        self.assertFalse(self.env.state_dir.exists())

    def test_already_initialised_exits_one_and_writes_nothing(self):
        ctx = self._ctx()
        self.env.config_dir.mkdir(parents=True)
        config_file = self.env.config_dir / SOURCE_CONFIG_NAME
        config_file.write_text("dummy-content", encoding="utf-8")

        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[self._installed_detection()],
        ):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("already initialised; use `all-agents sync`", self.err.getvalue())
        self.assertEqual(config_file.read_text(encoding="utf-8"), "dummy-content")

    def test_unknown_from_tool_exits_one(self):
        ctx = self._ctx()
        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[self._installed_detection()],
        ):
            code = main(["init", "--from", "unknown-tool-xyz"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("unknown tool 'unknown-tool-xyz'", self.err.getvalue())

    def test_non_interactive_without_yes_exits_one_and_writes_nothing(self):
        ctx = self._ctx(is_interactive=False)
        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[self._installed_detection()],
        ):
            code = main(["init"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("not asked: not interactive; pass --yes", self.out.getvalue())
        self.assertFalse((self.env.config_dir / SOURCE_CONFIG_NAME).exists())
        self.assertFalse(self.env.state_dir.exists())

    def test_dry_run_exits_zero_and_writes_nothing(self):
        (self.home / "pre_existing.txt").write_text("content", encoding="utf-8")
        before_digest = self._tree_digest(self.home)
        ctx = self._ctx()
        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[self._installed_detection()],
        ):
            code = main(["init", "--dry-run"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("Import proposal:", self.out.getvalue())
        self.assertEqual(self._tree_digest(self.home), before_digest)
        self.assertFalse(self.env.config_dir.exists())
        self.assertFalse(self.env.state_dir.exists())

    def test_secret_planted_in_problem_does_not_leak_into_output(self):
        agy_adapter = [a for a in all_adapters() if a.id == "agy"][0]
        agy_paths = agy_adapter.paths(self.env, Scope.GLOBAL)
        agy_paths.mcp.parent.mkdir(parents=True, exist_ok=True)
        agy_paths.mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "sec-env": {
                            "command": "cmd1",
                            "env": {"API_KEY": PLANTED_SECRET},
                        },
                        "sec-header": {
                            "serverUrl": "https://example.com/sse",
                            "headers": {"Authorization": f"Bearer {PLANTED_SECRET}"},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        agy_detection = Detection(
            installed=True,
            tool=agy_adapter.id,
            label=agy_adapter.label,
            binary=Path("/bin/agy"),
        )
        ctx = self._ctx()
        detection = self._installed_detection()
        problem = Problem(
            tool="example-tool",
            kind=Kind.MCP,
            path=None,
            message=f"failed authentication with token {PLANTED_SECRET}",
        )
        from one_setup_all_agents.reader import read_all

        real_readings = list(read_all(self.env, [agy_adapter], [agy_detection]))
        reading = ToolReading(
            tool="example-tool",
            label="Example Tool",
            instructions=None,
            mcp={},
            skills=(),
            problems=(problem,),
        )
        with (
            patch(
                "one_setup_all_agents.commands.init.detect_all",
                return_value=[agy_detection, detection],
            ),
            patch(
                "one_setup_all_agents.commands.init.read_all",
                return_value=real_readings + [reading],
            ),
        ):
            code = main(["init", "--dry-run"], context=ctx)
        self.assertEqual(code, 0)
        self.assertNotIn(PLANTED_SECRET, self.out.getvalue())
        self.assertNotIn(PLANTED_SECRET, self.err.getvalue())

    def test_successful_init_creates_config_and_journal_operation(self):
        ctx = self._ctx()
        detection = self._installed_detection()
        with patch(
            "one_setup_all_agents.commands.init.detect_all",
            return_value=[detection],
        ):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)
        config_file = self.env.config_dir / SOURCE_CONFIG_NAME
        self.assertTrue(config_file.exists())
        self.assertIn("version = 1", config_file.read_text(encoding="utf-8"))
        self.assertIn("[tools.example-tool]", config_file.read_text(encoding="utf-8"))
        operations = journal.list_operations(self.env.state_dir)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["label"], "init")
        self.assertEqual(operations[0]["status"], "done")

    def test_init_four_tools_snapshot_entries_and_revert_restores_home_byte_for_byte(self):
        adapters = all_adapters()
        detections = []
        for a in adapters:
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                paths.instructions.parent.mkdir(parents=True, exist_ok=True)
                paths.instructions.write_bytes(b"# Shared instructions\n")
            if paths.mcp:
                paths.mcp.parent.mkdir(parents=True, exist_ok=True)
                paths.mcp.write_bytes(b"{}" if "json" in paths.mcp.name else b"")
            for snap_file in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap_file.exists():
                    snap_file.parent.mkdir(parents=True, exist_ok=True)
                    snap_file.write_bytes(b"{}" if "json" in snap_file.name else b"")
            detections.append(
                Detection(installed=True, tool=a.id, label=a.label, binary=Path(f"/bin/{a.id}"))
            )

        before_listing = {
            p.relative_to(self.home).as_posix(): p.read_bytes()
            for p in self.home.rglob("*")
            if p.is_file()
        }

        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0)

        # 1. ops/ has exactly 1 operation done with label init
        operations = journal.list_operations(self.env.state_dir)
        self.assertEqual(len(operations), 1)
        op_id = operations[0]["id"]
        self.assertEqual(operations[0]["label"], "init")
        self.assertEqual(operations[0]["status"], "done")

        # 2. Entries include snapshot no-op entries
        entries = journal.load(self.env.state_dir, op_id).entries
        snap_entries = [e for e in entries if e.get("description") == "snapshot (no change)"]
        self.assertTrue(len(snap_entries) > 0)

        # 3. revert_operation restores temp home byte-for-byte, removes source folder, empties owned
        report = revert_operation(self.env, op_id, run_cli=FakeRunner())
        self.assertEqual(report.op_id, op_id)
        self.assertFalse((self.env.config_dir / SOURCE_CONFIG_NAME).exists())
        self.assertEqual(journal.owned_items(self.env.state_dir), set())

        after_listing = {
            p.relative_to(self.home).as_posix(): p.read_bytes()
            for p in self.home.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before_listing, after_listing)

    def test_subprocess_dry_run_in_isolated_temp_env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_bootstrap.SRC), *filter(None, [env.get("PYTHONPATH")])]
        )
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        result = subprocess.run(
            [sys.executable, "-m", "one_setup_all_agents", "init", "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            "No supported tool was found" in result.stdout or "Import proposal:" in result.stdout
        )

    def test_init_over_fixture_home_excludes_literal_secrets_and_loads_source(self):
        # 1. Copy fixture home
        fixtures_home = Path(__file__).resolve().parent / "fixtures" / "home"
        shutil.copytree(fixtures_home, self.home, dirs_exist_ok=True)

        detections = [
            Detection(installed=True, tool=a.id, label=a.label, binary=Path(f"/bin/{a.id}"))
            for a in all_adapters()
        ]

        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            code = main(["init", "--yes"], context=ctx)
        self.assertEqual(code, 0, f"init failed: {self.err.getvalue()}")

        # Assert no fake secrets appear in any file under config_dir
        fixture_secrets = [
            "FAKE-ARG-SECRET-bbb222",
            "FAKE-URL-SECRET-ccc333",
            "FAKE-ENV-SECRET-aaa111",
        ]
        for p in self.env.config_dir.rglob("*"):
            if p.is_file():
                file_text = p.read_text(encoding="utf-8")
                for s in fixture_secrets:
                    self.assertNotIn(s, file_text, f"secret {s} found in {p}")

        # Assert load_source on config_dir succeeds
        loaded = load_source(self.env.config_dir, [a.id for a in all_adapters()])
        self.assertIsNotNone(loaded)

        # Assert summary names secret-args without values
        out_text = self.out.getvalue() + self.err.getvalue()
        self.assertIn("secret-args", out_text)
        for s in fixture_secrets:
            self.assertNotIn(s, out_text)

        # Assert status and sync --dry-run exit 0 or 2, never 1
        out_status = io.StringIO()
        err_status = io.StringIO()
        ctx_status = CommandContext(
            env=self.env,
            read_line=lambda p: "",
            is_interactive=False,
            out=out_status,
            err=err_status,
        )
        with patch("one_setup_all_agents.commands.status.detect_all", return_value=detections):
            code_status = main(["status"], context=ctx_status)
        self.assertIn(
            code_status, (0, 2), f"status failed with code {code_status}: {err_status.getvalue()}"
        )

        out_sync = io.StringIO()
        err_sync = io.StringIO()
        ctx_sync = CommandContext(
            env=self.env,
            read_line=lambda p: "",
            is_interactive=False,
            out=out_sync,
            err=err_sync,
        )
        with patch("one_setup_all_agents.commands.sync.detect_all", return_value=detections):
            code_sync = main(["sync", "--dry-run"], context=ctx_sync)
        self.assertIn(
            code_sync, (0, 2, 3), f"sync failed with code {code_sync}: {err_sync.getvalue()}"
        )
        self.assertNotEqual(code_sync, 1, f"sync failed with source error: {err_sync.getvalue()}")

    def test_init_passes_probe_to_detect_all(self):
        ctx = self._ctx()
        ctx.probe = lambda argv: "dummy-version"
        with patch("one_setup_all_agents.commands.init.detect_all") as mock_detect:
            mock_detect.return_value = []
            from one_setup_all_agents.cli import build_parser
            from one_setup_all_agents.commands import init

            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            init.run(args, ctx)
            mock_detect.assert_called_once_with(ctx.env, probe=ctx.probe)

    def test_post_init_warning_skips_link_relink_of_adopted_skills(self):
        from one_setup_all_agents.commands import init
        from one_setup_all_agents.model import ItemRef, Link, Plan

        ctx = self._ctx()
        detection = self._installed_detection()
        adopted = [ItemRef(detection.tool, Kind.SKILLS, "skill-a")]
        link_op = Link(
            link=Path("/link/a"),
            target=Path("/target/a"),
            description="link skill",
            claims=(ItemRef(detection.tool, Kind.SKILLS, "skill-a"),),
        )
        ctx.env.config_dir.mkdir(parents=True, exist_ok=True)
        (ctx.env.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        with (
            patch("one_setup_all_agents.commands.init.load_source"),
            patch("one_setup_all_agents.commands.init.build_plan", return_value=Plan((link_op,))),
            patch("one_setup_all_agents.commands.init.all_adapters", return_value=[]),
        ):
            rc = init._check_adopted_replan(ctx, [detection], adopted)
            self.assertEqual(rc, 0)
            self.assertNotIn("still requires changes", ctx.err.getvalue())

    def test_post_init_replan_surfaces_unexpected_error_with_warning_and_exit_one(self):
        from one_setup_all_agents.commands import init
        from one_setup_all_agents.model import ItemRef

        ctx = self._ctx()
        detection = self._installed_detection()
        adopted = [ItemRef(detection.tool, Kind.SKILLS, "skill-a")]
        ctx.env.config_dir.mkdir(parents=True, exist_ok=True)
        (ctx.env.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        with (
            patch(
                "one_setup_all_agents.commands.init.load_source",
                side_effect=RuntimeError("unexpected boom"),
            ),
            patch("one_setup_all_agents.commands.init.all_adapters", return_value=[]),
        ):
            rc = init._check_adopted_replan(ctx, [detection], adopted)
            self.assertEqual(rc, 1)
            self.assertIn("unexpected boom", ctx.err.getvalue())

    def test_snapshot_retry_aborts_when_retried_proposal_differs(self):
        from one_setup_all_agents.cli import build_parser
        from one_setup_all_agents.commands import init
        from one_setup_all_agents.engine.errors import BackupError

        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        paths.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths.instructions.write_text("# Initial\n", encoding="utf-8")
        detection = Detection(
            installed=True, tool=adapter.id, label=adapter.label, binary=Path("/bin/adapter")
        )

        ctx = self._ctx()
        call_count = [0]
        orig_apply = init.apply_plan

        def fake_apply(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                paths.instructions.write_text("# Changed bytes\n", encoding="utf-8")
                raise BackupError("backup failed: permission denied")
            return orig_apply(*args, **kwargs)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=[detection]),
            patch("one_setup_all_agents.commands.init.apply_plan", side_effect=fake_apply),
        ):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)

        self.assertEqual(code, 1)
        self.assertIn("files changed during init", ctx.err.getvalue())
        self.assertFalse((self.env.config_dir / "instructions.md").exists())

    def test_snapshot_retry_succeeds_when_retried_proposal_matches(self):
        from one_setup_all_agents.cli import build_parser
        from one_setup_all_agents.commands import init
        from one_setup_all_agents.engine.errors import BackupError

        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        paths.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths.instructions.write_text("# Initial\n", encoding="utf-8")
        detection = Detection(
            installed=True, tool=adapter.id, label=adapter.label, binary=Path("/bin/adapter")
        )

        ctx = self._ctx()
        call_count = [0]
        orig_apply = init.apply_plan

        def fake_apply(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise BackupError("transient backup error")
            return orig_apply(*args, **kwargs)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=[detection]),
            patch("one_setup_all_agents.commands.init.apply_plan", side_effect=fake_apply),
        ):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)

        self.assertEqual(code, 0)
        self.assertEqual(call_count[0], 2)
        inst_file = self.env.config_dir / "instructions.md"
        self.assertTrue(inst_file.is_file())
        self.assertEqual(inst_file.read_text(encoding="utf-8"), "# Initial\n")

    def test_init_interactive_different_instructions_asks_each_question_once(self):
        from one_setup_all_agents.cli import build_parser
        from one_setup_all_agents.commands import init
        from one_setup_all_agents.model import Instructions
        from one_setup_all_agents.reader import ToolReading

        adapter_a = all_adapters()[0]
        adapter_b = all_adapters()[1]
        detection_a = Detection(
            installed=True, tool=adapter_a.id, label=adapter_a.label, binary=Path("/bin/a")
        )
        detection_b = Detection(
            installed=True, tool=adapter_b.id, label=adapter_b.label, binary=Path("/bin/b")
        )
        reading_a = ToolReading(
            adapter_a.id,
            adapter_a.label,
            Instructions(Path("a"), "# Instructions A\n"),
            {},
            (),
            (),
        )
        reading_b = ToolReading(
            adapter_b.id,
            adapter_b.label,
            Instructions(Path("b"), "# Instructions B\n"),
            {},
            (),
            (),
        )

        prompts_asked: list[str] = []

        answers = {"Choose": "1", "replace": "y", "Proceed": "y"}

        def counting_read_line(prompt: str) -> str:
            prompts_asked.append(prompt)
            for k, v in answers.items():
                if k in prompt:
                    return v
            raise EOFError(f"unexpected prompt #{len(prompts_asked)}: {prompt}")

        ctx = self._ctx(is_interactive=True, read_line=counting_read_line)

        with (
            patch(
                "one_setup_all_agents.commands.init.detect_all",
                return_value=[detection_a, detection_b],
            ),
            patch(
                "one_setup_all_agents.commands.init.read_all",
                return_value=[reading_a, reading_b],
            ),
        ):
            parser = build_parser()
            args = parser.parse_args(["init"])
            code = init.run(args, ctx)

        self.assertEqual(code, 0)
        self.assertEqual(len(prompts_asked), 3)
        inst_file = self.env.config_dir / "instructions.md"
        self.assertTrue(inst_file.is_file())
        self.assertEqual(inst_file.read_text(encoding="utf-8"), "# Instructions A\n")

    def test_init_interactive_different_instructions_replace_yes_adopts_and_sync_replaces(self):
        import json

        adapter_a = [a for a in all_adapters() if a.id == "claude"][0]
        adapter_b = [a for a in all_adapters() if a.id == "codex"][0]

        paths_a = adapter_a.paths(self.env, Scope.GLOBAL)
        paths_a.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_a.instructions.write_text("# Instructions A\n", encoding="utf-8")

        paths_b = adapter_b.paths(self.env, Scope.GLOBAL)
        paths_b.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_b.instructions.write_text("# Instructions B\n", encoding="utf-8")

        detection_a = Detection(
            installed=True, tool=adapter_a.id, label=adapter_a.label, binary=Path("/bin/claude")
        )
        detection_b = Detection(
            installed=True, tool=adapter_b.id, label=adapter_b.label, binary=Path("/bin/codex")
        )
        detections = [detection_a, detection_b]

        prompts_asked: list[str] = []
        answers = {"Choose": "2", "replace": "y", "Proceed": "y"}

        def counting_read_line(prompt: str) -> str:
            prompts_asked.append(prompt)
            for k, v in answers.items():
                if k in prompt:
                    return v
            raise EOFError(f"unexpected prompt #{len(prompts_asked)}: {prompt}")

        ctx = self._ctx(is_interactive=True, read_line=counting_read_line)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.status.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.sync.detect_all", return_value=detections),
        ):
            code_init = main(["init"], context=ctx)
            self.assertEqual(code_init, 0)
            self.assertEqual(len(prompts_asked), 3)

            # tool B was chosen, instructions.md has B
            inst_file = self.env.config_dir / "instructions.md"
            self.assertEqual(inst_file.read_text(encoding="utf-8"), "# Instructions B\n")

            # run sync: tool A's file is replaced with shared text
            out_sync = io.StringIO()
            err_sync = io.StringIO()
            ctx_sync = CommandContext(
                env=self.env,
                read_line=lambda p: "",
                is_interactive=False,
                out=out_sync,
                err=err_sync,
            )
            code_sync = main(["sync", "--yes"], context=ctx_sync)
            self.assertEqual(code_sync, 0, f"sync failed: {err_sync.getvalue()}")
            self.assertEqual(paths_a.instructions.read_text(encoding="utf-8"), "# Instructions B\n")

            # run status: no conflict
            out_status = io.StringIO()
            err_status = io.StringIO()
            ctx_status = CommandContext(
                env=self.env,
                read_line=lambda p: "",
                is_interactive=False,
                out=out_status,
                err=err_status,
            )
            code_status = main(["status", "--json"], context=ctx_status)
            self.assertEqual(code_status, 0, f"status failed: {err_status.getvalue()}")
            data = json.loads(out_status.getvalue())
            for tool in data["tools"]:
                self.assertNotEqual(tool["kinds"]["instructions"]["state"], "conflict")

    def test_init_interactive_different_instructions_replace_no_leaves_conflict(self):
        import json

        adapter_a = [a for a in all_adapters() if a.id == "claude"][0]
        adapter_b = [a for a in all_adapters() if a.id == "codex"][0]

        paths_a = adapter_a.paths(self.env, Scope.GLOBAL)
        paths_a.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_a.instructions.write_text("# Instructions A\n", encoding="utf-8")

        paths_b = adapter_b.paths(self.env, Scope.GLOBAL)
        paths_b.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_b.instructions.write_text("# Instructions B\n", encoding="utf-8")

        detection_a = Detection(
            installed=True, tool=adapter_a.id, label=adapter_a.label, binary=Path("/bin/claude")
        )
        detection_b = Detection(
            installed=True, tool=adapter_b.id, label=adapter_b.label, binary=Path("/bin/codex")
        )
        detections = [detection_a, detection_b]

        prompts_asked: list[str] = []
        answers = {"Choose": "2", "replace": "n", "Proceed": "y"}

        def counting_read_line(prompt: str) -> str:
            prompts_asked.append(prompt)
            for k, v in answers.items():
                if k in prompt:
                    return v
            raise EOFError(f"unexpected prompt #{len(prompts_asked)}: {prompt}")

        ctx = self._ctx(is_interactive=True, read_line=counting_read_line)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.status.detect_all", return_value=detections),
        ):
            code_init = main(["init"], context=ctx)
            self.assertEqual(code_init, 0)
            self.assertEqual(len(prompts_asked), 3)

            # run status: tool A has conflict
            out_status = io.StringIO()
            err_status = io.StringIO()
            ctx_status = CommandContext(
                env=self.env,
                read_line=lambda p: "",
                is_interactive=False,
                out=out_status,
                err=err_status,
            )
            code_status = main(["status", "--json"], context=ctx_status)
            self.assertEqual(code_status, 2)
            data = json.loads(out_status.getvalue())
            tool_a_data = [t for t in data["tools"] if t["id"] == adapter_a.id][0]
            self.assertEqual(tool_a_data["kinds"]["instructions"]["state"], "conflict")

    def test_init_snapshot_retry_preserves_interactive_choices_without_reasking(self):
        from one_setup_all_agents.commands import init
        from one_setup_all_agents.engine.errors import BackupError

        adapter_a = [a for a in all_adapters() if a.id == "claude"][0]
        adapter_b = [a for a in all_adapters() if a.id == "codex"][0]

        paths_a = adapter_a.paths(self.env, Scope.GLOBAL)
        paths_a.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_a.instructions.write_text("# Instructions A\n", encoding="utf-8")

        paths_b = adapter_b.paths(self.env, Scope.GLOBAL)
        paths_b.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_b.instructions.write_text("# Instructions B\n", encoding="utf-8")

        detection_a = Detection(
            installed=True, tool=adapter_a.id, label=adapter_a.label, binary=Path("/bin/claude")
        )
        detection_b = Detection(
            installed=True, tool=adapter_b.id, label=adapter_b.label, binary=Path("/bin/codex")
        )
        detections = [detection_a, detection_b]

        prompts_asked: list[str] = []
        answers = {"Choose": "2", "replace": "y", "Proceed": "y"}

        def counting_read_line(prompt: str) -> str:
            prompts_asked.append(prompt)
            for k, v in answers.items():
                if k in prompt:
                    return v
            raise EOFError(f"unexpected prompt #{len(prompts_asked)}: {prompt}")

        ctx = self._ctx(is_interactive=True, read_line=counting_read_line)

        call_count = [0]
        orig_apply = init.apply_plan

        def fake_apply(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise BackupError("transient backup failure")
            return orig_apply(*args, **kwargs)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.init.apply_plan", side_effect=fake_apply),
        ):
            code_init = main(["init"], context=ctx)
            self.assertEqual(code_init, 0, f"init failed: {ctx.err.getvalue()}")
            self.assertEqual(call_count[0], 2)
            self.assertEqual(len(prompts_asked), 3)

    def test_init_yes_never_replaces_differing_instructions_and_advises_adoption(self):
        import json

        adapter_a = [a for a in all_adapters() if a.id == "claude"][0]
        adapter_b = [a for a in all_adapters() if a.id == "codex"][0]

        paths_a = adapter_a.paths(self.env, Scope.GLOBAL)
        paths_a.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_a.instructions.write_text("# Instructions A\n", encoding="utf-8")

        paths_b = adapter_b.paths(self.env, Scope.GLOBAL)
        paths_b.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_b.instructions.write_text("# Instructions B\n", encoding="utf-8")

        detection_a = Detection(
            installed=True, tool=adapter_a.id, label=adapter_a.label, binary=Path("/bin/claude")
        )
        detection_b = Detection(
            installed=True, tool=adapter_b.id, label=adapter_b.label, binary=Path("/bin/codex")
        )
        detections = [detection_a, detection_b]

        ctx = self._ctx(is_interactive=False)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.status.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.sync.detect_all", return_value=detections),
        ):
            code_init = main(["init", "--yes", "--from", adapter_b.id], context=ctx)
            self.assertEqual(code_init, 0, f"init failed: {ctx.err.getvalue()}")

            # summary advises how to adopt later
            out_text = ctx.out.getvalue()
            self.assertIn("differing instructions in", out_text)
            self.assertIn("left unowned", out_text)
            self.assertIn("edit the file to match, or delete it, then sync", out_text)

            # tool A's file stays unowned and status shows conflicts
            out_status = io.StringIO()
            err_status = io.StringIO()
            ctx_status = CommandContext(
                env=self.env,
                read_line=lambda p: "",
                is_interactive=False,
                out=out_status,
                err=err_status,
            )
            code_status = main(["status", "--json"], context=ctx_status)
            self.assertEqual(code_status, 2)
            data = json.loads(out_status.getvalue())
            tool_a_data = [t for t in data["tools"] if t["id"] == adapter_a.id][0]
            self.assertEqual(tool_a_data["kinds"]["instructions"]["state"], "conflict")

            # run sync: tool A's file is untouched
            out_sync = io.StringIO()
            err_sync = io.StringIO()
            ctx_sync = CommandContext(
                env=self.env,
                read_line=lambda p: "",
                is_interactive=False,
                out=out_sync,
                err=err_sync,
            )
            code_sync = main(["sync", "--yes"], context=ctx_sync)
            self.assertEqual(code_sync, 3)  # conflict on unowned file skipped
            self.assertIn("Conflicts (left alone, not changed)", out_sync.getvalue())
            self.assertEqual(paths_a.instructions.read_text(encoding="utf-8"), "# Instructions A\n")

    def test_init_interactive_from_tool_different_instructions_asks_replace_once_choose_never(self):
        adapter_a = [a for a in all_adapters() if a.id == "claude"][0]
        adapter_b = [a for a in all_adapters() if a.id == "codex"][0]

        paths_a = adapter_a.paths(self.env, Scope.GLOBAL)
        paths_a.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_a.instructions.write_text("# Instructions A\n", encoding="utf-8")

        paths_b = adapter_b.paths(self.env, Scope.GLOBAL)
        paths_b.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_b.instructions.write_text("# Instructions B\n", encoding="utf-8")

        detection_a = Detection(
            installed=True, tool=adapter_a.id, label=adapter_a.label, binary=Path("/bin/claude")
        )
        detection_b = Detection(
            installed=True, tool=adapter_b.id, label=adapter_b.label, binary=Path("/bin/codex")
        )
        detections = [detection_a, detection_b]

        prompts_asked: list[str] = []
        answers = {"replace": "y", "Proceed": "y"}

        def counting_read_line(prompt: str) -> str:
            prompts_asked.append(prompt)
            for k, v in answers.items():
                if k.lower() in prompt.lower():
                    return v
            return "y"

        ctx = self._ctx(is_interactive=True, read_line=counting_read_line)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.status.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.sync.detect_all", return_value=detections),
        ):
            code_init = main(["init", "--from", adapter_b.id], context=ctx)
            self.assertEqual(code_init, 0, f"init failed: {ctx.err.getvalue()}")

            # replace question asked exactly once, choose question never asked
            self.assertEqual(len(prompts_asked), 2)
            self.assertTrue(any("replace" in p.lower() for p in prompts_asked))
            self.assertFalse(any("choose" in p.lower() for p in prompts_asked))

    def test_init_interactive_without_from_different_instructions_replace_no_leaves_conflicts_and_advises_adoption(
        self,
    ):
        import json

        adapter_a = [a for a in all_adapters() if a.id == "claude"][0]
        adapter_b = [a for a in all_adapters() if a.id == "codex"][0]

        paths_a = adapter_a.paths(self.env, Scope.GLOBAL)
        paths_a.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_a.instructions.write_text("# Instructions A\n", encoding="utf-8")

        paths_b = adapter_b.paths(self.env, Scope.GLOBAL)
        paths_b.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_b.instructions.write_text("# Instructions B\n", encoding="utf-8")

        detection_a = Detection(
            installed=True, tool=adapter_a.id, label=adapter_a.label, binary=Path("/bin/claude")
        )
        detection_b = Detection(
            installed=True, tool=adapter_b.id, label=adapter_b.label, binary=Path("/bin/codex")
        )
        detections = [detection_a, detection_b]

        def interactive_answers(prompt: str) -> str:
            p_lower = prompt.lower()
            if "choice:" in p_lower:
                return "2"  # choose codex
            if "replace" in p_lower:
                return "n"  # do not replace tool A's file
            if "proceed" in p_lower:
                return "y"
            return "y"

        ctx = self._ctx(is_interactive=True, read_line=interactive_answers)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.status.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.sync.detect_all", return_value=detections),
        ):
            code_init = main(["init"], context=ctx)
            self.assertEqual(code_init, 0, f"init failed: {ctx.err.getvalue()}")

            out_text = ctx.out.getvalue()
            self.assertIn("differing instructions in", out_text)
            self.assertIn("left unowned", out_text)
            self.assertIn("edit the file to match, or delete it, then sync", out_text)

            # status shows conflict for unchosen differing tool (claude)
            out_status = io.StringIO()
            err_status = io.StringIO()
            ctx_status = CommandContext(
                env=self.env,
                read_line=lambda p: "",
                is_interactive=False,
                out=out_status,
                err=err_status,
            )
            code_status = main(["status", "--json"], context=ctx_status)
            self.assertEqual(code_status, 2)
            data = json.loads(out_status.getvalue())
            tool_a_data = [t for t in data["tools"] if t["id"] == adapter_a.id][0]
            self.assertEqual(tool_a_data["kinds"]["instructions"]["state"], "conflict")

            # sync leaves differing files alone
            out_sync = io.StringIO()
            err_sync = io.StringIO()
            ctx_sync = CommandContext(
                env=self.env,
                read_line=lambda p: "",
                is_interactive=False,
                out=out_sync,
                err=err_sync,
            )
            code_sync = main(["sync", "--yes"], context=ctx_sync)
            self.assertEqual(code_sync, 3)
            self.assertIn("Conflicts (left alone, not changed)", out_sync.getvalue())
            self.assertEqual(paths_a.instructions.read_text(encoding="utf-8"), "# Instructions A\n")

    def test_init_yes_without_from_differing_instructions_refuses(self):
        adapter_a = [a for a in all_adapters() if a.id == "claude"][0]
        adapter_b = [a for a in all_adapters() if a.id == "codex"][0]

        paths_a = adapter_a.paths(self.env, Scope.GLOBAL)
        paths_a.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_a.instructions.write_text("# Instructions A\n", encoding="utf-8")

        paths_b = adapter_b.paths(self.env, Scope.GLOBAL)
        paths_b.instructions.parent.mkdir(parents=True, exist_ok=True)
        paths_b.instructions.write_text("# Instructions B\n", encoding="utf-8")

        detection_a = Detection(
            installed=True, tool=adapter_a.id, label=adapter_a.label, binary=Path("/bin/claude")
        )
        detection_b = Detection(
            installed=True, tool=adapter_b.id, label=adapter_b.label, binary=Path("/bin/codex")
        )
        detections = [detection_a, detection_b]

        ctx = self._ctx(is_interactive=False)

        with (
            patch("one_setup_all_agents.commands.init.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.status.detect_all", return_value=detections),
            patch("one_setup_all_agents.commands.sync.detect_all", return_value=detections),
        ):
            code_init = main(["init", "--yes"], context=ctx)
            self.assertEqual(code_init, 1)
            self.assertIn("instructions differ between tools; pass --from TOOL", ctx.err.getvalue())


if __name__ == "__main__":
    unittest.main()
