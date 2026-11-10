"""Tests for the `all-agents status` command (Phase 5 M05-T2)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import EngineCase, FakeRunner
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.cli import main
from one_setup_all_agents.config_source import INSTRUCTIONS_NAME, MCP_NAME, SOURCE_CONFIG_NAME
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.detect import ProbeResult
from one_setup_all_agents.model import Scope

PLANTED_SECRET = "sk-" + "a1b2c3d4" * 6


class CmdStatusTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner()
        self.probe_calls: list[list[str]] = []

    def _probe(self, argv, environ, timeout):
        self.probe_calls.append(list(argv))
        return ProbeResult(0, "1.2.3\n", "")

    def _ctx(
        self,
        runner: FakeRunner | None = None,
        is_interactive: bool = False,
        read_line=lambda prompt: "",
    ) -> tuple[CommandContext, io.StringIO, io.StringIO]:
        out = io.StringIO()
        err = io.StringIO()
        ctx = CommandContext(
            env=self.env,
            read_line=read_line,
            run_cli=runner or self.runner,
            probe=self._probe,
            is_interactive=is_interactive,
            out=out,
            err=err,
        )
        return ctx, out, err

    def _init_and_sync(self) -> None:
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.instructions:
                self.write(paths.instructions, b"# Shared instructions\n")
            if paths.mcp:
                self.write(paths.mcp, b"{}" if "json" in paths.mcp.name else b"")
            for snap in a.snapshot_files(self.env, Scope.GLOBAL):
                if not snap.exists():
                    self.write(snap, b"{}" if "json" in snap.name else b"")

        ctx, _, _ = self._ctx()
        code_init = main(["init", "--yes"], context=ctx)
        self.assertEqual(code_init, 0)
        code_sync = main(["sync", "--yes"], context=ctx)
        self.assertEqual(code_sync, 0)
        self.runner.calls.clear()
        self.probe_calls.clear()

    def test_not_initialised_exits_one(self):
        # Non-JSON
        ctx, out, err = self._ctx()
        code = main(["status"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("not initialised: run `all-agents init`", out.getvalue())

        # JSON
        ctx_json, out_json, _ = self._ctx()
        code_json = main(["status", "--json"], context=ctx_json)
        self.assertEqual(code_json, 1)
        data = json.loads(out_json.getvalue())
        self.assertEqual(data["schema"], 1)
        self.assertFalse(data["initialised"])
        self.assertEqual(data["tools"], [])
        self.assertEqual(data["undoable_operations"], 0)

    def test_fully_in_sync_after_init_and_sync_exits_zero(self):
        self._init_and_sync()

        # Human-readable
        ctx, out, err = self._ctx()
        code = main(["status"], context=ctx)
        self.assertEqual(code, 0)
        self.assertIn("Undoable operations:", out.getvalue())

        # JSON
        ctx_json, out_json, _ = self._ctx()
        code_json = main(["status", "--json"], context=ctx_json)
        self.assertEqual(code_json, 0)
        data = json.loads(out_json.getvalue())
        self.assertEqual(data["schema"], 1)
        self.assertTrue(data["initialised"])
        for tool in data["tools"]:
            self.assertEqual(tool["state"], "in sync")
            self.assertEqual(tool["kinds"]["instructions"]["state"], "in sync")
            self.assertEqual(tool["kinds"]["mcp"]["state"], "in sync")

    def test_one_mcp_entry_differs_exits_two(self):
        self._init_and_sync()

        # Add server "demo-server" in shared mcp.jsonc
        mcp_content = json.dumps(
            {"version": 1, "servers": {"demo-server": {"command": "source-command"}}}
        )
        (self.env.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")

        # Human-readable
        ctx, out, err = self._ctx()
        code = main(["status"], context=ctx)
        self.assertEqual(code, 2)
        self.assertIn("differs", out.getvalue().lower())
        self.assertIn("demo-server", out.getvalue())

        # JSON
        ctx_json, out_json, _ = self._ctx()
        code_json = main(["status", "--json"], context=ctx_json)
        self.assertEqual(code_json, 2)
        data = json.loads(out_json.getvalue())
        differs_found = False
        for tool in data["tools"]:
            if tool["kinds"]["mcp"]["state"] == "differs":
                differs_found = True
                self.assertIn("demo-server", tool["kinds"]["mcp"]["items"])
        self.assertTrue(differs_found)

    def test_unowned_conflicting_entry_exits_two(self):
        self._init_and_sync()

        # Add server "demo-server" in source
        mcp_content = json.dumps(
            {"version": 1, "servers": {"demo-server": {"command": "source-command"}}}
        )
        (self.env.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")

        # Plant an unowned conflicting entry in an adapter's MCP config file
        target_adapter = None
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.mcp and "json" in paths.mcp.name:
                target_adapter = a
                break
        self.assertIsNotNone(target_adapter)
        mcp_path = target_adapter.paths(self.env, Scope.GLOBAL).mcp
        conflicting_text = json.dumps(
            {"mcp": {"demo-server": {"command": "conflicting-unowned-command"}}}
        )
        self.write(mcp_path, conflicting_text.encode("utf-8"))

        ctx_json, out_json, _ = self._ctx()
        code = main(["status", "--json"], context=ctx_json)
        self.assertEqual(code, 2)
        data = json.loads(out_json.getvalue())
        conflict_found = False
        for tool in data["tools"]:
            if tool["id"] == target_adapter.id:
                self.assertEqual(tool["kinds"]["mcp"]["state"], "conflict")
                self.assertIn("demo-server", tool["kinds"]["mcp"]["items"])
                self.assertTrue(len(tool["kinds"]["mcp"]["reasons"]) > 0)
                conflict_found = True
        self.assertTrue(conflict_found)

    def test_unreadable_config_exits_two(self):
        self._init_and_sync()

        # Plant corrupted json in an adapter's config file
        target_adapter = None
        for a in all_adapters():
            paths = a.paths(self.env, Scope.GLOBAL)
            if paths.mcp and "json" in paths.mcp.name:
                target_adapter = a
                break
        self.assertIsNotNone(target_adapter)
        mcp_path = target_adapter.paths(self.env, Scope.GLOBAL).mcp
        self.write(mcp_path, b"{invalid-json-content")

        ctx_json, out_json, _ = self._ctx()
        code = main(["status", "--json"], context=ctx_json)
        self.assertEqual(code, 2)
        data = json.loads(out_json.getvalue())
        unreadable_found = False
        for tool in data["tools"]:
            if tool["id"] == target_adapter.id:
                self.assertEqual(tool["kinds"]["mcp"]["state"], "unreadable")
                self.assertTrue(len(tool["kinds"]["mcp"]["reasons"]) > 0)
                # Other kinds still reported
                self.assertEqual(tool["kinds"]["instructions"]["state"], "in sync")
                unreadable_found = True
        self.assertTrue(unreadable_found)

    def test_disabled_tool_reported_as_disabled(self):
        self._init_and_sync()

        # Disable one tool in config.toml
        config_file = self.env.config_dir / SOURCE_CONFIG_NAME
        text = config_file.read_text("utf-8")
        target_id = all_adapters()[0].id
        text = text.replace(
            f"[tools.{target_id}]\nenabled = true",
            f"[tools.{target_id}]\nenabled = false",
        )
        config_file.write_text(text, encoding="utf-8")

        ctx_json, out_json, _ = self._ctx()
        code = main(["status", "--json"], context=ctx_json)
        self.assertEqual(code, 0)
        data = json.loads(out_json.getvalue())
        for tool in data["tools"]:
            if tool["id"] == target_id:
                self.assertEqual(tool["state"], "disabled")
                self.assertEqual(tool["kinds"]["instructions"]["state"], "disabled")
                self.assertEqual(tool["kinds"]["mcp"]["state"], "disabled")
                self.assertEqual(tool["kinds"]["skills"]["state"], "disabled")

    def test_uninstalled_tool_reported_as_not_installed(self):
        # Initialise with only one tool's config
        single_adapter = all_adapters()[0]
        paths = single_adapter.paths(self.env, Scope.GLOBAL)
        if paths.instructions:
            self.write(paths.instructions, b"# instructions\n")
        if paths.mcp:
            self.write(paths.mcp, b"{}" if "json" in paths.mcp.name else b"")
        for snap in single_adapter.snapshot_files(self.env, Scope.GLOBAL):
            if not snap.exists():
                self.write(snap, b"{}" if "json" in snap.name else b"")

        ctx_init, _, _ = self._ctx()
        main(["init", "--yes"], context=ctx_init)

        ctx_json, out_json, _ = self._ctx()
        code = main(["status", "--json"], context=ctx_json)
        self.assertEqual(code, 0)
        data = json.loads(out_json.getvalue())
        uninstalled_ids = {a.id for a in all_adapters() if a.id != single_adapter.id}
        for tool in data["tools"]:
            if tool["id"] in uninstalled_ids:
                self.assertEqual(tool["state"], "not installed")
                for k_info in tool["kinds"].values():
                    self.assertEqual(k_info["state"], "not installed")

    def test_json_parses_schema_keys_present_no_home_path_no_secrets(self):
        self._init_and_sync()

        # Plant secret in source instructions
        instr_file = self.env.config_dir / INSTRUCTIONS_NAME
        instr_file.write_text(f"# Secret instructions: {PLANTED_SECRET}\n", encoding="utf-8")

        ctx_json, out_json, _ = self._ctx()
        code = main(["status", "--json"], context=ctx_json)
        # Differences exist because instructions changed
        self.assertEqual(code, 2)
        raw_output = out_json.getvalue()

        # Must not contain real home directory path or secret
        self.assertNotIn(str(self.home), raw_output)
        self.assertNotIn(PLANTED_SECRET, raw_output)

        data = json.loads(raw_output)
        self.assertIn("schema", data)
        self.assertIn("initialised", data)
        self.assertIn("tools", data)
        self.assertIn("undoable_operations", data)

    def test_versions_flag_controls_probe_runner_invocation(self):
        self._init_and_sync()

        # Without --versions: 0 probe calls
        ctx1, _, _ = self._ctx()
        main(["status"], context=ctx1)
        self.assertEqual(len(self.probe_calls), 0)

        # With --versions: probe is called if binaries exist
        # Create a fake binary for one tool
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake_binary = bin_dir / ("agy.exe" if os.name == "nt" else "agy")
        fake_binary.write_bytes(b"")
        fake_binary.chmod(0o755)
        self.env.environ["PATH"] = str(bin_dir)

        ctx2, _, _ = self._ctx()
        main(["status", "--versions"], context=ctx2)
        self.assertGreater(len(self.probe_calls), 0)

    def test_read_only_status_writes_nothing(self):
        # 1. When not initialised and state does not exist: status creates no state folder
        ctx_uninit, _, _ = self._ctx()
        before_root = self.listing(self.root)
        main(["status"], context=ctx_uninit)
        main(["status", "--json"], context=ctx_uninit)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.listing(self.root), before_root)

        # 2. After initialised: status modifies neither home, config, nor state
        self._init_and_sync()
        before_home = self.listing(self.home)
        before_config = self.listing(self.env.config_dir)
        before_state = self.listing(self.state)

        ctx, _, _ = self._ctx()
        main(["status"], context=ctx)
        main(["status", "--json"], context=ctx)

        self.assertEqual(self.listing(self.home), before_home)
        self.assertEqual(self.listing(self.env.config_dir), before_config)
        self.assertEqual(self.listing(self.state), before_state)

    def test_undoable_count_equals_count_of_done_operations(self):
        self._init_and_sync()
        # After init, 1 done operation exists
        ctx_json1, out_json1, _ = self._ctx()
        main(["status", "--json"], context=ctx_json1)
        data1 = json.loads(out_json1.getvalue())
        self.assertEqual(data1["undoable_operations"], 1)

        # Apply a change with sync --yes to create a second operation
        (self.env.config_dir / INSTRUCTIONS_NAME).write_text(
            "# New instructions\n", encoding="utf-8"
        )
        ctx_sync, _, _ = self._ctx()
        code_sync = main(["sync", "--yes"], context=ctx_sync)
        self.assertEqual(code_sync, 0)

        # Now 2 done operations exist
        ctx_json2, out_json2, _ = self._ctx()
        main(["status", "--json"], context=ctx_json2)
        data2 = json.loads(out_json2.getvalue())
        self.assertEqual(data2["undoable_operations"], 2)

    def test_subprocess_smoke_status_json(self):
        self._init_and_sync()

        sub_env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
            "XDG_CONFIG_HOME": str(self.root / "xdg_config"),
            "XDG_STATE_HOME": str(self.root / "xdg_state"),
            "ALL_AGENTS_CONFIG_DIR": str(self.env.config_dir),
            "ALL_AGENTS_STATE_DIR": str(self.state),
            "PATH": str(Path(sys.executable).parent),
            "PYTHONPATH": str(_bootstrap.SRC),
        }
        for var in ("SYSTEMROOT", "SystemDrive", "TMP", "TEMP"):
            if var in os.environ:
                sub_env[var] = os.environ[var]

        res = subprocess.run(
            [sys.executable, "-m", "one_setup_all_agents", "status", "--json"],
            env=sub_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"stdout: {res.stdout}\nstderr: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertEqual(data["schema"], 1)
        self.assertTrue(data["initialised"])
        self.assertEqual(data["undoable_operations"], 1)

    def test_status_with_corrupt_ledger_exits_one_without_traceback(self):
        self._init_and_sync()
        # Corrupt ledger.json
        (self.state / "ledger.json").write_bytes(b"{corrupted json")

        ctx, out, err = self._ctx()
        code = main(["status"], context=ctx)
        self.assertEqual(code, 1)
        self.assertIn("cannot read ledger.json", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("Traceback", out.getvalue())

    def test_status_json_with_corrupt_ledger_exits_one(self):
        self._init_and_sync()
        # Corrupt ledger.json
        (self.state / "ledger.json").write_bytes(b"{corrupted json")

        ctx, out, err = self._ctx()
        code = main(["status", "--json"], context=ctx)
        self.assertEqual(code, 1)
        data = json.loads(out.getvalue())
        self.assertEqual(data["schema"], 1)
        self.assertIn("cannot read ledger.json", data["error"])


    def test_status_quiet_in_sync_prints_nothing_and_exits_zero(self):
        self._init_and_sync()
        ctx, out, err = self._ctx()
        code = main(["status", "--quiet"], context=ctx)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_status_quiet_on_drift_prints_single_line_to_stderr_and_exits_zero(self):
        self._init_and_sync()
        # Create drift by modifying instructions in source config
        (self.env.config_dir / INSTRUCTIONS_NAME).write_text("# Drifting content\n", encoding="utf-8")
        ctx, out, err = self._ctx()
        code = main(["status", "--quiet"], context=ctx)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        err_lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(err_lines), 1)
        self.assertIn("drift detected", err_lines[0])
        self.assertNotIn("Traceback", err.getvalue())

    def test_status_quiet_when_uninitialised_exits_one(self):
        ctx, out, err = self._ctx()
        code = main(["status", "--quiet"], context=ctx)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("not initialised: run `all-agents init`", err.getvalue())


if __name__ == "__main__":
    unittest.main()
