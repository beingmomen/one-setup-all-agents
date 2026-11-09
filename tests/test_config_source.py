"""Tests for config_source loader, validator, and renderers."""

import re
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from demo_tool_adapter import DemoToolAdapter
from engine_support import make_env
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
    load_source_texts,
    render_instructions_core,
    render_mcp_source,
    render_source_config,
    source_exists,
)
from one_setup_all_agents.engine.plan import build_plan
from one_setup_all_agents.model import HeaderRef, McpServer
from support import FIXTURE_SECRETS

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
        self.assertNotIn("unknown_tool", str(ctx.exception))
        self.assertIn("position #", str(ctx.exception))

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
        self.assertNotIn("non_existent_tool", str(ctx.exception))
        self.assertIn("position #", str(ctx.exception))

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
        self.assertIn("is not a valid environment variable name", str(ctx.exception))

    def test_leak1_env_vars_invalid_name_does_not_leak_secret(self):
        fake = "sk-" + "a1" * 20
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        mcp_content = f"""{{
            "version": 1,
            "servers": {{
                "s1": {{
                    "command": "demo",
                    "env_vars": ["{fake}"]
                }}
            }}
        }}"""
        (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertNotIn(fake, str(ctx.exception))
        self.assertIn("env_vars entry #1", str(ctx.exception))

    def test_leak2_args_flag_plus_literal_secret_refused(self):
        fake = "plainSecretValue12345"
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        mcp_content = f"""{{
            "version": 1,
            "servers": {{
                "s1": {{
                    "command": "demo",
                    "args": ["--api-key", "{fake}"]
                }}
            }}
        }}"""
        (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertNotIn(fake, str(ctx.exception))
        self.assertIn("args look like a secret", str(ctx.exception))

    def test_leak2_args_flag_plus_env_var_accepted(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        mcp_content = """{
            "version": 1,
            "servers": {
                "s1": {
                    "command": "demo",
                    "args": ["--api-key", "${API_KEY}"]
                }
            }
        }"""
        (self.config_dir / MCP_NAME).write_text(mcp_content, encoding="utf-8")
        cfg = load_source(self.config_dir, self.adapter_ids)
        self.assertEqual(cfg.servers["s1"].server.args, ("--api-key", "${API_KEY}"))

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
        matching_ops = [
            op
            for op in plan.executable
            if any(ref.key == "srv" for ref in getattr(op, "claims", ()))
        ]
        self.assertGreater(len(matching_ops), 0)
        self.assertEqual(matching_ops[0].claims[0].key, "srv")

    def test_strict_types_args_must_be_list_of_strings(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "args": "not-a-list"}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertIn("args", str(ctx.exception))

        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "args": ["ok", 123]}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertIn("args", str(ctx.exception))

    def test_strict_types_cwd_must_be_string(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "cwd": 123}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertIn("cwd", str(ctx.exception))

    def test_strict_types_url_and_command_exclusivity_and_non_empty(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        # Both url and command
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "url": "http://example.com"}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

        # Neither
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"cwd": "/tmp"}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

        # Empty command
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "   "}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

        # Empty url
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"url": ""}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

    def test_strict_types_startup_timeout_sec_positive_number_not_bool(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        # bool True
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "startup_timeout_sec": true}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

        # zero or negative
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "startup_timeout_sec": 0}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

    def test_strict_types_auth_only_oauth_none_or_absent(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "auth": "bearer"}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

    def test_strict_types_skills_exclude_items_must_be_strings(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text(
            "[tools.example]\nenabled = true\nskills_exclude = [123]\nversion = 1\n",
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

    def test_strict_types_version_int_not_bool(self):
        # bool version in config.toml
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = true\n", encoding="utf-8")
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

        # bool version in mcp.jsonc
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / MCP_NAME).write_text(
            '{"version": true, "servers": {}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

    def test_strict_types_tools_items_must_be_strings(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "tools": [123]}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

    def test_strict_types_headers_only_on_http_and_env_only_on_stdio(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        # headers on stdio
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"command": "echo", "headers": {"X-Test": "${TEST}"}}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

        # env on http
        (self.config_dir / MCP_NAME).write_text(
            '{"version": 1, "servers": {"s": {"url": "https://example.com", "env": {"VAR": "val"}}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError):
            load_source(self.config_dir, self.adapter_ids)

    def test_unreadable_instructions_or_skills_raises_clean_source_error(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / INSTRUCTIONS_NAME).write_bytes(b"\xff\xfe\x00\x80\x81")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertEqual(str(ctx.exception), f"cannot read '{INSTRUCTIONS_NAME}'")

        (self.config_dir / INSTRUCTIONS_NAME).write_text("# Valid\n", encoding="utf-8")
        (self.config_dir / "instructions.example.md").write_bytes(b"\xff\xfe\x00\x80\x81")
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertEqual(str(ctx.exception), "cannot read 'instructions.example.md'")

    def test_unknown_tool_id_message_names_position_not_value(self):
        secret_tool = "SECRET_TOOL_ID_XYZ"
        (self.config_dir / SOURCE_CONFIG_NAME).write_text(
            f"version = 1\n[tools.{secret_tool}]\nenabled = true\n",
            encoding="utf-8",
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertNotIn(secret_tool, str(ctx.exception))
        self.assertIn("position", str(ctx.exception).lower())

        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        (self.config_dir / MCP_NAME).write_text(
            f'{{"version": 1, "servers": {{"s": {{"command": "echo", "tools": ["{secret_tool}"]}}}}}}',
            encoding="utf-8",
        )
        with self.assertRaises(SourceError) as ctx:
            load_source(self.config_dir, self.adapter_ids)
        self.assertNotIn(secret_tool, str(ctx.exception))
        self.assertIn("position", str(ctx.exception).lower())

    def test_renderer_skills_exclude_uses_json_dumps(self):
        tools = {
            "example": ToolSettings(enabled=True, skills_exclude=('skip"quoted"',)),
        }
        rendered = render_source_config(tools)
        self.assertIn(r'skills_exclude = ["skip\"quoted\""]', rendered)

    def test_renderer_notes_emitted_per_server_and_escapes_newlines(self):
        entries = {
            "srv1": ServerEntry(server=McpServer(name="srv1", command="echo")),
            "srv2": ServerEntry(server=McpServer(name="srv2", command="echo")),
        }
        notes = {
            "srv1": ["note with\nnewline and\r\ncarriage"],
        }
        rendered = render_mcp_source(entries, notes=notes)
        self.assertNotIn("newline and\n", rendered)
        self.assertIn("note with newline and carriage", rendered)

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

    def test_load_source_texts_valid_in_memory_returns_source_config(self):
        toml_text = "version = 1\n[tools.example]\nenabled = true\n"
        mcp_text = """{
  "version": 1,
  "servers": {
    "demo": {
      "command": "demo-bin"
    }
  }
}
"""
        inst_text = "# Core instructions\n"
        app_text = {"example": "# Appendix example\n"}
        cfg = load_source_texts(
            config_toml=toml_text,
            mcp_jsonc=mcp_text,
            instructions=inst_text,
            appendices=app_text,
            valid_tool_ids=("example",),
        )
        self.assertIn("example", cfg.tools)
        self.assertTrue(cfg.tools["example"].enabled)
        self.assertIn("demo", cfg.servers)
        self.assertEqual(cfg.instructions_core, "# Core instructions\n")
        self.assertEqual(cfg.appendices.get("example"), "# Appendix example\n")
        self.assertEqual(cfg.warnings, ())

    def test_load_source_texts_invalid_toml_raises_source_error(self):
        with self.assertRaises(SourceError) as ctx:
            load_source_texts("not valid toml [[", valid_tool_ids=self.adapter_ids)
        self.assertIn("failed to parse", str(ctx.exception))

    def test_load_source_texts_invalid_mcp_raises_source_error(self):
        with self.assertRaises(SourceError) as ctx:
            load_source_texts(
                "version = 1\n", valid_tool_ids=self.adapter_ids, mcp_jsonc="{ not valid jsonc"
            )
        self.assertIn("failed to parse", str(ctx.exception))

    def test_load_source_texts_unknown_tool_id_raises_source_error(self):
        toml_text = "version = 1\n[tools.unknown]\nenabled = true\n"
        with self.assertRaises(SourceError) as ctx:
            load_source_texts(toml_text, valid_tool_ids=("known-only",))
        self.assertIn("unknown tool id", str(ctx.exception))

    def test_load_source_bom_and_crlf_instructions_decoded_without_feff(self):
        (self.config_dir / SOURCE_CONFIG_NAME).write_text("version = 1\n", encoding="utf-8")
        bom_crlf = b"\xef\xbb\xbf# Core instructions\r\nSecond line\r\n"
        (self.config_dir / INSTRUCTIONS_NAME).write_bytes(bom_crlf)
        (self.config_dir / "instructions.example.md").write_bytes(
            b"\xef\xbb\xbf# Appendix content\r\nSecond line\r\n"
        )
        cfg = load_source(self.config_dir, self.adapter_ids)
        self.assertNotIn("\ufeff", cfg.instructions_core)
        self.assertEqual(cfg.instructions_core, "# Core instructions\nSecond line\n")
        self.assertNotIn("\ufeff", cfg.appendices["example"])
        self.assertEqual(cfg.appendices["example"], "# Appendix content\nSecond line\n")

    def test_render_mcp_source_escapes_quotes_and_backslashes_in_server_name(self):
        weird_name = r'test"name\server'
        entries = {
            weird_name: ServerEntry(server=McpServer(name=weird_name, command="echo")),
        }
        rendered = render_mcp_source(entries)
        cfg = load_source_texts(
            config_toml="version = 1\n", valid_tool_ids=self.adapter_ids, mcp_jsonc=rendered
        )
        self.assertIn(weird_name, cfg.servers)

    def test_startup_timeout_sec_rejects_nan_and_infinities(self):
        mcp_nan = """{
  "version": 1,
  "servers": {
    "srv": {
      "command": "echo",
      "startup_timeout_sec": NaN
    }
  }
}"""
        with self.assertRaises(SourceError) as ctx:
            load_source_texts(
                config_toml="version = 1\n", valid_tool_ids=self.adapter_ids, mcp_jsonc=mcp_nan
            )
        self.assertIn(
            "'startup_timeout_sec' in server 'srv' must be a positive number",
            str(ctx.exception),
        )

        mcp_inf = """{
  "version": 1,
  "servers": {
    "srv": {
      "command": "echo",
      "startup_timeout_sec": Infinity
    }
  }
}"""
        with self.assertRaises(SourceError) as ctx:
            load_source_texts(
                config_toml="version = 1\n", valid_tool_ids=self.adapter_ids, mcp_jsonc=mcp_inf
            )
        self.assertIn(
            "'startup_timeout_sec' in server 'srv' must be a positive number",
            str(ctx.exception),
        )

    def test_unknown_field_and_setting_masks_secret_looking_key(self):
        secret_key = "ghp_" + "1" * 20
        toml_text = f"version = 1\n[tools.example]\nenabled = true\n{secret_key} = true\n"
        with self.assertRaises(SourceError) as ctx:
            load_source_texts(toml_text, valid_tool_ids=("example",))
        self.assertNotIn(secret_key, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

        mcp_text = f"""{{
  "version": 1,
  "servers": {{
    "srv": {{
      "command": "echo",
      "{secret_key}": "val"
    }}
  }}
}}"""
        with self.assertRaises(SourceError) as ctx:
            load_source_texts(
                config_toml="version = 1\n", valid_tool_ids=self.adapter_ids, mcp_jsonc=mcp_text
            )
        self.assertNotIn(secret_key, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_server_env_and_env_vars_clash_raises_clean_source_error(self):
        mcp_text = """{
  "version": 1,
  "servers": {
    "srv": {
      "command": "echo",
      "env": {
        "SHARED_VAR": "non_secret_value"
      },
      "env_vars": [
        "SHARED_VAR"
      ]
    }
  }
}"""
        with self.assertRaises(SourceError) as ctx:
            load_source_texts(
                config_toml="version = 1\n", valid_tool_ids=self.adapter_ids, mcp_jsonc=mcp_text
            )
        self.assertEqual(str(ctx.exception), "server 'srv' is invalid")
        self.assertNotIn("non_secret_value", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
