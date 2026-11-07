"""Focused tests of the Claude Code adapter (the generic contract suite is in test_adapter_contract).

Everything runs in a temporary home and applies ops with the real engine (the same seam as the
contract suite). `claude mcp ...` is an in-process fake (`claude_support.FakeClaudeCli`); no
program is ever started. The platform is always passed explicitly, so the path logic is tested the
same way on Windows, Linux and macOS. Names are neutral; the one secret-looking string is a fake.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
import contract_applier
from claude_support import (
    FIXTURE,
    FakeClaudeCli,
    canonical_bytes,
    make_fake_binary,
    seed_claude_home,
)
from one_setup_all_agents.adapters import get_adapter
from one_setup_all_agents.adapters._stub import StubAdapter
from one_setup_all_agents.adapters.claude_code import (
    BIN_ENV,
    ClaudeCodeAdapter,
    ConfigUnreadable,
    decode_server,
    encode_server,
    find_cli,
)
from one_setup_all_agents.engine import fsops, journal
from one_setup_all_agents.engine.errors import ApplyError
from one_setup_all_agents.model import (
    INSTRUCTIONS_KEY,
    Conflict,
    EditFile,
    Env,
    HeaderRef,
    Kind,
    Link,
    McpServer,
    Plan,
    RunCli,
    Scope,
    SkillRef,
    Unsupported,
)
from one_setup_all_agents.paths import detect_platform

G = Scope.GLOBAL
HOST = detect_platform()
ABSOLUTE = {"windows": "C:\\claude-demo", "linux": "/claude-demo", "macos": "/claude-demo"}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osaa-claude-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.adapter = ClaudeCodeAdapter()
        self.binary = make_fake_binary(self.root / "bin")
        self.env = self.make_env()
        for directory in (self.env.home, self.env.config_dir, self.env.state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.cli = FakeClaudeCli(self.state_file)
        self.applier = contract_applier.make_applier(self.env, self.cli)

    def make_env(self, platform=HOST, environ=None, with_cli=True):
        """An Env in the temp dir. By default the fake binary is found through the override."""
        environ = dict(environ or {})
        if with_cli:
            environ.setdefault(BIN_ENV, str(self.binary))
        return Env(
            home=self.root / "home",
            platform=platform,
            environ=environ,
            config_dir=self.root / "config",
            state_dir=self.root / "state",
        )

    @property
    def state_file(self):
        return self.adapter.paths(self.env, G).mcp

    def owned(self, kind):
        return frozenset(journal.owned_for(self.env.state_dir, self.adapter.id, kind.value))

    def plan_mcp(self, desired, owned=None, env=None):
        owned = self.owned(Kind.MCP) if owned is None else frozenset(owned)
        return self.adapter.plan_mcp(env or self.env, G, desired, owned)

    def sync_mcp(self, desired):
        plan = Plan(self.plan_mcp(desired))
        self.applier.apply(plan)
        return plan

    def write_state(self, doc):
        data = doc if isinstance(doc, bytes) else canonical_bytes(doc)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_bytes(data)

    def doc(self):
        return json.loads(self.state_file.read_text("utf-8"))

    def servers(self):
        return self.doc().get("mcpServers", {})

    @staticmethod
    def stdio(name="demo-server", **kw):
        return McpServer(name=name, command=kw.pop("command", "demo-cmd"), **kw)

    @staticmethod
    def clis(ops):
        return [op for op in ops if isinstance(op, RunCli)]

    @staticmethod
    def entry_of(op):
        """The JSON entry in an `add-json` op."""
        return json.loads(op.argv[4])


# -- registry ------------------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_claude_is_a_real_adapter_and_two_tools_are_still_stubs(self):
        self.assertNotIsInstance(get_adapter("claude"), StubAdapter)
        for tool in ("codex", "agy"):
            self.assertIsInstance(get_adapter(tool), StubAdapter, tool)


# -- paths ---------------------------------------------------------------------------------------


class PathTests(Base):
    def test_default_locations_on_every_platform(self):
        for platform in ("windows", "linux", "macos"):
            with self.subTest(platform=platform):
                env = self.make_env(platform)
                paths = self.adapter.paths(env, G)
                self.assertEqual(paths.instructions, env.home / ".claude" / "CLAUDE.md")
                self.assertEqual(paths.skills_dir, env.home / ".claude" / "skills")
                self.assertEqual(paths.mcp, env.home / ".claude.json")

    def test_an_absolute_claude_config_dir_moves_all_three_including_claude_json(self):
        for platform, value in ABSOLUTE.items():
            with self.subTest(platform=platform):
                env = self.make_env(platform, {"CLAUDE_CONFIG_DIR": value})
                paths = self.adapter.paths(env, G)
                self.assertEqual(paths.instructions, Path(value) / "CLAUDE.md")
                self.assertEqual(paths.skills_dir, Path(value) / "skills")
                self.assertEqual(paths.mcp, Path(value) / ".claude.json")

    def test_a_relative_or_foreign_style_value_is_ignored_and_detect_warns(self):
        foreign = {"windows": "/claude-demo", "linux": "C:\\claude-demo", "macos": "C:\\x"}
        for platform in ("windows", "linux", "macos"):
            for value in ("relative/dir", "~/claude", foreign[platform], ""):
                with self.subTest(platform=platform, value=value):
                    env = self.make_env(platform, {"CLAUDE_CONFIG_DIR": value})
                    self.assertEqual(
                        self.adapter.paths(env, G).mcp, env.home / ".claude.json", value
                    )
                    warned = [w for w in self.adapter.detect(env).warnings if "absolute" in w]
                    self.assertEqual(bool(warned), bool(value), value)

    def test_the_process_environment_is_never_read(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ABSOLUTE[HOST]}):
            paths = self.adapter.paths(self.make_env(), G)
        self.assertEqual(paths.instructions, self.env.home / ".claude" / "CLAUDE.md")

    def test_reading_and_planning_follow_the_moved_claude_json(self):
        alt = self.root / "alt-claude"
        env = self.make_env(HOST, {"CLAUDE_CONFIG_DIR": str(alt)})
        alt.mkdir()
        (alt / ".claude.json").write_bytes(
            canonical_bytes({"mcpServers": {"moved": {"type": "stdio", "command": "x"}}})
        )
        self.write_state({"mcpServers": {"in-home": {"type": "stdio", "command": "y"}}})
        self.assertEqual(set(self.adapter.read_mcp(env, G)), {"moved"})
        self.assertEqual(set(self.adapter.read_mcp(self.env, G)), {"in-home"})
        ops = self.adapter.plan_mcp(env, G, {"demo-server": self.stdio()}, frozenset())
        self.assertEqual(len(self.clis(ops)), 1)

    def test_detect_uses_the_given_environment_only(self):
        detection = self.adapter.detect(self.env)
        self.assertEqual(detection.config_roots, (self.env.home / ".claude",))
        self.assertEqual(detection.binary, self.binary)
        self.assertTrue(detection.installed)
        self.assertIsNone(detection.version)  # reading it would start the program
        bare = self.make_env(with_cli=False)
        self.assertIsNone(self.adapter.detect(bare).binary)
        self.assertFalse(self.adapter.detect(bare).installed)

    def test_detect_warns_when_configured_but_the_cli_is_missing(self):
        bare = self.make_env(with_cli=False)
        self.write_state({"numStartups": 1})
        detection = self.adapter.detect(bare)
        self.assertTrue(detection.installed)
        self.assertTrue(any("claude CLI was not found" in w for w in detection.warnings))

    def test_find_cli_override_path_and_a_typo(self):
        self.assertEqual(find_cli(self.make_env())[:2], (str(self.binary), self.binary))
        missing = self.make_env(environ={BIN_ENV: str(self.root / "nope")})
        argv0, binary, warning = find_cli(missing)
        self.assertIsNone(argv0)
        self.assertIsNone(binary)
        self.assertIn(BIN_ENV, warning)  # a typo is not replaced by an install found on PATH

    def test_find_cli_searches_the_path_of_the_given_environment(self):
        directory = self.root / "path-bin"
        suffix = ".cmd" if HOST == "windows" else ""
        found = make_fake_binary(directory, "claude" + suffix)
        env = self.make_env(environ={"PATH": str(directory)}, with_cli=False)
        argv0, binary, _ = find_cli(env)
        self.assertEqual(argv0, "claude")  # the bare name: the plan reads the same everywhere
        self.assertEqual(Path(binary).resolve(), found.resolve())

    def test_project_scope_is_unsupported_everywhere(self):
        for plan in (
            lambda: self.adapter.plan_instructions(self.env, Scope.PROJECT, "x", frozenset()),
            lambda: self.adapter.plan_mcp(self.env, Scope.PROJECT, {}, frozenset()),
            lambda: self.adapter.plan_skills(self.env, Scope.PROJECT, [], frozenset()),
        ):
            ops = plan()
            self.assertEqual([type(o) for o in ops], [Unsupported])
        self.assertEqual(self.adapter.paths(self.env, Scope.PROJECT).mcp, None)


# -- instructions ----------------------------------------------------------------------------------


class InstructionsTests(Base):
    def test_a_new_file_is_created_and_claimed(self):
        ops = self.adapter.plan_instructions(self.env, G, "# Hi\n", frozenset())
        self.assertEqual([type(o) for o in ops], [EditFile])
        self.assertEqual(ops[0].path, self.env.home / ".claude" / "CLAUDE.md")
        self.assertIsNone(ops[0].expect_sha256)
        self.assertEqual(ops[0].claims[0].key, INSTRUCTIONS_KEY)
        self.applier.apply(Plan(ops))
        self.assertEqual(self.adapter.read_instructions(self.env, G).text, "# Hi\n")

    def test_unowned_different_file_is_a_conflict_and_equal_file_is_left_alone(self):
        path = self.env.home / ".claude" / "CLAUDE.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"mine\n")
        ops = self.adapter.plan_instructions(self.env, G, "other\n", frozenset())
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(self.adapter.plan_instructions(self.env, G, "mine\n", frozenset()), [])

    def test_an_owned_file_is_updated_with_the_current_hash(self):
        self.applier.apply(Plan(self.adapter.plan_instructions(self.env, G, "v1\n", frozenset())))
        owned = frozenset({INSTRUCTIONS_KEY})
        ops = self.adapter.plan_instructions(self.env, G, "v2\n", owned)
        self.assertEqual([type(o) for o in ops], [EditFile])
        self.assertIsNotNone(ops[0].expect_sha256)
        self.applier.apply(Plan(ops))
        self.assertEqual(self.adapter.read_instructions(self.env, G).text, "v2\n")

    def test_the_file_moves_with_claude_config_dir(self):
        alt = self.root / "alt-claude"
        env = self.make_env(HOST, {"CLAUDE_CONFIG_DIR": str(alt)})
        ops = self.adapter.plan_instructions(env, G, "x\n", frozenset())
        self.assertEqual(ops[0].path, alt / "CLAUDE.md")


# -- MCP: format -----------------------------------------------------------------------------------


class McpFormatTests(Base):
    def test_encoding_of_local_and_remote_servers(self):
        local = McpServer(
            name="demo-server",
            command="demo-cmd",
            args=("--flag",),
            env_vars=("DEMO_TOKEN",),
            env=(("DEMO_MODE", "test"),),
        )
        self.assertEqual(
            encode_server(local),
            {
                "type": "stdio",
                "command": "demo-cmd",
                "args": ["--flag"],
                "env": {"DEMO_MODE": "test", "DEMO_TOKEN": "${DEMO_TOKEN}"},
            },
        )
        remote = McpServer(
            name="demo-remote",
            url="https://demo.example.test/mcp",
            headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"), HeaderRef("X-Key", "KEY")),
        )
        self.assertEqual(
            encode_server(remote),
            {
                "type": "http",
                "url": "https://demo.example.test/mcp",
                "headers": {"Authorization": "Bearer ${DEMO_TOKEN}", "X-Key": "${KEY}"},
            },
        )

    def test_secrets_are_referenced_by_name_and_no_value_is_written(self):
        server = self.stdio(env_vars=("DEMO_TOKEN",))
        os_value = "FAKE-VALUE-should-never-appear"
        with mock.patch.dict(os.environ, {"DEMO_TOKEN": os_value}):
            text = json.dumps(encode_server(server))
            ops = self.plan_mcp({"demo-server": server})
        self.assertIn("${DEMO_TOKEN}", text)
        self.assertNotIn(os_value, text)
        self.assertNotIn(os_value, json.dumps([list(op.argv) for op in self.clis(ops)]))
        with self.assertRaises(ValueError):  # the model refuses a secret-looking literal
            McpServer(name="x", command="c", env=(("DEMO_TOKEN", "abc123"),))

    def test_reading_never_copies_a_secret_literal_into_the_model(self):
        seed_claude_home(self.env)
        servers = self.adapter.read_mcp(self.env, G)
        self.assertEqual(servers["user-local"].env, ())
        self.assertEqual(servers["user-local"].env_vars, ())
        self.assertNotIn("FAKE-ENV-SECRET", repr(servers))

    def test_decode_handles_references_defaults_and_foreign_variable_names(self):
        entry = {
            "type": "stdio",
            "command": "demo-cmd",
            "args": ["a", 2],
            "env": {
                "SAME": "${SAME}",
                "OTHER": "${ELSEWHERE}",
                "WITH_DEFAULT": "${X:-fallback}",
                "MODE": "test",
                "DEMO_TOKEN": "literal-secret",
                "NUM": 5,
            },
        }
        server = decode_server("demo", entry)
        self.assertEqual(server.env_vars, ("SAME",))
        self.assertEqual(server.env, (("MODE", "test"),))
        self.assertEqual(server.args, ("a", "2"))

    def test_http_variants_are_read_as_http_and_websocket_is_not_read(self):
        self.write_state(
            {
                "mcpServers": {
                    "a": {"type": "sse", "url": "https://a.example.test/sse"},
                    "b": {"type": "streamable-http", "url": "https://b.example.test/mcp"},
                    "c": {"type": "ws", "url": "wss://c.example.test/socket"},
                    "d": {"url": "https://d.example.test/mcp"},
                    "e": {"command": "e-cmd"},
                    "f": "not an object",
                }
            }
        )
        read = self.adapter.read_mcp(self.env, G)
        self.assertEqual(set(read), {"a", "b", "d", "e"})
        self.assertEqual(read["a"].url, "https://a.example.test/sse")
        self.assertEqual(read["e"].command, "e-cmd")

    def test_a_literal_header_is_not_representable_and_is_skipped_on_read(self):
        server = decode_server(
            "demo",
            {"type": "http", "url": "https://x.example.test", "headers": {"X-A": "plain"}},
        )
        self.assertEqual(server.headers, ())

    def test_normalize_mcp_names_exactly_what_does_not_survive(self):
        full = McpServer(
            name="demo-server",
            command="npx.cmd",
            args=("-y",),
            env_vars=("A",),
            env=(("M", "1"),),
            cwd="work",
            startup_timeout_sec=5,
            auth="oauth",
            enabled=False,
        )
        out = self.adapter.normalize_mcp({"demo-server": full})["demo-server"]
        self.assertEqual(out.command, "npx")  # the .cmd suffix is not part of the neutral view
        self.assertEqual((out.args, out.env_vars, out.env), (("-y",), ("A",), (("M", "1"),)))
        self.assertEqual(
            (out.cwd, out.startup_timeout_sec, out.auth, out.enabled), (None, None, None, True)
        )
        remote = McpServer(
            name="r",
            url="https://r.example.test",
            command="c",
            args=("x",),
            env=(("M", "1"),),
            env_vars=("A",),
            headers=(HeaderRef("Authorization", "T", "Bearer"),),
        )
        out = self.adapter.normalize_mcp({"r": remote})["r"]
        self.assertEqual((out.command, out.args, out.env, out.env_vars), (None, (), (), ()))
        self.assertEqual(out.headers, remote.headers)
        stdio = self.stdio(headers=(HeaderRef("A", "B"),))
        self.assertEqual(self.adapter.normalize_mcp({"s": stdio})["s"].headers, ())


# -- MCP: planning -----------------------------------------------------------------------------------


class McpPlanTests(Base):
    def test_a_new_server_is_one_add_json_call_scoped_to_user_with_an_inverse(self):
        ops = self.plan_mcp({"demo-server": self.stdio(args=("--x",))})
        self.assertEqual([type(o) for o in ops], [RunCli])
        op = ops[0]
        self.assertEqual(op.argv[:4], [str(self.binary), "mcp", "add-json", "demo-server"])
        self.assertEqual(op.argv[5:], ["-s", "user"])
        self.assertEqual(
            self.entry_of(op), {"type": "stdio", "command": "demo-cmd", "args": ["--x"]}
        )
        self.assertEqual(
            op.inverse_argv, [str(self.binary), "mcp", "remove", "demo-server", "-s", "user"]
        )
        self.assertEqual([r.key for r in op.claims], ["demo-server"])
        self.assertEqual(op.releases, ())

    def test_the_plan_never_edits_claude_json_and_planning_does_not_touch_it(self):
        seed_claude_home(self.env)
        before = self.state_file.read_bytes()
        ops = self.plan_mcp({"demo-server": self.stdio(), "gone": self.stdio("gone")})
        self.assertFalse([o for o in ops if isinstance(o, EditFile)])
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_the_bare_name_is_used_when_the_cli_comes_from_path(self):
        directory = self.root / "path-bin"
        make_fake_binary(directory, "claude" + (".cmd" if HOST == "windows" else ""))
        env = self.make_env(environ={"PATH": str(directory)}, with_cli=False)
        ops = self.plan_mcp({"demo-server": self.stdio()}, env=env)
        self.assertEqual(ops[0].argv[:3], ["claude", "mcp", "add-json"])

    def test_round_trip_read_then_plan_gives_no_ops(self):
        desired = {
            "demo-server": self.stdio(env_vars=("DEMO_TOKEN",), env=(("MODE", "x"),)),
            "demo-remote": McpServer(
                name="demo-remote",
                url="https://demo.example.test/mcp",
                headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),),
            ),
        }
        self.sync_mcp(desired)
        self.assertEqual(self.plan_mcp(desired), [])
        read = self.adapter.read_mcp(self.env, G)
        self.assertEqual(read, self.adapter.normalize_mcp(desired))
        self.assertEqual(self.plan_mcp(read), [])  # what was read plans nothing (owned or not)
        self.assertEqual(self.plan_mcp(read, owned=()), [])

    def test_other_bytes_of_claude_json_and_project_servers_survive_apply_and_undo(self):
        seed_claude_home(self.env)
        original = self.state_file.read_bytes()
        project_before = self.doc()["projects"]
        plan = Plan(self.plan_mcp({"demo-server": self.stdio()}))
        receipt = self.applier.apply(plan)
        after = self.doc()
        self.assertEqual(after["projects"], project_before)
        self.assertEqual((after["theme"], after["numStartups"]), ("dark", 12))
        self.assertIn("demo-server", after["mcpServers"])
        self.assertTrue(all(call[-2:] == ["-s", "user"] for call in self.cli.calls))
        self.applier.undo(receipt)
        self.assertEqual(self.state_file.read_bytes(), original)

    def test_a_same_named_project_server_is_not_a_user_server(self):
        self.write_state(
            {"projects": {"/work/demo": {"mcpServers": {"demo-server": {"command": "p"}}}}}
        )
        ops = self.plan_mcp({"demo-server": self.stdio()})
        self.assertEqual([type(o) for o in ops], [RunCli])  # no conflict with the project entry

    def test_updating_an_owned_entry_is_remove_then_add_and_keeps_keys_we_do_not_own(self):
        self.sync_mcp({"demo-server": self.stdio()})
        doc = self.doc()
        doc["mcpServers"]["demo-server"]["oauth"] = {"scopes": "read"}
        doc["mcpServers"]["demo-server"]["env"] = {"STALE": "1"}
        self.write_state(doc)
        ops = self.plan_mcp({"demo-server": self.stdio(args=("--new",))})
        self.assertEqual([op.argv[2] for op in ops], ["remove", "add-json"])
        remove, add = ops
        self.assertEqual(remove.releases, ())
        self.assertEqual([r.key for r in add.claims], ["demo-server"])
        self.assertEqual(json.loads(remove.inverse_argv[4]), doc["mcpServers"]["demo-server"])
        new = self.entry_of(add)
        self.assertEqual(new["oauth"], {"scopes": "read"})  # not ours: kept
        self.assertEqual(new["args"], ["--new"])
        self.assertNotIn("env", new)  # ours and no longer wanted
        self.applier.apply(Plan(ops))
        self.assertEqual(self.servers()["demo-server"], new)
        self.assertEqual(self.plan_mcp({"demo-server": self.stdio(args=("--new",))}), [])

    def test_a_failed_add_after_the_remove_puts_the_old_entry_back(self):
        self.sync_mcp({"demo-server": self.stdio()})
        before = self.state_file.read_bytes()
        plan = Plan(self.plan_mcp({"demo-server": self.stdio(args=("--new",))}))
        self.cli.fail_if = lambda argv: argv[2] == "add-json" and "--new" in argv[4]
        with self.assertRaises(ApplyError) as ctx:
            self.applier.apply(plan)
        self.assertTrue(ctx.exception.reverted)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(self.owned(Kind.MCP), {"demo-server"})

    def test_switching_a_server_from_stdio_to_http_drops_only_our_keys(self):
        self.sync_mcp({"demo-server": self.stdio(env_vars=("T",))})
        remote = McpServer(name="demo-server", url="https://x.example.test/mcp")
        add = self.plan_mcp({"demo-server": remote})[1]
        self.assertEqual(self.entry_of(add), {"type": "http", "url": "https://x.example.test/mcp"})

    def test_unowned_differing_entry_is_a_conflict_and_nothing_runs(self):
        seed_claude_home(self.env)
        wanted = {"user-local": self.stdio("user-local", command="other-cmd")}
        ops = self.plan_mcp(wanted)
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(ops[0].path, self.state_file)
        self.assertFalse(Plan(ops).is_empty)
        self.assertEqual(Plan(ops).executable, [])

    def test_an_equal_unowned_entry_is_left_alone_even_with_other_bytes(self):
        # no "type", extra keys, a .cmd launcher shim and defaults left out: same meaning
        self.write_state(
            {
                "mcpServers": {
                    "demo-server": {
                        "command": "npx.cmd",
                        "args": ["-y", "pkg"],
                        "timeout": 5,
                        "disabled": True,
                    }
                }
            }
        )
        wanted = {"demo-server": self.stdio(command="npx", args=("-y", "pkg"))}
        self.assertEqual(self.plan_mcp(wanted), [])
        self.assertEqual(self.plan_mcp(wanted, owned=("demo-server",)), [])

    def test_only_owned_entries_are_removed_and_the_inverse_re_adds_the_old_entry(self):
        seed_claude_home(self.env)
        self.sync_mcp({"demo-server": self.stdio(), "demo-two": self.stdio("demo-two")})
        old = self.servers()["demo-two"]
        ops = self.plan_mcp({"demo-server": self.stdio()})
        self.assertEqual([type(o) for o in ops], [RunCli])
        self.assertEqual(ops[0].argv[2:4], ["remove", "demo-two"])
        self.assertEqual([r.key for r in ops[0].releases], ["demo-two"])
        self.assertEqual(json.loads(ops[0].inverse_argv[4]), old)
        self.assertEqual(ops[0].inverse_argv[2:4], ["add-json", "demo-two"])
        self.applier.apply(Plan(ops))
        self.assertEqual(set(self.servers()), {"user-remote", "user-local", "demo-server"})

    def test_an_owned_entry_that_vanished_is_not_released_or_recreated_from_nothing(self):
        self.assertEqual(self.plan_mcp({}, owned=("gone",)), [])

    def test_a_non_object_entry_is_a_conflict_even_when_owned(self):
        self.write_state({"mcpServers": {"demo-server": "oops", "old": 3}})
        ops = self.plan_mcp({"demo-server": self.stdio()}, owned=("demo-server", "old"))
        self.assertEqual(
            {(type(o), o.key) for o in ops}, {(Conflict, "demo-server"), (Conflict, "old")}
        )

    def test_a_missing_cli_turns_each_change_into_a_conflict_and_nothing_else(self):
        bare = self.make_env(with_cli=False)
        seed_claude_home(self.env)
        wanted = {"demo-server": self.stdio(), "user-local": self.stdio("user-local", command="z")}
        ops = self.plan_mcp(wanted, env=bare)
        self.assertEqual([type(o) for o in ops], [Conflict, Conflict])
        self.assertEqual({o.key for o in ops}, {"demo-server", "user-local"})
        self.assertIn("claude CLI was not found", {o.key: o for o in ops}["demo-server"].reason)
        # nothing to change: no CLI needed, no conflict
        self.sync_mcp({"demo-server": self.stdio()})
        self.assertEqual(self.plan_mcp({"demo-server": self.stdio()}, env=bare), [])
        # the other kinds do not need the CLI
        self.assertEqual(
            [type(o) for o in self.adapter.plan_instructions(bare, G, "x\n", frozenset())],
            [EditFile],
        )

    def test_a_windows_cmd_shim_is_refused_for_characters_cmd_exe_would_mangle(self):
        shim = make_fake_binary(self.root / "shim", "claude.cmd")
        for platform in ("windows", "linux"):
            env = self.make_env(platform, {BIN_ENV: str(shim)})
            risky = McpServer(name="demo-remote", url="https://x.example.test/mcp?a=1&b=2")
            plain = McpServer(name="demo-plain", url="https://x.example.test/mcp")
            ops = self.adapter.plan_mcp(
                env, G, {"demo-remote": risky, "demo-plain": plain}, frozenset()
            )
            with self.subTest(platform=platform):
                if platform == "windows":
                    self.assertEqual({type(o) for o in ops}, {Conflict})
                    self.assertIn(".cmd shim", ops[0].reason)
                else:  # the quirk is Windows-only
                    self.assertEqual({type(o) for o in ops}, {RunCli})
        exe = make_fake_binary(self.root / "exe", "claude.exe")
        env = self.make_env("windows", {BIN_ENV: str(exe)})
        ops = self.adapter.plan_mcp(env, G, {"demo-remote": risky}, frozenset())
        self.assertEqual([type(o) for o in ops], [RunCli])

    def test_names_the_cli_would_reject_are_skipped_with_a_reason(self):
        bad = {n: self.stdio(n) for n in ("with.dot", "-dash", "sp ace")}
        bad["fine_name-1"] = self.stdio("fine_name-1")
        ops = self.plan_mcp(bad)
        self.assertEqual([o.argv[3] for o in self.clis(ops)], ["fine_name-1"])
        gap = [o for o in ops if isinstance(o, Unsupported)][0]
        for name in ("with.dot", "-dash", "sp ace"):
            self.assertIn(f"{name}: not written", gap.reason)


class McpWindowsLauncherTests(Base):
    def test_cmd_is_never_added_on_any_platform(self):
        for platform in ("windows", "linux", "macos"):
            with self.subTest(platform=platform):
                env = self.make_env(platform)
                server = self.stdio(command="npx", args=("-y", "pkg"))
                ops = self.adapter.plan_mcp(env, G, {"demo-server": server}, frozenset())
                self.assertEqual(self.entry_of(ops[0])["command"], "npx")

    def test_reading_strips_cmd_and_an_existing_npx_cmd_is_not_rewritten(self):
        self.write_state(
            {"mcpServers": {"demo-server": {"type": "stdio", "command": "npx.cmd", "args": ["-y"]}}}
        )
        self.assertEqual(self.adapter.read_mcp(self.env, G)["demo-server"].command, "npx")
        wanted = {"demo-server": self.stdio(command="npx", args=("-y",))}
        for platform in ("windows", "linux"):
            env = self.make_env(platform)
            self.assertEqual(self.adapter.plan_mcp(env, G, wanted, frozenset({"demo-server"})), [])

    def test_only_the_known_launchers_lose_cmd(self):
        for command, stripped in (
            ("pnpm.CMD", "pnpm"),
            ("yarn.cmd", "yarn"),
            ("tool.cmd", "tool.cmd"),
        ):
            self.assertEqual(decode_server("x", {"command": command}).command, stripped)


class McpUnsupportedTests(Base):
    def gap(self, server):
        ops = self.plan_mcp({server.name: server})
        gaps = [o for o in ops if isinstance(o, Unsupported)]
        return ops, (gaps[0].reason if gaps else None)

    def test_cwd_timeout_and_disabled_are_reported_not_silently_dropped(self):
        server = self.stdio(cwd="work", startup_timeout_sec=30, enabled=False)
        ops, reason = self.gap(server)
        for word in ("cwd", "startup_timeout_sec", "enabled=false"):
            self.assertIn(word, reason)
        entry = self.entry_of(self.clis(ops)[0])
        self.assertNotIn("cwd", entry)
        self.assertNotIn("disabled", entry)  # written enabled; the plan says so

    def test_remote_server_fields_with_no_place_are_reported(self):
        server = McpServer(
            name="demo-remote", url="https://x.example.test", command="c", args=("a",), cwd="w"
        )
        _ops, reason = self.gap(server)
        for word in ("command", "args", "cwd"):
            self.assertIn(word, reason)

    def test_headers_on_a_local_server_are_reported(self):
        server = self.stdio(headers=(HeaderRef("A", "B"),))
        self.assertIn("headers", self.gap(server)[1])

    def test_auth_is_documentation_only_and_a_clean_server_has_no_gap(self):
        server = McpServer(name="demo-remote", url="https://x.example.test", auth="oauth")
        ops, reason = self.gap(server)
        self.assertIsNone(reason)
        self.assertNotIn("oauth", json.dumps(self.entry_of(ops[0])))

    def test_servers_that_cannot_be_written_at_all_are_skipped_with_a_reason(self):
        cases = {
            "empty": McpServer(name="empty"),
            "badvar": self.stdio("badvar", env_vars=("1BAD",)),
            "badhdr": McpServer(name="badhdr", url="https://x", headers=(HeaderRef("A B", "V"),)),
            "badscheme": McpServer(
                name="badscheme", url="https://x", headers=(HeaderRef("A", "V", "Bad Scheme"),)
            ),
            "expands": self.stdio("expands", env=(("MODE", "a${OTHER}b"),)),
        }
        ops = self.plan_mcp(cases)
        self.assertEqual(self.clis(ops), [])
        reason = [o for o in ops if isinstance(o, Unsupported)][0].reason
        for name in cases:
            self.assertIn(f"{name}: not written", reason)

    def test_a_server_that_became_unwritable_is_not_removed(self):
        self.sync_mcp({"demo-server": self.stdio()})
        ops = self.plan_mcp({"demo-server": McpServer(name="demo-server")})
        self.assertEqual(self.clis(ops), [])
        self.assertIn("demo-server", self.servers())


class UnreadableConfigTests(Base):
    def test_syntax_errors_are_refused_not_overwritten(self):
        self.write_state(b'{"mcpServers": {"x": ')
        with self.assertRaises(ConfigUnreadable):
            self.adapter.read_mcp(self.env, G)
        ops = self.plan_mcp({"demo-server": self.stdio()}, owned=("old",))
        self.assertEqual(
            {(type(o), o.key) for o in ops}, {(Conflict, "demo-server"), (Conflict, "old")}
        )
        self.assertEqual(Plan(ops).executable, [])
        self.assertEqual(self.state_file.read_bytes(), b'{"mcpServers": {"x": ')

    def test_a_wrong_shape_is_refused(self):
        for text in (b"[1, 2]", b'{"mcpServers": []}', b'{"mcpServers": 3}', b"\xff\xfe\x00"):
            with self.subTest(text=text):
                self.write_state(text)
                with self.assertRaises(ConfigUnreadable):
                    self.adapter.read_mcp(self.env, G)
                ops = self.plan_mcp({"demo-server": self.stdio()})
                self.assertEqual([type(o) for o in ops], [Conflict])

    def test_blank_and_absent_files_are_empty_and_a_bom_is_accepted(self):
        self.assertEqual(self.adapter.read_mcp(self.env, G), {})
        self.write_state(b"  \n")
        self.assertEqual(self.adapter.read_mcp(self.env, G), {})
        self.write_state(
            b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"a": {"command": "c"}}}).encode()
        )
        self.assertEqual(set(self.adapter.read_mcp(self.env, G)), {"a"})

    def test_a_torn_read_is_retried_once(self):
        good = canonical_bytes({"mcpServers": {"a": {"command": "c"}}})
        self.write_state(b'{"mcpServers": {"a"')
        reads = []
        real = Path.read_bytes

        def flaky(path):
            if path == self.state_file:
                reads.append(1)
                if len(reads) == 2:
                    self.state_file.write_bytes(good)  # the session finished writing
            return real(path)

        with mock.patch.object(Path, "read_bytes", flaky):
            self.assertEqual(set(self.adapter.read_mcp(self.env, G)), {"a"})
        self.assertEqual(len(reads), 2)

    def test_the_error_message_never_contains_file_content(self):
        self.write_state(b'{"mcpServers": {"secret-name": "FAKE-ENV-SECRET-zzz",}}')
        with self.assertRaises(ConfigUnreadable) as ctx:
            self.adapter.read_mcp(self.env, G)
        self.assertNotIn("FAKE-ENV-SECRET", str(ctx.exception))
        self.assertNotIn("secret-name", str(ctx.exception))


# -- skills ----------------------------------------------------------------------------------------


class SecretInOwnedEntryTests(Base):
    """An owned entry the user edited to hold a secret must not reach a command line or the journal
    (the old entry goes into `inverse_argv`, a patched copy into `argv`)."""

    VALUE = "FAKE-LITERAL-zq81"

    def own(self, entry):
        """Own `demo-server`, then replace its entry on disk with `entry` (as a user edit)."""
        self.sync_mcp({"demo-server": self.stdio()})
        doc = self.doc()
        doc["mcpServers"]["demo-server"] = entry
        self.write_state(doc)

    CASES = {
        "env literal": {"command": "demo-cmd", "env": {"API_TOKEN": "FAKE-LITERAL-zq81"}},
        "env literal, plain name": {"command": "demo-cmd", "env": {"PLAIN": "ghp_" + "a1" * 12}},
        "bearer header": {
            "type": "http",
            "url": "https://x.example.test/mcp",
            "headers": {"Authorization": "Bearer FAKE-LITERAL-zq81"},
        },
        "bare authorization header": {
            "type": "http",
            "url": "https://x.example.test/mcp",
            "headers": {"Authorization": "FAKE-LITERAL-zq81"},
        },
        "nested oauth": {
            "type": "stdio",
            "command": "demo-cmd",
            "oauth": {"clientId": "id", "nested": {"clientSecret": "FAKE-LITERAL-zq81"}},
        },
        "argument after a secret flag": {
            "command": "demo-cmd",
            "args": ["--token", "FAKE-LITERAL-zq81"],
        },
    }

    def check_refused(self, ops, label):
        self.assertEqual([type(o) for o in ops], [Conflict], label)
        reason = ops[0].reason
        self.assertIn("environment variable", reason)
        self.assertIn("command line", reason)
        self.assertNotIn(self.VALUE, reason)
        self.assertNotIn(self.VALUE, repr(ops))
        self.assertEqual(Plan(ops).executable, [])

    def test_replace_is_refused_when_the_old_entry_holds_a_secret(self):
        for label, entry in self.CASES.items():
            with self.subTest(label):
                self.setUp()
                self.own(entry)
                ops = self.plan_mcp({"demo-server": self.stdio(args=("--new",))})
                self.check_refused(ops, label)

    def test_remove_is_refused_when_the_old_entry_holds_a_secret(self):
        for label, entry in self.CASES.items():
            with self.subTest(label):
                self.setUp()
                self.own(entry)
                self.check_refused(self.plan_mcp({}), label)

    def test_the_refusal_names_the_field_and_not_the_value(self):
        self.own(self.CASES["nested oauth"])
        reason = self.plan_mcp({})[0].reason
        self.assertIn("oauth.nested.clientSecret", reason)

    def test_references_are_names_not_secrets_and_still_allowed(self):
        entry = {
            "type": "http",
            "url": "https://x.example.test/mcp",
            "headers": {"Authorization": "Bearer ${API_TOKEN}", "X-Key": "${KEY}"},
            "oauth": {"clientSecret": "${CLIENT_SECRET}"},
            "env": {"API_TOKEN": "${API_TOKEN}", "MODE": "test"},
        }
        self.own(entry)
        replace = self.plan_mcp({"demo-server": self.stdio(args=("--new",))})
        self.assertEqual([op.argv[2] for op in replace], ["remove", "add-json"])
        remove = self.plan_mcp({})
        self.assertEqual([op.argv[2] for op in remove], ["remove"])
        self.assertEqual(json.loads(remove[0].inverse_argv[4]), entry)

    def test_an_unowned_entry_with_a_secret_is_a_conflict_for_its_own_reason(self):
        seed_claude_home(self.env)  # user-local holds a fake literal token
        ops = self.plan_mcp({"user-local": self.stdio("user-local", command="other")})
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertIn("not created by all-agents", ops[0].reason)


class SkillTests(Base):
    def source(self, name, variant=""):
        folder = self.env.config_dir / "skills" / f"{name}{variant}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\nDemo {name}.\n", "utf-8")
        return SkillRef(name=name, path=folder)

    @property
    def skills_dir(self):
        return self.adapter.paths(self.env, G).skills_dir

    def plan(self, desired, owned=None, env=None):
        owned = self.owned(Kind.SKILLS) if owned is None else frozenset(owned)
        return self.adapter.plan_skills(env or self.env, G, desired, owned)

    def sync(self, desired):
        ops = self.plan(desired)
        self.applier.apply(Plan(ops))
        return ops

    def test_links_go_straight_into_the_claude_skills_folder_and_are_claimed(self):
        skill = self.source("demo-skill-a")
        ops = self.plan([skill])
        self.assertEqual([type(o) for o in ops], [Link])
        self.assertEqual(ops[0].link, self.env.home / ".claude" / "skills" / "demo-skill-a")
        self.assertEqual(ops[0].target, skill.path)
        self.assertTrue(ops[0].allow_copy_fallback)
        self.sync([skill])
        self.assertTrue(fsops.same_target(self.skills_dir / "demo-skill-a", skill.path))
        self.assertEqual(self.owned(Kind.SKILLS), {"demo-skill-a"})
        self.assertEqual(self.plan([skill]), [])

    def test_the_folder_moves_with_claude_config_dir(self):
        alt = self.root / "alt-claude"
        env = self.make_env(HOST, {"CLAUDE_CONFIG_DIR": str(alt)})
        ops = self.plan([self.source("demo-skill-a")], env=env)
        self.assertEqual(ops[0].link, alt / "skills" / "demo-skill-a")

    def test_a_skill_installed_by_other_means_is_never_touched(self):
        seed_claude_home(self.env)
        user = self.skills_dir / "user-skill"
        before = (user / "SKILL.md").read_bytes()
        self.sync([self.source("demo-skill-a")])
        self.assertEqual({o.releases[0].key for o in self.plan([])}, {"demo-skill-a"})
        self.sync([])
        self.assertEqual((user / "SKILL.md").read_bytes(), before)
        self.assertFalse((self.skills_dir / "demo-skill-a").exists())

    def test_a_clash_with_a_skill_of_the_user_is_a_conflict(self):
        seed_claude_home(self.env)
        ops = self.plan([self.source("user-skill")])
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(ops[0].path, self.skills_dir / "user-skill")

    def test_a_link_from_an_earlier_tool_to_another_folder_is_a_conflict(self):
        # for example a junction made by the prototype to ~/.agents/skills/<name>
        old = self.source("demo-skill-a", "-old")
        self.skills_dir.mkdir(parents=True)
        fsops.create_dir_link(self.skills_dir / "demo-skill-a", old.path)
        new = self.source("demo-skill-a")
        self.assertEqual([type(o) for o in self.plan([new])], [Conflict])
        self.assertEqual(self.plan([SkillRef("demo-skill-a", old.path)]), [])

    def test_the_folders_claude_manages_are_never_listed_or_touched(self):
        seed_claude_home(self.env)
        for name in (".trash", "synced"):
            (self.skills_dir / name).mkdir()
            (self.skills_dir / name / "x.md").write_text("keep\n", "utf-8")
        self.assertEqual([s.name for s in self.adapter.read_skills(self.env, G)], ["user-skill"])
        self.assertEqual([type(o) for o in self.plan([self.source("synced")])], [Conflict])
        self.assertEqual(self.plan([]), [])

    def test_withdrawal_releases_and_leaves_an_unowned_link_alone(self):
        self.sync([self.source("demo-skill-a")])
        elsewhere = self.source("elsewhere")
        fsops.create_dir_link(self.skills_dir / "elsewhere", elsewhere.path)
        ops = self.plan([])
        self.assertEqual(len(ops), 1)
        self.assertIsNone(ops[0].target)
        self.applier.apply(Plan(ops))
        self.assertTrue((self.skills_dir / "elsewhere").exists())
        self.assertEqual(self.owned(Kind.SKILLS), set())

    def test_a_missing_source_or_a_bad_name_is_a_conflict(self):
        bad = self.source("x")
        missing = SkillRef("gone", self.env.config_dir / "skills" / "gone")
        ops = self.plan([missing, SkillRef("a/b", bad.path)])
        self.assertEqual([type(o) for o in ops], [Conflict] * 2)

    def test_read_skills_marks_links(self):
        seed_claude_home(self.env)
        self.sync([self.source("demo-skill-a")])
        read = {s.name: s for s in self.adapter.read_skills(self.env, G)}
        self.assertEqual(set(read), {"user-skill", "demo-skill-a"})
        self.assertFalse(read["user-skill"].is_link)
        self.assertTrue(read["demo-skill-a"].is_link)


class NotesAndFixtureTests(unittest.TestCase):
    def test_declares_the_three_v01_kinds_and_states_its_limits(self):
        adapter = ClaudeCodeAdapter()
        self.assertEqual(adapter.capabilities, {Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})
        text = " ".join(adapter.notes())
        for word in (
            "claude mcp add-json",
            BIN_ENV,
            "${NAME}",
            "CLAUDE_CONFIG_DIR",
            ".cmd",
            "RunCli has no drift check",
            "unverified",
        ):
            self.assertIn(word, text)

    def test_the_fixture_is_neutral_json(self):
        doc = json.loads(FIXTURE.read_text("utf-8"))
        self.assertIn("mcpServers", doc)
        self.assertIn("projects", doc)


if __name__ == "__main__":
    unittest.main()
