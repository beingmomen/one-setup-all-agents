"""Focused tests of the Codex adapter (the generic contract suite is in test_adapter_contract).

Everything runs in a temporary home and applies ops with the real engine (the same seam as the
contract suite). The platform is always passed explicitly, so the path logic is tested the same
way on Windows, Linux and macOS. Names are neutral; the one secret-looking string is a fake.
"""

import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
import contract_applier
from codex_support import FIXTURE, seed_codex_home
from engine_support import can_make_dir_link
from one_setup_all_agents._core import toml_edit
from one_setup_all_agents.adapters import get_adapter
from one_setup_all_agents.adapters.codex import (
    CodexAdapter,
    ConfigUnreadable,
    decode_server,
    encode_server,
)
from one_setup_all_agents.engine import journal
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
    Scope,
    SkillRef,
    Unsupported,
)

G = Scope.GLOBAL
HOST = "windows" if sys.platform.startswith("win") else "linux"  # style of the temp paths
ABSOLUTE = {"windows": "C:\\codex-demo", "linux": "/codex-demo", "macos": "/codex-demo"}


def _links_work():
    probe = Path(tempfile.mkdtemp(prefix="osaa-probe-")).resolve()
    try:
        return can_make_dir_link(probe)
    finally:
        shutil.rmtree(probe, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osaa-codex-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.adapter = CodexAdapter()
        self.env = self.make_env()
        for directory in (self.env.home, self.env.config_dir, self.env.state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.applier = contract_applier.make_applier(self.env)

    def make_env(self, platform="linux", environ=None):
        return Env(
            home=self.root / "home",
            platform=platform,
            environ=environ or {},
            config_dir=self.root / "config",
            state_dir=self.root / "state",
        )

    @property
    def config_file(self):
        return self.adapter.paths(self.env, G).mcp

    def owned(self, kind):
        return frozenset(journal.owned_for(self.env.state_dir, self.adapter.id, kind.value))

    def plan_mcp(self, desired, owned=None, env=None):
        owned = self.owned(Kind.MCP) if owned is None else frozenset(owned)
        return self.adapter.plan_mcp(env or self.env, G, desired, owned)

    def sync_mcp(self, desired, env=None):
        plan = Plan(self.plan_mcp(desired, env=env))
        self.applier.apply(plan)
        return plan

    def write_config(self, text):
        path = self.config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def text(self):
        return self.config_file.read_text("utf-8")

    def doc(self):
        return tomllib.loads(self.text().lstrip("\ufeff"))

    @staticmethod
    def stdio(name="demo-server", **kw):
        return McpServer(name=name, command=kw.pop("command", "demo-cmd"), **kw)

    @staticmethod
    def remote(name="demo-remote", **kw):
        return McpServer(name=name, url=kw.pop("url", "https://demo.example.test/mcp"), **kw)

    @staticmethod
    def executable(ops):
        return [op for op in ops if isinstance(op, (EditFile, Link))]


class RegistryTests(unittest.TestCase):
    def test_codex_is_a_real_adapter(self):
        adapter = get_adapter("codex")
        self.assertIsInstance(adapter, CodexAdapter)
        self.assertEqual(adapter.capabilities, {Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})


# -- paths ---------------------------------------------------------------------------------------


class PathTests(Base):
    def test_default_location_is_home_dot_codex_on_every_platform(self):
        for platform in ("windows", "linux", "macos"):
            with self.subTest(platform=platform):
                env = self.make_env(platform)
                paths = self.adapter.paths(env, G)
                self.assertEqual(paths.instructions, env.home / ".codex" / "AGENTS.md")
                self.assertEqual(paths.mcp, env.home / ".codex" / "config.toml")
                self.assertEqual(paths.skills_dir, env.home / ".agents" / "skills")

    def test_an_absolute_codex_home_is_used_through_env_environ(self):
        for platform, value in ABSOLUTE.items():
            with self.subTest(platform=platform):
                env = self.make_env(platform, {"CODEX_HOME": value})
                paths = self.adapter.paths(env, G)
                self.assertEqual(paths.instructions, Path(value) / "AGENTS.md")
                self.assertEqual(paths.mcp, Path(value) / "config.toml")
                # skills do not follow CODEX_HOME: Codex reads ~/.agents/skills
                self.assertEqual(paths.skills_dir, env.home / ".agents" / "skills")
                self.assertEqual(self.adapter.detect(env).config_roots, (Path(value),))

    def test_a_relative_empty_or_foreign_style_value_is_ignored_and_warned_about(self):
        cases = [("linux", "relative/dir"), ("macos", "C:\\codex-demo"), ("windows", "/codex-demo")]
        for platform, value in cases:
            with self.subTest(platform=platform, value=value):
                env = self.make_env(platform, {"CODEX_HOME": value})
                self.assertEqual(
                    self.adapter.paths(env, G).mcp, env.home / ".codex" / "config.toml"
                )
                warnings = self.adapter.detect(env).warnings
                self.assertTrue(any("CODEX_HOME" in w for w in warnings), warnings)
        env = self.make_env("linux", {"CODEX_HOME": ""})
        self.assertEqual(self.adapter.paths(env, G).mcp, env.home / ".codex" / "config.toml")
        self.assertEqual(self.adapter.detect(env).warnings, ())

    def test_the_process_environment_is_never_read(self):
        import os

        old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.root / "leak")
        try:
            paths = self.adapter.paths(self.env, G)
        finally:
            if old is None:
                del os.environ["CODEX_HOME"]
            else:
                os.environ["CODEX_HOME"] = old
        self.assertEqual(paths.mcp.parent, self.env.home / ".codex")

    def test_a_missing_codex_home_folder_is_warned_about_because_codex_refuses_it(self):
        missing = self.root / "no-such-folder"
        env = self.make_env(HOST, {"CODEX_HOME": str(missing)})
        self.assertTrue(any("does not exist" in w for w in self.adapter.detect(env).warnings))
        missing.mkdir()
        self.assertEqual(self.adapter.detect(env).warnings, ())

    def test_detect_uses_the_given_path_and_config_dir_only(self):
        self.assertFalse(self.adapter.detect(self.env).installed)
        base = self.env.home / ".codex"
        base.mkdir(parents=True)
        found = self.adapter.detect(self.env)
        self.assertTrue(found.installed)
        self.assertEqual(found.config_roots, (base,))
        self.assertIsNone(found.version)  # detect never starts a program

    def test_a_codex_home_elsewhere_gets_its_own_files(self):
        elsewhere = self.root / "elsewhere"
        env = self.make_env(HOST, {"CODEX_HOME": str(elsewhere)})
        self.sync_mcp({"demo-server": self.stdio()}, env=env)
        self.assertTrue((elsewhere / "config.toml").is_file())
        self.assertFalse((self.env.home / ".codex").exists())

    def test_project_scope_is_unsupported_everywhere(self):
        self.assertEqual(self.adapter.paths(self.env, Scope.PROJECT).mcp, None)
        self.assertEqual(self.adapter.read_mcp(self.env, Scope.PROJECT), {})
        self.assertEqual(self.adapter.read_skills(self.env, Scope.PROJECT), [])
        self.assertIsNone(self.adapter.read_instructions(self.env, Scope.PROJECT))
        for kind, ops in (
            (
                Kind.INSTRUCTIONS,
                self.adapter.plan_instructions(self.env, Scope.PROJECT, "x", frozenset()),
            ),
            (Kind.MCP, self.adapter.plan_mcp(self.env, Scope.PROJECT, {}, frozenset())),
            (Kind.SKILLS, self.adapter.plan_skills(self.env, Scope.PROJECT, [], frozenset())),
        ):
            self.assertEqual([type(o) for o in ops], [Unsupported], kind)
            self.assertEqual(ops[0].kind, kind)


# -- instructions --------------------------------------------------------------------------------


class InstructionsTests(Base):
    def plan(self, text, owned=()):
        return self.adapter.plan_instructions(self.env, G, text, frozenset(owned))

    def test_a_new_file_is_created_and_claimed(self):
        (op,) = self.plan("# Demo\n")
        self.assertIsInstance(op, EditFile)
        self.assertEqual(op.path, self.env.home / ".codex" / "AGENTS.md")
        self.assertIsNone(op.expect_sha256)
        self.assertEqual(op.claims[0].key, INSTRUCTIONS_KEY)
        self.applier.apply(Plan([op]))
        self.assertEqual(self.adapter.read_instructions(self.env, G).text, "# Demo\n")
        self.assertEqual(self.plan("# Demo\n"), [])

    def test_unowned_different_file_is_a_conflict_and_equal_file_is_left_alone(self):
        path = self.adapter.paths(self.env, G).instructions
        path.parent.mkdir(parents=True)
        path.write_bytes(b"the user's own\n")
        (conflict,) = self.plan("# Demo\n")
        self.assertIsInstance(conflict, Conflict)
        self.assertEqual(self.plan("the user's own\n"), [])
        (op,) = self.plan("# Demo\n", owned={INSTRUCTIONS_KEY})
        self.assertIsInstance(op, EditFile)

    def test_a_non_empty_override_file_is_reported_and_never_touched(self):
        override = self.env.home / ".codex" / "AGENTS.override.md"
        override.parent.mkdir(parents=True)
        override.write_bytes(b"temporary override\n")
        ops = self.plan("# Demo\n")
        self.assertEqual([type(o) for o in ops], [EditFile, Conflict])
        self.assertEqual(ops[1].path, override)
        self.assertIn("instead of", ops[1].reason)
        self.assertTrue(
            any("AGENTS.override.md" in w for w in self.adapter.detect(self.env).warnings)
        )
        self.applier.apply(Plan(ops))
        self.assertEqual(override.read_bytes(), b"temporary override\n")
        self.assertEqual(len(self.plan("# Demo\n", owned={INSTRUCTIONS_KEY})), 1)  # still reported

    def test_an_empty_override_file_does_not_count(self):
        override = self.env.home / ".codex" / "AGENTS.override.md"
        override.parent.mkdir(parents=True)
        override.write_text("  \n", "utf-8")
        self.assertEqual([type(o) for o in self.plan("# Demo\n")], [EditFile])


# -- the format ----------------------------------------------------------------------------------


class McpFormatTests(Base):
    def test_encoding_of_local_remote_disabled_and_timeout(self):
        local = self.stdio(
            args=("--a",),
            env=(("DEMO_MODE", "test"),),
            env_vars=("DEMO_TOKEN",),
            cwd="work",
            startup_timeout_sec=60,
        )
        self.assertEqual(
            encode_server(local, windows=False),
            {
                "command": "demo-cmd",
                "args": ["--a"],
                "cwd": "work",
                "env_vars": ["DEMO_TOKEN"],
                "env": {"DEMO_MODE": "test"},
                "startup_timeout_sec": 60,
            },
        )
        remote = self.remote(
            headers=(
                HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),
                HeaderRef("X-Demo", "DEMO_HEADER"),
            ),
            auth="oauth",
            enabled=False,
        )
        self.assertEqual(
            encode_server(remote, windows=False),
            {
                "url": "https://demo.example.test/mcp",
                "bearer_token_env_var": "DEMO_TOKEN",
                "env_http_headers": {"X-Demo": "DEMO_HEADER"},
                "enabled": False,
            },
        )

    def test_secrets_are_referenced_by_name_and_no_value_is_written(self):
        servers = {
            "demo-server": self.stdio(env_vars=("DEMO_TOKEN",)),
            "demo-remote": self.remote(
                headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),)
            ),
        }
        self.sync_mcp(servers)
        text = self.text()
        self.assertIn('env_vars = ["DEMO_TOKEN"]', text)
        self.assertIn('bearer_token_env_var = "DEMO_TOKEN"', text)
        self.assertNotIn("Bearer", text)  # Codex adds the scheme itself
        self.assertNotIn("${", text)
        self.assertEqual(self.adapter.read_mcp(self.env, G), self.adapter.normalize_mcp(servers))

    def test_enabled_and_timeout_are_written_natively_and_not_reported_as_lost(self):
        servers = {"demo-off": self.stdio("demo-off", enabled=False, startup_timeout_sec=90)}
        ops = self.plan_mcp(servers)
        self.assertEqual([type(o) for o in ops], [EditFile])
        self.sync_mcp(servers)
        table = self.doc()["mcp_servers"]["demo-off"]
        self.assertIs(table["enabled"], False)
        self.assertEqual(table["startup_timeout_sec"], 90)
        read = self.adapter.read_mcp(self.env, G)["demo-off"]
        self.assertFalse(read.enabled)
        self.assertEqual(read.startup_timeout_sec, 90)

    def test_reading_never_copies_a_secret_literal_into_the_model(self):
        self.write_config(FIXTURE.read_text("utf-8"))
        read = self.adapter.read_mcp(self.env, G)
        self.assertEqual(set(read), {"user-remote", "user-local"})
        self.assertEqual(read["user-local"].env, ())
        self.assertNotIn("FAKE-ENV-SECRET", repr(read))
        # a bearer variable and a literal header: only the variable is representable
        self.assertEqual(
            read["user-remote"].headers, (HeaderRef("Authorization", "USER_TOKEN", "Bearer"),)
        )

    def test_decode_handles_float_timeouts_env_var_tables_and_cmd(self):
        server = decode_server(
            "x",
            {
                "command": "npx.cmd",
                "args": ["-y", 3, "pkg"],
                "env_vars": [
                    "A",
                    {"name": "B"},
                    {"name": "C", "source": "local"},
                    {"name": "D", "source": "remote"},
                ],
                "env": {"MODE": "test", "A": "shadowed", "SECRET_KEY": "FAKE-ENV-SECRET-aaa111"},
                "startup_timeout_sec": 20.0,
            },
        )
        self.assertEqual(server.command, "npx")
        self.assertEqual(server.args, ("-y", "pkg"))
        self.assertEqual(server.env_vars, ("A", "B", "C"))
        self.assertEqual(server.env, (("MODE", "test"),))
        self.assertEqual(server.startup_timeout_sec, 20)
        self.assertIsNone(
            decode_server("x", {"command": "c", "startup_timeout_sec": 1.5}).startup_timeout_sec
        )
        self.assertIsNone(
            decode_server("x", {"command": "c", "startup_timeout_sec": True}).startup_timeout_sec
        )

    def test_normalize_mcp_names_exactly_what_does_not_survive(self):
        full_local = self.stdio(
            command="npx",
            args=("a",),
            env=(("DEMO_MODE", "test"),),
            env_vars=("DEMO_TOKEN",),
            cwd="w",
            headers=(HeaderRef("X-Demo", "DEMO_HEADER"),),
            auth="oauth",
            startup_timeout_sec=5,
            enabled=False,
        )
        full_remote = self.remote(
            command="c",
            args=("a",),
            env=(("DEMO_MODE", "test"),),
            env_vars=("DEMO_TOKEN",),
            cwd="w",
            headers=(
                HeaderRef("X-Demo", "DEMO_HEADER"),
                HeaderRef("authorization", "DEMO_TOKEN", "bearer"),
            ),
            auth="none",
            startup_timeout_sec=5,
        )
        got = self.adapter.normalize_mcp({"l": full_local, "r": full_remote})
        self.assertEqual(
            got["l"],
            self.stdio(
                command="npx",
                args=("a",),
                env=(("DEMO_MODE", "test"),),
                env_vars=("DEMO_TOKEN",),
                cwd="w",
                startup_timeout_sec=5,
                enabled=False,
            ),
        )
        self.assertEqual(
            got["r"],
            self.remote(
                headers=(
                    HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),
                    HeaderRef("X-Demo", "DEMO_HEADER"),
                ),
                startup_timeout_sec=5,
            ),
        )

    def test_notes_state_the_limits(self):
        text = " ".join(self.adapter.notes())
        for word in ("AGENTS.override.md", "env_vars", ".cmd", "unverified", "enabled"):
            self.assertIn(word, text)


# -- planning MCP --------------------------------------------------------------------------------


class McpPlanTests(Base):
    def test_round_trip_read_then_plan_gives_no_ops(self):
        desired = {
            "demo-server": self.stdio(
                args=("--a",),
                env=(("DEMO_MODE", "test"),),
                env_vars=("DEMO_TOKEN",),
                cwd="w",
                startup_timeout_sec=30,
            ),
            "demo-remote": self.remote(
                headers=(
                    HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),
                    HeaderRef("X-Demo", "DEMO_HEADER"),
                )
            ),
            "demo-off": self.stdio("demo-off", enabled=False),
        }
        self.sync_mcp(desired)
        read = self.adapter.read_mcp(self.env, G)
        self.assertEqual(read, self.adapter.normalize_mcp(desired))
        self.assertEqual(self.plan_mcp(desired), [])
        self.assertEqual(self.plan_mcp(read), [])  # also what `read_mcp` returned, owned or not
        self.assertEqual(self.plan_mcp(read, owned=()), [])  # an equal unowned item is no conflict

    def test_a_new_file_holds_the_managed_region(self):
        self.sync_mcp({"demo-server": self.stdio()})
        text = self.text()
        self.assertTrue(text.startswith(toml_edit.BEGIN))
        self.assertTrue(text.rstrip().endswith(toml_edit.END))
        self.assertEqual(self.doc()["mcp_servers"]["demo-server"]["command"], "demo-cmd")
        self.assertEqual(self.owned(Kind.MCP), {"demo-server"})

    def test_comments_order_and_unrelated_tables_survive_add_update_remove_and_undo(self):
        path = self.write_config(FIXTURE.read_text("utf-8"))
        original = path.read_bytes()
        first = Plan(self.plan_mcp({"demo-server": self.stdio(args=("--a",))}))
        r1 = self.applier.apply(first)
        after_add = path.read_text("utf-8")
        # everything of the user's file is still there, in order, before the region
        self.assertTrue(after_add.startswith(original.decode("utf-8").rstrip("\n")))
        for needle in ("# keep this comment", "# The user's own Codex settings", "[tui]"):
            self.assertIn(needle, after_add)
        self.assertLess(after_add.index("[features]"), after_add.index("[projects."))
        self.assertLess(after_add.index("[tui]"), after_add.index(toml_edit.BEGIN))
        r2 = self.applier.apply(Plan(self.plan_mcp({"demo-server": self.stdio(args=("--b",))})))
        self.assertEqual(self.doc()["mcp_servers"]["demo-server"]["args"], ["--b"])
        self.assertIn("# keep this comment", self.text())
        r3 = self.applier.apply(Plan(self.plan_mcp({})))
        self.assertNotIn("demo-server", self.doc()["mcp_servers"])
        self.assertNotIn(toml_edit.BEGIN, self.text())  # an empty region is dropped
        self.assertEqual(set(self.doc()["mcp_servers"]), {"user-remote", "user-local"})
        for receipt in (r3, r2, r1):
            self.applier.undo(receipt)
        self.assertEqual(path.read_bytes(), original)

    def test_the_users_unrelated_values_are_unchanged_after_an_edit(self):
        self.write_config(FIXTURE.read_text("utf-8"))
        before = self.doc()
        self.sync_mcp({"demo-server": self.stdio()})
        after = self.doc()
        for key in before:
            if key != "mcp_servers":
                self.assertEqual(after[key], before[key], key)
        for name in before["mcp_servers"]:
            self.assertEqual(after["mcp_servers"][name], before["mcp_servers"][name], name)

    def test_updating_an_owned_table_keeps_the_keys_we_do_not_own(self):
        self.sync_mcp({"demo-server": self.stdio(startup_timeout_sec=10)})
        text = self.text().replace(
            "startup_timeout_sec = 10",
            "startup_timeout_sec = 10\ntool_timeout_sec = 90\nrequired = true",
        )
        self.write_config(text)
        self.sync_mcp({"demo-server": self.stdio(args=("--x",))})
        table = self.doc()["mcp_servers"]["demo-server"]
        self.assertEqual(table["tool_timeout_sec"], 90)
        self.assertIs(table["required"], True)
        self.assertEqual(table["args"], ["--x"])
        # the source says nothing about the timeout: "no opinion", the file's value stays
        self.assertEqual(table["startup_timeout_sec"], 10)
        self.assertEqual(self.plan_mcp({"demo-server": self.stdio(args=("--x",))}), [])

    def test_switching_a_server_from_local_to_remote_drops_only_our_keys(self):
        self.sync_mcp({"demo-server": self.stdio(args=("--a",), env_vars=("DEMO_TOKEN",))})
        self.sync_mcp({"demo-server": self.remote("demo-server")})
        table = self.doc()["mcp_servers"]["demo-server"]
        self.assertEqual(table, {"url": "https://demo.example.test/mcp"})

    def test_a_header_set_is_replaced_and_removed_with_the_server_definition(self):
        self.sync_mcp({"r": self.remote("r", headers=(HeaderRef("Authorization", "A", "Bearer"),))})
        self.sync_mcp({"r": self.remote("r", headers=(HeaderRef("X-Demo", "B"),))})
        table = self.doc()["mcp_servers"]["r"]
        self.assertNotIn("bearer_token_env_var", table)
        self.assertEqual(table["env_http_headers"], {"X-Demo": "B"})
        self.sync_mcp({"r": self.remote("r")})
        self.assertEqual(self.doc()["mcp_servers"]["r"], {"url": "https://demo.example.test/mcp"})

    def test_a_literal_http_headers_table_of_the_user_survives_an_update(self):
        self.write_config(
            '[mcp_servers.demo-remote]\nurl = "https://old.example.test/mcp"\n\n'
            '[mcp_servers.demo-remote.http_headers]\nX-Demo = "literal"\n'
        )
        ops = self.plan_mcp({"demo-remote": self.remote()}, owned={"demo-remote"})
        self.applier.apply(Plan(ops))
        table = self.doc()["mcp_servers"]["demo-remote"]
        self.assertEqual(table["url"], "https://demo.example.test/mcp")
        self.assertEqual(table["http_headers"], {"X-Demo": "literal"})

    def test_a_server_name_with_dots_is_quoted_and_round_trips(self):
        self.sync_mcp({"demo.server@1": self.stdio("demo.server@1")})
        self.assertIn('[mcp_servers."demo.server@1"]', self.text())
        self.assertEqual(self.plan_mcp({"demo.server@1": self.stdio("demo.server@1")}), [])
        self.sync_mcp({})
        self.assertNotIn("demo.server", self.text())

    def test_a_blank_existing_file_is_filled_in(self):
        self.write_config("\n  \n")
        self.sync_mcp({"demo-server": self.stdio()})
        self.assertIn("demo-server", self.doc()["mcp_servers"])

    def test_a_file_with_a_region_of_ours_gets_new_servers_inside_it(self):
        self.sync_mcp({"one": self.stdio("one")})
        self.write_config(self.text() + '\n[after]\nkey = "kept"\n')
        self.sync_mcp({"one": self.stdio("one"), "two": self.stdio("two")})
        text = self.text()
        self.assertEqual(text.count(toml_edit.BEGIN), 1)
        self.assertEqual(self.doc()["after"], {"key": "kept"})
        self.assertLess(text.index("mcp_servers.two"), text.index(toml_edit.END))

    def test_crlf_and_bom_are_preserved(self):
        original = FIXTURE.read_text("utf-8").replace("\n", "\r\n")
        path = self.write_config(b"\xef\xbb\xbf" + original.encode("utf-8"))
        self.sync_mcp({"demo-server": self.stdio()})
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "a bare LF crept in")
        self.assertIn("demo-server", tomllib.loads(data[3:].decode("utf-8")).get("mcp_servers"))

    def test_unowned_differing_entry_is_a_conflict_and_nothing_is_written(self):
        path = self.write_config(FIXTURE.read_text("utf-8"))
        before = path.read_bytes()
        ops = self.plan_mcp({"user-local": self.stdio("user-local", command="other")})
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(ops[0].key, "user-local")
        self.assertFalse(Plan(ops).is_empty)
        self.applier.apply(Plan(ops))
        self.assertEqual(path.read_bytes(), before)

    def test_an_equal_entry_of_the_user_is_left_alone_even_with_extra_keys(self):
        self.write_config(
            '[mcp_servers.demo-server]\ncommand = "demo-cmd"\nargs = []\ntool_timeout_sec = 5\n'
        )
        self.assertEqual(self.plan_mcp({"demo-server": self.stdio()}), [])
        self.assertEqual(self.owned(Kind.MCP), frozenset())

    def test_only_owned_entries_are_removed(self):
        self.write_config(FIXTURE.read_text("utf-8"))
        self.sync_mcp({"demo-server": self.stdio()})
        self.sync_mcp({})
        self.assertEqual(set(self.doc()["mcp_servers"]), {"user-remote", "user-local"})

    def test_an_owned_entry_that_vanished_is_not_released_or_recreated_from_nothing(self):
        self.sync_mcp({"demo-server": self.stdio()})
        self.write_config("")
        self.assertEqual(self.plan_mcp({}), [])

    def test_owned_equal_entries_in_the_users_own_position_are_not_moved(self):
        self.write_config(
            '[mcp_servers.demo-server]\ncommand = "demo-cmd"\nargs = []\n\n[tui]\nx = 1\n'
        )
        before = self.config_file.read_bytes()
        self.assertEqual(self.plan_mcp({"demo-server": self.stdio()}, owned={"demo-server"}), [])
        self.assertEqual(self.config_file.read_bytes(), before)


class McpWindowsShimTests(Base):
    def test_cmd_is_added_on_windows_only(self):
        server = {"demo-server": self.stdio(command="npx", args=("-y", "pkg"))}
        for platform, expected in (("windows", "npx.cmd"), ("linux", "npx"), ("macos", "npx")):
            with self.subTest(platform=platform):
                (op,) = self.executable(self.plan_mcp(server, env=self.make_env(platform)))
                text = op.transform(None).decode("utf-8")
                self.assertEqual(
                    tomllib.loads(text)["mcp_servers"]["demo-server"]["command"], expected
                )

    def test_other_commands_are_not_changed_on_windows(self):
        (op,) = self.executable(
            self.plan_mcp({"d": self.stdio("d", command="node")}, env=self.make_env("windows"))
        )
        self.assertIn('command = "node"', op.transform(None).decode("utf-8"))

    def test_reading_strips_cmd_and_an_existing_npx_is_not_rewritten_on_windows(self):
        env = self.make_env("windows")
        self.write_config('[mcp_servers.demo-server]\ncommand = "npx"\nargs = []\n')
        self.assertEqual(self.adapter.read_mcp(env, G)["demo-server"].command, "npx")
        self.assertEqual(
            self.plan_mcp(
                {"demo-server": self.stdio(command="npx")}, owned={"demo-server"}, env=env
            ),
            [],
        )
        self.write_config('[mcp_servers.demo-server]\ncommand = "npx.cmd"\nargs = []\n')
        self.assertEqual(self.adapter.read_mcp(env, G)["demo-server"].command, "npx")
        self.assertEqual(self.plan_mcp({"demo-server": self.stdio(command="npx")}, env=env), [])

    def test_round_trip_with_a_launcher_on_windows(self):
        env = self.make_env("windows")
        desired = {"demo-server": self.stdio(command="pnpm", args=("dlx", "pkg"))}
        self.sync_mcp(desired, env=env)
        self.assertEqual(self.adapter.read_mcp(env, G), self.adapter.normalize_mcp(desired))
        self.assertEqual(self.plan_mcp(desired, env=env), [])


class McpUnsupportedTests(Base):
    def gaps(self, ops):
        return [o for o in ops if isinstance(o, Unsupported)]

    def test_remote_server_fields_with_no_place_are_reported(self):
        server = self.remote(
            command="c", args=("a",), env=(("DEMO_MODE", "t"),), env_vars=("V",), cwd="w"
        )
        ops = self.plan_mcp({"demo-remote": server})
        (gap,) = self.gaps(ops)
        for field in ("command", "args", "env", "env_vars", "cwd"):
            self.assertIn(field, gap.reason)
        self.assertEqual(len(self.executable(ops)), 1)  # still written, without them

    def test_headers_on_a_local_server_are_reported(self):
        ops = self.plan_mcp({"d": self.stdio("d", headers=(HeaderRef("X-Demo", "V"),))})
        (gap,) = self.gaps(ops)
        self.assertIn("headers", gap.reason)

    def test_timeout_and_enabled_are_not_reported(self):
        ops = self.plan_mcp({"d": self.stdio("d", startup_timeout_sec=5, enabled=False)})
        self.assertEqual(self.gaps(ops), [])

    def test_a_header_codex_cannot_read_from_a_variable_is_not_written(self):
        cases = [
            HeaderRef("X-Demo", "V", "Bearer"),  # a scheme on a header other than Authorization
            HeaderRef("Authorization", "V", "Basic"),  # a scheme other than Bearer
            HeaderRef("Bad Name", "V"),
            HeaderRef("X-Demo", "bad-var"),
        ]
        for header in cases:
            with self.subTest(header=header):
                ops = self.plan_mcp({"r": self.remote("r", headers=(header,))})
                self.assertEqual(self.executable(ops), [])
                (gap,) = self.gaps(ops)
                self.assertIn("not written", gap.reason)
                self.assertNotIn("V\n", gap.reason)

    def test_servers_that_cannot_be_written_at_all_are_skipped_with_a_reason(self):
        cases = {
            "bad name": self.stdio("bad name"),
            "demo-empty": McpServer(name="demo-empty"),
            "demo-var": self.stdio("demo-var", env_vars=("1bad",)),
        }
        for name, server in cases.items():
            with self.subTest(name=name):
                ops = self.plan_mcp({name: server})
                self.assertEqual(self.executable(ops), [])
                self.assertIn("not written", self.gaps(ops)[0].reason)

    def test_a_server_that_became_unwritable_is_not_removed_from_the_file(self):
        self.sync_mcp({"demo-server": self.stdio()})
        ops = self.plan_mcp({"demo-server": McpServer(name="demo-server")})
        self.assertEqual(self.executable(ops), [])
        self.assertIn("demo-server", self.doc()["mcp_servers"])

    def test_import_notes_finds_secret_env_and_headers(self):
        self.write_config(
            '[mcp_servers.srv]\ncommand = "c"\n[mcp_servers.srv.env]\nTOKEN = "sk-placeholder-secret-token-val"\n'
        )
        notes = self.adapter.import_notes(self.env, G)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].server, "srv")
        self.assertEqual(notes[0].field, "env")
        self.assertEqual(notes[0].name, "TOKEN")


class UnreadableConfigTests(Base):
    def assert_refused(self, content, desired_names=("demo-server",)):
        path = self.write_config(content)
        before = path.read_bytes()
        ops = self.plan_mcp({n: self.stdio(n) for n in desired_names})
        self.assertEqual([type(o) for o in ops], [Conflict] * len(desired_names), content)
        self.assertTrue(all(o.kind is Kind.MCP and o.path == path and o.reason for o in ops))
        self.assertFalse(Plan(ops).is_empty)
        self.applier.apply(Plan(ops))
        self.assertEqual(path.read_bytes(), before)

    def test_syntax_errors_are_refused_not_overwritten(self):
        self.assert_refused("this is = = not toml")
        self.assert_refused("[unclosed\nkey = 1")
        self.assert_refused(b"\xff\xfe\x00bad bytes")
        with self.assertRaises(ConfigUnreadable):
            self.adapter.read_mcp(self.env, G)

    def test_a_wrong_shape_is_refused(self):
        self.assert_refused('mcp_servers = "oops"')
        with self.assertRaises(ConfigUnreadable):
            self.adapter.read_mcp(self.env, G)

    def test_every_desired_and_owned_name_gets_its_own_conflict(self):
        self.sync_mcp({"owned-one": self.stdio("owned-one")})
        self.write_config("= broken")
        ops = self.plan_mcp({"new-one": self.stdio("new-one")})
        self.assertEqual({o.key for o in ops}, {"owned-one", "new-one"})

    def test_an_unreadable_file_with_nothing_to_do_plans_nothing(self):
        self.write_config("= broken")
        self.assertEqual(self.plan_mcp({}), [])

    def test_the_error_message_never_contains_file_content(self):
        self.write_config('secret = "FAKE-ENV-SECRET-aaa111" = broken')
        with self.assertRaises(ConfigUnreadable) as ctx:
            self.adapter.read_mcp(self.env, G)
        self.assertNotIn("FAKE-ENV-SECRET", str(ctx.exception))
        (conflict,) = self.plan_mcp({"d": self.stdio("d")})
        self.assertNotIn("FAKE-ENV-SECRET", conflict.reason)


class UneditableLayoutTests(Base):
    """A file that parses but that the text edit cannot change safely is refused, not guessed at."""

    def assert_conflicts(self, content, desired, owned=()):
        path = self.write_config(content)
        before = path.read_bytes()
        ops = self.plan_mcp(desired, owned=owned)
        self.assertEqual(self.executable(ops), [], content)
        self.assertTrue(any(isinstance(o, Conflict) for o in ops), content)
        self.assertTrue(all(o.reason for o in ops if isinstance(o, Conflict)))
        self.applier.apply(Plan(ops))
        self.assertEqual(path.read_bytes(), before)

    def test_a_bare_mcp_servers_table(self):
        self.assert_conflicts(
            '[mcp_servers]\nuser = { command = "x" }\n', {"demo-server": self.stdio()}
        )

    def test_an_inline_table_definition_of_the_same_name_cannot_be_replaced(self):
        content = 'mcp_servers.demo-server = { command = "old" }\n'
        self.assert_conflicts(content, {"demo-server": self.stdio()}, owned={"demo-server"})

    def test_nested_tables_below_an_owned_server_are_not_rewritten_blindly(self):
        content = (
            '[mcp_servers.demo-server]\ncommand = "old"\n\n'
            '[mcp_servers.demo-server.tools.search]\napproval_mode = "approve"\n'
        )
        self.assert_conflicts(content, {"demo-server": self.stdio()}, owned={"demo-server"})

    def test_other_servers_are_still_reported_per_name(self):
        path = self.write_config('[mcp_servers]\nuser = { command = "x" }\n')
        ops = self.plan_mcp({"a": self.stdio("a"), "b": self.stdio("b")})
        self.assertEqual({o.key for o in ops if isinstance(o, Conflict)}, {"a", "b"})
        self.assertTrue(path.is_file())


# -- skills --------------------------------------------------------------------------------------


@unittest.skipUnless(_links_work(), "cannot make directory links")
class SkillTests(Base):
    def source(self, name, variant=""):
        folder = self.env.config_dir / "skills" / f"{name}{variant}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\nDemo {name}.\n", "utf-8")
        return SkillRef(name=name, path=folder)

    @property
    def skills_dir(self):
        return self.adapter.paths(self.env, G).skills_dir

    def plan(self, desired, owned=None):
        owned = self.owned(Kind.SKILLS) if owned is None else frozenset(owned)
        return self.adapter.plan_skills(self.env, G, desired, owned)

    def sync(self, desired):
        ops = self.plan(desired)
        self.applier.apply(Plan(ops))
        return ops

    def test_skills_go_to_the_shared_agents_folder_not_the_codex_home(self):
        ops = self.plan([self.source("demo-skill")])
        self.assertEqual(ops[0].link, self.env.home / ".agents" / "skills" / "demo-skill")
        self.assertTrue(ops[0].allow_copy_fallback)
        self.assertEqual(ops[0].claims[0].tool, "codex")
        elsewhere = self.make_env(HOST, {"CODEX_HOME": str(self.root / "elsewhere")})
        self.assertEqual(self.adapter.paths(elsewhere, G).skills_dir, self.skills_dir)

    def test_a_skill_installed_by_other_means_is_never_touched(self):
        seed_codex_home(self.env)
        self.sync([self.source("demo-skill")])
        self.sync([])
        self.assertTrue((self.skills_dir / "user-skill" / "SKILL.md").is_file())
        self.assertFalse((self.skills_dir / "demo-skill").exists())

    def test_a_clash_with_a_skill_of_the_user_is_a_conflict(self):
        seed_codex_home(self.env)
        (user,) = [self.source("user-skill")]
        (conflict,) = self.plan([user])
        self.assertIsInstance(conflict, Conflict)
        self.assertEqual(conflict.key, "user-skill")

    def test_ownership_follows_claims_and_releases(self):
        self.sync([self.source("demo-skill")])
        self.assertEqual(self.owned(Kind.SKILLS), {"demo-skill"})
        self.assertEqual(self.plan([self.source("demo-skill")]), [])
        (withdraw,) = self.plan([])
        self.assertIsNone(withdraw.target)
        self.assertEqual(withdraw.releases[0].key, "demo-skill")
        self.applier.apply(Plan([withdraw]))
        self.assertEqual(self.owned(Kind.SKILLS), frozenset())

    def test_an_equal_link_that_opencode_made_is_no_conflict_and_a_different_one_is(self):
        folder = self.source("demo-skill")
        self.skills_dir.mkdir(parents=True)
        from one_setup_all_agents.engine import fsops

        fsops.create_dir_link(self.skills_dir / "demo-skill", folder.path)
        self.assertEqual(self.plan([folder]), [])  # equal, owned by nobody: nothing to do
        other = self.source("demo-skill", "-other")
        (conflict,) = self.plan([SkillRef("demo-skill", other.path)])
        self.assertIsInstance(conflict, Conflict)

    def test_read_skills_lists_folders_and_marks_links(self):
        seed_codex_home(self.env)
        self.sync([self.source("demo-skill")])
        read = {s.name: s for s in self.adapter.read_skills(self.env, G)}
        self.assertEqual(set(read), {"user-skill", "demo-skill"})
        self.assertFalse(read["user-skill"].is_link)
        self.assertTrue(read["demo-skill"].is_link)

    def test_wanted_links_names_the_shared_paths_without_planning_anything(self):
        skill = self.source("demo-skill")
        (want,) = self.adapter.wanted_links(self.env, G, [skill, SkillRef("bad/name", skill.path)])
        self.assertEqual(want.link, self.skills_dir / "demo-skill")
        self.assertEqual(want.target, skill.path)
        self.assertEqual(want.ref.tool, "codex")
        self.assertEqual(self.adapter.wanted_links(self.env, Scope.PROJECT, [skill]), [])


if __name__ == "__main__":
    unittest.main()
