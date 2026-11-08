"""Tests for config_source loader, validator, and renderers."""

import re
import tempfile
import unittest
from pathlib import Path

from tests.demo_tool_adapter import DemoToolAdapter
from tests.engine_support import make_env
from tests.support import FIXTURE_SECRETS

import _bootstrap  # noqa: F401
from one_setup_all_agents import config_source
from one_setup_all_agents.config_source import (
    INSTRUCTIONS_NAME,
    MCP_NAME,
    SKILLS_DIRNAME,
    SOURCE_CONFIG_NAME,
    ServerEntry,
    SourceError,
    ToolSettings,
    desired_all,
    desired_for,
    load_source,
    render_instructions_core,
    render_mcp_source,
    render_source_config,
    source_exists,
)
from one_setup_all_agents.engine.plan import build_plan
from one_setup_all_agents.model import HeaderRef, McpServer

PLANTED_SECRET = FIXTURE_SECRETS[0]


class ConfigSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.adapter_ids = ("example", "other")

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_exists_and_empty_check(self):
        self.assertFalse(source_exists(self.config_dir))
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        self.assertTrue(source_exists(self.config_dir))

    def test_round_trip_stdio_http_and_settings(self):
        tools = {
            "example": ToolSettings(enabled=True, skills_exclude=("skip-skill",)),
            "other": ToolSettings(enabled=False, skills_exclude=()),
        }
        (self.config_dir / SOURCE_CONFIG_NAME).write_text(
            render_source_config(tools), encoding="utf-8"
        )

        entries = {
            "demo-stdio": ServerEntry(
                server=McpServer(
                    name="demo-stdio",
                    command="demo-bin",
                    args=("--flag", "val"),
                    cwd="/tmp/demo",
                    env=(("PUBLIC_VAR", "literal_1"),),
                    env_vars=("SECRET_VAR",),
                    startup_timeout_sec=45,
                    enabled=True,
                ),
                tools=("example",),
            ),
            "demo-http": ServerEntry(
                server=McpServer(
                    name="demo-http",
                    url="https://api.example.test/mcp",
                    headers=(
                        HeaderRef("Authorization", "AUTH_TOKEN", "Bearer"),
                        HeaderRef("X-Api-Key", "API_KEY", None),
                    ),
                    auth="oauth",
                    enabled=False,
                ),
                tools=None,
            ),
        }
        (self.config_dir / MCP_NAME).write_text(
            render_mcp_source(entries, notes={"demo-stdio": ["test note"]}), encoding="utf-8"
        )

        (self.config_dir / INSTRUCTIONS_NAME).write_text(
            render_instructions_core("# Core instructions"), encoding="utf-8"
        )
        (self.config_dir / "instructions.example.md").write_text(
            "Example appendix content\n", encoding="utf-8"
        )

        skill_dir = self.config_dir / SKILLS_DIRNAME / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")

        cfg = load_source(self.config_dir, self.adapter_ids)
        self.assertEqual(cfg.tools["example"].enabled, True)
        self.assertEqual(cfg.tools["example"].skills_exclude, ("skip-skill",))
        self.assertEqual(cfg.tools["other"].enabled, False)

        stdio_loaded = cfg.servers["demo-stdio"]
        self.assertEqual(stdio_loaded.tools, ("example",))
        self.assertEqual(stdio_loaded.server.command, "demo-bin")
        self.assertEqual(stdio_loaded.server.args, ("--flag", "val"))
        self.assertEqual(stdio_loaded.server.cwd, "/tmp/demo")
        self.assertEqual(stdio_loaded.server.env, (("PUBLIC_VAR", "literal_1"),))
        self.assertEqual(stdio_loaded.server.env_vars, ("SECRET_VAR",))
        self.assertEqual(stdio_loaded.server.startup_timeout_sec, 45)
        self.assertEqual(stdio_loaded.server.enabled, True)

        http_loaded = cfg.servers["demo-http"]
        self.assertEqual(http_loaded.tools, None)
        self.assertEqual(http_loaded.server.url, "https://api.example.test/mcp")
        self.assertEqual(http_loaded.server.auth, "oauth")
        self.assertEqual(http_loaded.server.enabled, False)
        self.assertEqual(
            http_loaded.server.headers,
            (
                HeaderRef("Authorization", "AUTH_TOKEN", "Bearer"),
                HeaderRef("X-Api-Key", "API_KEY", None),
            ),
        )

        self.assertEqual(cfg.instructions_core, "# Core instructions\n")
        self.assertEqual(cfg.appendices["example"], "Example appendix content\n")
        self.assertEqual(len(cfg.skills), 1)
        self.assertEqual(cfg.skills[0].name, "test-skill")
        self.assertEqual(cfg.warnings, ())

    def test_refuse_unsupported_source_version(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 2\n", encoding="utf-8")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertIn("unsupported source version 2", str(ctx.exception))

    def test_refuse_unknown_top_level_key_in_config(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text(
            "version = 1\nunknown_key = true\n", encoding="utf-8"
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertIn("unknown top-level key 'unknown_key'", str(ctx.exception))

    def test_refuse_unknown_tool_id_in_tools_table(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text(
            "version = 1\n[tools.unknown_tool]\nenabled = true\n", encoding="utf-8"
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertIn("unknown tool id 'unknown_tool'", str(ctx.exception))

    def test_refuse_unknown_tool_id_in_mcp_servers(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        mcp_content = """{
            "version": 1,
            "servers": {
                "s1": {
                    "command": "demo",
                    "tools": ["non_existent_tool"]
                }
            }
        }"""
        (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertIn("unknown tool id 'non_existent_tool'", str(ctx.exception))

    def test_refuse_literal_token_in_headers_and_no_secret_leak(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        mcp_content = f"""{{
            "version": 1,
            "servers": {{
                "s1": {{
                    "url": "https://example.test/mcp",
                    "headers": {{ "Authorization": "Bearer {PLANTED_SECRET}" }}
                }}
            }}
        }}"""
        (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        msg = str(ctx.exception)
        self.assertIn("headers must reference an environment variable", msg)
        self.assertNotIn(PLANTED_SECRET, msg)

    def test_refuse_secret_looking_env_literal_and_no_leak(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        mcp_content = f"""{{
            "version": 1,
            "servers": {{
                "s1": {{
                    "command": "demo",
                    "env": {{ "SECRET_KEY": "{PLANTED_SECRET}" }}
                }}
            }}
        }}"""
        (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        msg = str(ctx.exception)
        self.assertIn("looks like a secret; use env_vars", msg)
        self.assertNotIn(PLANTED_SECRET, msg)

    def test_refuse_legacy_double_curly_var_syntax_and_no_leak(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        mcp_content = f"""{{
            "version": 1,
            "servers": {{
                "s1": {{
                    "command": "demo",
                    "args": ["{{{{{PLANTED_SECRET}}}}}"]
                }}
            }}
        }}"""
        (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        msg = str(ctx.exception)
        self.assertIn("legacy '{{VAR}}' syntax", msg)
        self.assertNotIn(PLANTED_SECRET, msg)

    def test_refuse_legacy_keys(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        for legacy_key in ("retired", "unmanaged", "keep_live_env", "vars"):
            mcp_content = f"""{{
                "version": 1,
                "servers": {{
                    "s1": {{
                        "command": "demo",
                        "{legacy_key}": true
                    }}
                }}
            }}"""
            (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
            with self.assertRaises(SourceError) as ctx:
                load_source(self.config_dir, self.adapter_ids)
            self.assertIn(f"legacy key '{legacy_key}'", str(ctx.exception))

    def test_refuse_invalid_env_var_name(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        mcp_content = """{
            "version": 1,
            "servers": {
                "s1": {
                    "command": "demo",
                    "env_vars": ["INVALID-NAME!"]
                }
            }
        }"""
        (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertIn("invalid environment variable name", str(ctx.exception))

    def test_bom_and_crlf_handling(self):
        crlf_toml = "\ufeffversion = 1\r\n[tools.example]\r\nenabled = true\r\n"
        (self.config_dir / SOURCE_CONFIG_NAME).write_bytes(crlf_toml.encode("utf-8"))

        crlf_mcp = '\ufeff{\r\n  "version": 1,\r\n  "servers": {}\r\n}\r\n'
        (self.config_dir / MCP_NAME).write_bytes(crlf_mcp.encode("utf-8"))

        cfg = load_source(self.config_dir, self.adapter_ids)
        self.assertTrue(cfg.tools["example"].enabled)
        self.assertEqual(cfg.servers, {})

    def test_desired_for_and_desired_all_filtering(self):
        tools = {
            "example": ToolSettings(enabled=True, skills_exclude=("skip-skill",)),
            "other": ToolSettings(enabled=False, skills_exclude=()),
        }
        (self.config_dir / SOURCE_CONFIG_NAME).write_text(
            render_source_config(tools), encoding="utf-8"
        )

        entries = {
            "all-tools": ServerEntry(server=McpServer(name="all-tools", command="demo")),
            "example-only": ServerEntry(
                server=McpServer(name="example-only", command="demo"), tools=("example",)
            ),
            "other-only": ServerEntry(
                server=McpServer(name="other-only", command="demo"), tools=("other",)
            ),
        }
        (self.config_dir / MCP_NAME).write_text(render_mcp_source(entries), encoding="utf-8")

        (self.config_dir / INSTRUCTIONS_NAME).write_text("Base instructions\n", encoding="utf-8")
        (self.config_dir / "instructions.example.md").write_text(
            "Example appendix\n", encoding="utf-8"
        )

        for sname in ("keep-skill", "skip-skill"):
            sdir = self.config_dir / SKILLS_DIRNAME / sname
            sdir.mkdir(parents=True)
            (sdir / "SKILL.md").write_text(f"# {sname}\n", encoding="utf-8")

        cfg = load_source(self.config_dir, self.adapter_ids)
        d_example = desired_for(cfg, "example")
        self.assertEqual(d_example.instructions, "Base instructions\n\nExample appendix\n")
        self.assertEqual(set(d_example.mcp.keys()), {"all-tools", "example-only"})
        self.assertEqual([s.name for s in d_example.skills], ["keep-skill"])

        # desired_all omits disabled tools
        d_all = desired_all(cfg, self.adapter_ids)
        self.assertIn("example", d_all)
        self.assertNotIn("other", d_all)

    def test_absent_instructions_yields_none(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        cfg = load_source(self.config_dir, self.adapter_ids)
        self.assertIsNone(cfg.instructions_core)
        d = desired_for(cfg, "example")
        self.assertIsNone(d.instructions)

    def test_skill_folder_without_skill_md_and_dotted_folder_skipped_with_warning(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / SKILLS_DIRNAME / ".hidden").mkdir(parents=True)
        (self.config_dir / SKILLS_DIRNAME / "incomplete").mkdir(parents=True)
        cfg = load_source(self.config_dir, self.adapter_ids)
        self.assertEqual(len(cfg.skills), 0)
        self.assertEqual(len(cfg.warnings), 2)
        self.assertTrue(any("skipped (invalid name)" in w for w in cfg.warnings))
        self.assertTrue(any("skipped (missing SKILL.md)" in w for w in cfg.warnings))

    def test_bidi_character_in_instructions_triggers_warning(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / INSTRUCTIONS_NAME).write_text(
            "Instructions with bidi \u200f test\n", encoding="utf-8"
        )
        cfg = load_source(self.config_dir, self.adapter_ids)
        self.assertEqual(len(cfg.warnings), 1)
        self.assertIn("bidirectional control character", cfg.warnings[0])

    def test_desired_state_works_with_engine_build_plan(self):
        env = make_env(self.config_dir / "home")
        adapter = DemoToolAdapter()
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        entries = {"srv": ServerEntry(server=McpServer(name="srv", command="run-srv"))}
        (self.config_dir / MCP_NAME).write_text(render_mcp_source(entries), encoding="utf-8")

        cfg = load_source(self.config_dir, (adapter.id,))
        desired = desired_for(cfg, adapter.id)
        plan = build_plan(env, [adapter], desired)
        self.assertIsNotNone(plan)

    def test_fixture_source_loads_cleanly(self):
        fixture_dir = Path(__file__).parent / "fixtures" / "source"
        cfg = load_source(fixture_dir, ("example",))
        self.assertIn("example", cfg.tools)
        self.assertIn("demo-server", cfg.servers)
        self.assertEqual(len(cfg.skills), 1)
        self.assertEqual(cfg.skills[0].name, "demo-skill")
        self.assertEqual(cfg.warnings, ())

    def test_config_source_module_contains_no_banned_tool_names(self):
        mod_path = Path(config_source.__file__)
        text = mod_path.read_text("utf-8")
        for banned in ("opencode", "claude", "codex", "antigravity", "gemini", "agy"):
            pattern = rf"(?<![A-Za-z]){banned}(?![A-Za-z])"
            self.assertIsNone(
                re.search(pattern, text, re.IGNORECASE),
                f"Banned name '{banned}' in config_source.py",
            )


if __name__ == "__main__":
    unittest.main()
