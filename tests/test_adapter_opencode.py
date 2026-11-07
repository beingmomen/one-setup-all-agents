"""Focused tests of the OpenCode adapter (the generic contract suite is in test_adapter_contract).

Everything runs in a temporary home and applies ops with the real engine (the same seam as the
contract suite). The platform is always passed explicitly, so the path logic is tested the same
way on Windows, Linux and macOS. Names are neutral; the one secret-looking string is a fake.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
import contract_applier
from engine_support import can_make_dir_link
from one_setup_all_agents._core import jsonc
from one_setup_all_agents.adapters import get_adapter
from one_setup_all_agents.adapters._stub import StubAdapter
from one_setup_all_agents.adapters.opencode import (
    ConfigUnreadable,
    OpenCodeAdapter,
    decode_server,
    encode_server,
)
from one_setup_all_agents.engine import fsops, journal
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
from opencode_support import FIXTURE, seed_opencode_home


def _links_work():
    probe = Path(tempfile.mkdtemp(prefix="osaa-probe-")).resolve()
    try:
        return can_make_dir_link(probe)
    finally:
        shutil.rmtree(probe, ignore_errors=True)


G = Scope.GLOBAL
ABSOLUTE = {"windows": "C:\\xdg-demo", "linux": "/xdg-demo", "macos": "/xdg-demo"}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osaa-opencode-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.adapter = OpenCodeAdapter()
        self.env = self.make_env()
        for directory in (self.env.home, self.env.config_dir, self.env.state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.applier = contract_applier.make_applier(self.env)

    def make_env(self, platform="linux", environ=None, root=None):
        root = root or self.root
        return Env(
            home=root / "home",
            platform=platform,
            environ=environ or {},
            config_dir=root / "config",
            state_dir=root / "state",
        )

    @property
    def config_file(self):
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

    def write_config(self, text, newline=None):
        path = self.config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8") if isinstance(text, str) else text
        path.write_bytes(data)
        return path

    def doc(self):
        return jsonc.loads(self.config_file.read_text("utf-8-sig"))

    @staticmethod
    def stdio(name="demo-server", **kw):
        return McpServer(name=name, command=kw.pop("command", "demo-cmd"), **kw)

    @staticmethod
    def executable(ops):
        return [op for op in ops if isinstance(op, (EditFile, Link))]


# -- registry ------------------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_opencode_is_a_real_adapter_and_the_other_three_are_still_stubs(self):
        self.assertNotIsInstance(get_adapter("opencode"), StubAdapter)
        for tool in ("claude", "codex", "agy"):
            self.assertIsInstance(get_adapter(tool), StubAdapter, tool)


# -- paths ---------------------------------------------------------------------------------------


class PathTests(Base):
    def test_default_location_is_home_dot_config_on_every_platform(self):
        for platform in ("windows", "linux", "macos"):
            with self.subTest(platform=platform):
                env = self.make_env(platform)
                paths = self.adapter.paths(env, G)
                base = env.home / ".config" / "opencode"
                self.assertEqual(paths.instructions, base / "AGENTS.md")
                self.assertEqual(paths.mcp, base / "opencode.jsonc")
                self.assertEqual(paths.skills_dir, env.home / ".agents" / "skills")

    def test_absolute_xdg_config_home_is_used_through_env_environ(self):
        for platform, value in ABSOLUTE.items():
            with self.subTest(platform=platform):
                env = self.make_env(platform, {"XDG_CONFIG_HOME": value})
                paths = self.adapter.paths(env, G)
                self.assertEqual(paths.instructions, Path(value) / "opencode" / "AGENTS.md")
                self.assertEqual(paths.mcp, Path(value) / "opencode" / "opencode.jsonc")
                # skills do not follow XDG: they live under ~/.agents
                self.assertEqual(paths.skills_dir, env.home / ".agents" / "skills")

    def test_a_relative_or_foreign_style_xdg_value_is_ignored(self):
        cases = [("linux", "relative/dir"), ("macos", "C:\\xdg-demo"), ("windows", "/xdg-demo")]
        for platform, value in cases:
            with self.subTest(platform=platform, value=value):
                env = self.make_env(platform, {"XDG_CONFIG_HOME": value})
                self.assertEqual(
                    self.adapter.paths(env, G).mcp,
                    env.home / ".config" / "opencode" / "opencode.jsonc",
                )

    def test_the_process_environment_is_never_read(self):
        import os

        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "leak")
        try:
            paths = self.adapter.paths(self.env, G)
        finally:
            if old is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = old
        self.assertEqual(paths.mcp.parent, self.env.home / ".config" / "opencode")

    def test_config_file_choice(self):
        base = self.env.home / ".config" / "opencode"
        base.mkdir(parents=True)
        self.assertEqual(self.config_file, base / "opencode.jsonc")  # neither exists
        (base / "opencode.json").write_text("{}", "utf-8")
        self.assertEqual(self.config_file, base / "opencode.json")  # only json exists
        (base / "opencode.jsonc").write_text("{}", "utf-8")
        self.assertEqual(self.config_file, base / "opencode.jsonc")  # jsonc wins

    def test_detect_uses_the_given_path_and_config_dir_only(self):
        self.assertFalse(self.adapter.detect(self.env).installed)
        base = self.env.home / ".config" / "opencode"
        base.mkdir(parents=True)
        found = self.adapter.detect(self.env)
        self.assertTrue(found.installed)
        self.assertEqual(found.config_roots, (base,))
        self.assertIsNone(found.version)  # detect never starts a program

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
    def test_a_new_file_is_created_and_claimed(self):
        ops = self.adapter.plan_instructions(self.env, G, "# Demo\n", frozenset())
        self.assertEqual(len(ops), 1)
        self.assertIsNone(ops[0].expect_sha256)
        self.assertEqual(ops[0].claims[0].key, INSTRUCTIONS_KEY)
        self.applier.apply(Plan(ops))
        self.assertEqual(self.adapter.read_instructions(self.env, G).text, "# Demo\n")

    def test_unowned_different_file_is_a_conflict_and_equal_file_is_left_alone(self):
        path = self.adapter.paths(self.env, G).instructions
        path.parent.mkdir(parents=True)
        path.write_bytes(b"the user's own rules\n")
        ops = self.adapter.plan_instructions(self.env, G, "# Demo\n", frozenset())
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(
            self.adapter.plan_instructions(self.env, G, "the user's own rules\n", frozenset()), []
        )

    def test_an_owned_file_is_updated_with_the_current_hash(self):
        path = self.adapter.paths(self.env, G).instructions
        self.applier.apply(Plan(self.adapter.plan_instructions(self.env, G, "one\n", frozenset())))
        owned = self.owned(Kind.INSTRUCTIONS)
        self.assertEqual(owned, {INSTRUCTIONS_KEY})
        ops = self.adapter.plan_instructions(self.env, G, "two\n", owned)
        self.assertEqual(ops[0].expect_sha256, contract_applier.sha256_of(path.read_bytes()))
        self.applier.apply(Plan(ops))
        self.assertEqual(path.read_bytes(), b"two\n")


# -- MCP: format ---------------------------------------------------------------------------------


class McpFormatTests(Base):
    def test_encoding_of_local_remote_and_disabled_servers(self):
        local = McpServer(
            name="a",
            command="demo-cmd",
            args=("--flag", "v"),
            env=(("DEMO_MODE", "test"),),
            env_vars=("DEMO_TOKEN",),
            cwd="work",
        )
        self.assertEqual(
            encode_server(local, windows=False),
            {
                "type": "local",
                "command": ["demo-cmd", "--flag", "v"],
                "environment": {"DEMO_MODE": "test", "DEMO_TOKEN": "{env:DEMO_TOKEN}"},
                "cwd": "work",
            },
        )
        remote = McpServer(
            name="b",
            url="https://demo.example.test/mcp",
            headers=(
                HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),
                HeaderRef("X-Key", "DEMO_KEY"),
            ),
        )
        self.assertEqual(
            encode_server(remote, windows=False),
            {
                "type": "remote",
                "url": "https://demo.example.test/mcp",
                "headers": {
                    "Authorization": "Bearer {env:DEMO_TOKEN}",
                    "X-Key": "{env:DEMO_KEY}",
                },
            },
        )
        off = self.stdio(enabled=False)
        self.assertEqual(encode_server(off, windows=False)["disabled"], True)
        self.assertEqual(encode_server(off, windows=False, v1=True)["enabled"], False)

    def test_secrets_are_referenced_by_name_and_no_value_is_written(self):
        server = self.stdio(env_vars=("DEMO_TOKEN",), env=(("DEMO_MODE", "test"),))
        remote = McpServer(
            name="r",
            url="https://x.example.test",
            headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),),
        )
        self.sync_mcp({"demo-server": server, "r": remote})
        text = self.config_file.read_text("utf-8")
        self.assertIn("{env:DEMO_TOKEN}", text)
        self.assertIn("Bearer {env:DEMO_TOKEN}", text)
        self.assertNotIn("$DEMO_TOKEN", text)
        self.assertNotIn("${DEMO_TOKEN}", text)

    def test_reading_never_copies_a_secret_literal_into_the_model(self):
        self.write_config(FIXTURE.read_bytes())
        servers = self.adapter.read_mcp(self.env, G)
        self.assertEqual(set(servers), {"user-remote", "user-local"})
        local = servers["user-local"]
        self.assertEqual((local.env, local.env_vars), ((), ()))  # the fake secret was skipped
        self.assertNotIn("FAKE-ENV-SECRET", repr(servers))
        self.assertEqual(servers["user-remote"].auth, "none")  # "oauth": false
        self.assertEqual(servers["user-remote"].url, "https://user.example.test/mcp")

    def test_decode_handles_both_switches_and_indirect_references(self):
        entry = {
            "type": "local",
            "command": ["demo", "-x"],
            "enabled": False,
            "environment": {"A": "{env:A}", "B": "{env:OTHER}", "MODE": "test", "C": "pre{env:C}"},
        }
        server = decode_server("n", entry)
        self.assertFalse(server.enabled)
        self.assertEqual((server.command, server.args), ("demo", ("-x",)))
        self.assertEqual(server.env_vars, ("A",))
        self.assertEqual(server.env, (("MODE", "test"),))
        self.assertFalse(decode_server("n", {"command": ["x"], "disabled": True}).enabled)
        self.assertTrue(decode_server("n", {"command": ["x"], "disabled": False}).enabled)

    def test_a_literal_header_is_not_representable_and_is_skipped_on_read(self):
        entry = {"type": "remote", "url": "https://x.example.test", "headers": {"X-Mode": "plain"}}
        self.assertEqual(decode_server("n", entry).headers, ())

    def test_normalize_mcp_names_exactly_what_does_not_survive(self):
        a = self.adapter
        local = McpServer(
            name="l",
            command="npx.cmd",
            args=("p",),
            env=(("M", "1"),),
            startup_timeout_sec=9,
            headers=(HeaderRef("H", "V"),),
            auth="oauth",
        )
        remote = McpServer(
            name="r",
            url="https://x.example.test",
            command="c",
            args=("a",),
            env_vars=("V",),
            env=(("M", "1"),),
            cwd="w",
            headers=(HeaderRef("H", "V"),),
            auth="oauth",
            enabled=False,
        )
        out = a.normalize_mcp({"l": local, "r": remote})
        self.assertEqual(
            out["l"], McpServer(name="l", command="npx", args=("p",), env=(("M", "1"),))
        )
        self.assertEqual(
            out["r"],
            McpServer(
                name="r",
                url="https://x.example.test",
                headers=(HeaderRef("H", "V"),),
                enabled=False,
            ),
        )


# -- MCP: planning and editing -------------------------------------------------------------------


class McpPlanTests(Base):
    def test_round_trip_read_then_plan_gives_no_ops(self):
        desired = {
            "demo-server": self.stdio(
                args=("--a",), env=(("DEMO_MODE", "test"),), env_vars=("DEMO_TOKEN",), cwd="w"
            ),
            "demo-remote": McpServer(
                name="demo-remote",
                url="https://demo.example.test/mcp",
                headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),),
            ),
            "demo-off": self.stdio("demo-off", enabled=False),
        }
        self.sync_mcp(desired)
        read = self.adapter.read_mcp(self.env, G)
        self.assertEqual(read, self.adapter.normalize_mcp(desired))
        self.assertEqual(self.plan_mcp(desired), [])
        self.assertEqual(self.plan_mcp(read), [])  # also what `read_mcp` returned, owned or not
        self.assertEqual(self.plan_mcp(read, owned=()), [])  # an equal unowned item is no conflict

    def test_a_new_file_has_the_schema_and_the_v2_layout(self):
        self.sync_mcp({"demo-server": self.stdio()})
        doc = self.doc()
        self.assertEqual(doc["$schema"], "https://opencode.ai/config.json")
        self.assertEqual(
            doc["mcp"]["servers"]["demo-server"], {"type": "local", "command": ["demo-cmd"]}
        )
        self.assertEqual(self.owned(Kind.MCP), {"demo-server"})

    def test_comments_and_the_users_other_keys_survive_add_update_remove_and_undo(self):
        original = FIXTURE.read_bytes()
        self.write_config(original)
        first = self.applier.apply(Plan(self.plan_mcp({"demo-server": self.stdio(args=("--a",))})))
        text = self.config_file.read_text("utf-8")
        for kept in (
            "// The user's own settings",
            "/* inline block comment */",
            "// the user's own remote server; its oauth setting is not ours",
            "// keep this comment",
            '"model": "fake/model"',
            '"timeout": { "startup": 45000 }',
            '"oauth": false',
        ):
            self.assertIn(kept, text)
        self.assertEqual(
            set(self.doc()["mcp"]["servers"]), {"user-remote", "user-local", "demo-server"}
        )
        second = self.applier.apply(Plan(self.plan_mcp({"demo-server": self.stdio(args=("--b",))})))
        self.assertEqual(
            self.doc()["mcp"]["servers"]["demo-server"]["command"], ["demo-cmd", "--b"]
        )
        third = self.applier.apply(Plan(self.plan_mcp({})))
        self.assertEqual(set(self.doc()["mcp"]["servers"]), {"user-remote", "user-local"})
        self.assertIn("// keep this comment", self.config_file.read_text("utf-8"))
        for receipt in (third, second, first):
            self.applier.undo(receipt)
        self.assertEqual(self.config_file.read_bytes(), original)

    def test_updating_an_owned_entry_keeps_the_keys_we_do_not_own(self):
        self.sync_mcp({"demo-remote": McpServer(name="demo-remote", url="https://a.example.test")})
        doc = self.doc()
        doc["mcp"]["servers"]["demo-remote"]["timeout"] = {"startup": 1000}
        doc["mcp"]["servers"]["demo-remote"]["oauth"] = False
        self.config_file.write_text(json.dumps(doc, indent=2) + "\n", "utf-8")
        self.sync_mcp({"demo-remote": McpServer(name="demo-remote", url="https://b.example.test")})
        entry = self.doc()["mcp"]["servers"]["demo-remote"]
        self.assertEqual(entry["url"], "https://b.example.test")
        self.assertEqual(entry["timeout"], {"startup": 1000})
        self.assertIs(entry["oauth"], False)

    def test_switching_a_server_from_local_to_remote_drops_only_our_keys(self):
        self.sync_mcp({"x": self.stdio("x", env=(("M", "1"),), cwd="w")})
        self.sync_mcp({"x": McpServer(name="x", url="https://x.example.test")})
        self.assertEqual(
            self.doc()["mcp"]["servers"]["x"], {"type": "remote", "url": "https://x.example.test"}
        )

    def test_a_file_without_mcp_gets_it_added_and_comments_stay(self):
        self.write_config('{\n  // my settings\n  "model": "fake/model" // trailing\n}\n')
        self.sync_mcp({"demo-server": self.stdio()})
        text = self.config_file.read_text("utf-8")
        self.assertIn("// my settings", text)
        self.assertIn("// trailing", text)
        self.assertEqual(self.doc()["mcp"]["servers"]["demo-server"]["type"], "local")
        self.assertEqual(self.doc()["model"], "fake/model")

    def test_mcp_without_servers_gets_the_servers_object_and_keeps_the_timeout(self):
        self.write_config('{\n  "mcp": {\n    "timeout": {"startup": 5000}\n  }\n}\n')
        self.sync_mcp({"demo-server": self.stdio()})
        self.assertEqual(self.doc()["mcp"]["timeout"], {"startup": 5000})
        self.assertIn("demo-server", self.doc()["mcp"]["servers"])

    def test_a_blank_existing_file_is_filled_in(self):
        self.write_config("\n")
        ops = self.plan_mcp({"demo-server": self.stdio()})
        self.assertIsNotNone(ops[0].expect_sha256)
        self.applier.apply(Plan(ops))
        self.assertIn("demo-server", self.doc()["mcp"]["servers"])

    def test_the_older_v1_layout_is_edited_in_place_with_enabled(self):
        self.write_config(
            '{\n  "mcp": {\n    "user-v1": {"type": "local", "command": ["u"]}\n  }\n}\n'
        )
        self.sync_mcp({"demo-off": self.stdio("demo-off", enabled=False)})
        servers = self.doc()["mcp"]
        self.assertNotIn("servers", servers)
        self.assertEqual(
            servers["demo-off"], {"type": "local", "command": ["demo-cmd"], "enabled": False}
        )
        self.assertIn("user-v1", servers)
        self.assertEqual(set(self.adapter.read_mcp(self.env, G)), {"user-v1", "demo-off"})
        self.assertFalse(self.adapter.read_mcp(self.env, G)["demo-off"].enabled)

    def test_crlf_and_bom_are_preserved(self):
        text = '{\r\n  // note\r\n  "mcp": {"servers": {}}\r\n}\r\n'
        self.write_config(b"\xef\xbb\xbf" + text.encode("utf-8"))
        self.sync_mcp({"demo-server": self.stdio()})
        data = self.config_file.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        body = data[3:]
        self.assertNotIn(b"\n", body.replace(b"\r\n", b""))
        self.assertIn(b"// note", body)
        self.assertIn("demo-server", self.adapter.read_mcp(self.env, G))

    def test_unowned_differing_entry_is_a_conflict_and_nothing_is_written(self):
        original = FIXTURE.read_bytes()
        self.write_config(original)
        desired = {"user-local": self.stdio("user-local", command="other")}
        ops = self.plan_mcp(desired)
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual((ops[0].kind, ops[0].key), (Kind.MCP, "user-local"))
        self.applier.apply(Plan(ops))
        self.assertEqual(self.config_file.read_bytes(), original)

    def test_only_owned_entries_are_removed(self):
        self.write_config(FIXTURE.read_bytes())
        self.sync_mcp({"demo-server": self.stdio()})
        self.assertEqual(self.owned(Kind.MCP), {"demo-server"})
        ops = self.plan_mcp({})  # user-local / user-remote are not desired and not owned
        self.assertEqual(ops[0].releases[0].key, "demo-server")
        self.assertEqual(len(ops[0].releases), 1)
        self.applier.apply(Plan(ops))
        self.assertEqual(set(self.doc()["mcp"]["servers"]), {"user-remote", "user-local"})

    def test_an_owned_entry_that_vanished_is_not_released_or_recreated_from_nothing(self):
        self.sync_mcp({"demo-server": self.stdio()})
        self.write_config('{"mcp": {"servers": {}}}\n')
        self.assertEqual(self.plan_mcp({}), [])  # nothing to remove

    def test_a_non_object_member_of_an_owned_name_is_replaced_but_not_an_unowned_one(self):
        self.sync_mcp({"x": self.stdio("x")})
        self.write_config('{"mcp": {"servers": {"x": null, "y": null}}}\n')
        ops = self.plan_mcp({"x": self.stdio("x"), "y": self.stdio("y")})
        self.assertEqual([type(o) for o in ops], [Conflict, EditFile])
        self.applier.apply(Plan(ops))
        self.assertEqual(self.doc()["mcp"]["servers"]["x"]["type"], "local")
        self.assertIsNone(self.doc()["mcp"]["servers"]["y"])


class McpWindowsShimTests(Base):
    def test_cmd_is_added_on_windows_only(self):
        for shim in ("npx", "npm", "pnpm", "yarn"):
            server = self.stdio(command=shim, args=("-y", "pkg"))
            self.assertEqual(encode_server(server, windows=True)["command"][0], f"{shim}.cmd")
            self.assertEqual(encode_server(server, windows=False)["command"][0], shim)
        other = self.stdio(command="node")
        self.assertEqual(encode_server(other, windows=True)["command"], ["node"])
        explicit = self.stdio(command="npx.cmd")
        self.assertEqual(encode_server(explicit, windows=True)["command"], ["npx.cmd"])

    def test_the_plan_follows_env_platform_not_the_host(self):
        server = {"s": self.stdio("s", command="npx", args=("pkg",))}
        for platform, expected in (("windows", "npx.cmd"), ("linux", "npx"), ("macos", "npx")):
            with self.subTest(platform=platform):
                env = self.make_env(platform, root=self.root / platform)
                ops = self.adapter.plan_mcp(env, G, server, frozenset())
                doc = jsonc.loads(ops[0].transform(None).decode("utf-8"))
                self.assertEqual(doc["mcp"]["servers"]["s"]["command"][0], expected)

    def test_reading_strips_cmd_and_an_existing_npx_is_not_rewritten_on_windows(self):
        win = self.make_env("windows")
        self.write_config(
            '{"mcp": {"servers": {"s": {"type": "local", "command": ["npx", "pkg"]}}}}\n'
        )
        wanted = {"s": self.stdio("s", command="npx", args=("pkg",))}
        self.assertEqual(
            self.adapter.plan_mcp(win, G, wanted, frozenset({"s"})), []
        )  # owned, equal
        self.assertEqual(self.adapter.plan_mcp(win, G, wanted, frozenset()), [])  # unowned, equal
        self.write_config(
            '{"mcp": {"servers": {"s": {"type": "local", "command": ["npx.cmd", "pkg"]}}}}\n'
        )
        self.assertEqual(self.adapter.read_mcp(win, G)["s"].command, "npx")
        self.assertEqual(self.adapter.plan_mcp(win, G, wanted, frozenset()), [])

    def test_round_trip_with_a_launcher_on_windows(self):
        win = self.make_env("windows")
        desired = {"s": self.stdio("s", command="npx", args=("pkg",))}
        ops = self.adapter.plan_mcp(win, G, desired, frozenset())
        self.applier.apply(Plan(ops))
        self.assertEqual(self.adapter.read_mcp(win, G), self.adapter.normalize_mcp(desired))
        self.assertEqual(self.adapter.plan_mcp(win, G, desired, frozenset({"s"})), [])


class McpUnsupportedTests(Base):
    def reasons(self, ops):
        return " | ".join(o.reason for o in ops if isinstance(o, Unsupported))

    def test_startup_timeout_is_reported_not_silently_dropped(self):
        ops = self.plan_mcp({"s": self.stdio("s", startup_timeout_sec=60)})
        self.assertEqual([type(o) for o in ops], [EditFile, Unsupported])
        self.assertIn("s: startup_timeout_sec not stored", self.reasons(ops))
        self.assertNotIn(
            "startup", self.config_file.read_text("utf-8") if self.config_file.exists() else ""
        )
        # reported again, but a plan that only reports is "empty" for the engine
        self.applier.apply(Plan(ops))
        again = Plan(self.plan_mcp({"s": self.stdio("s", startup_timeout_sec=60)}))
        self.assertTrue(again.is_empty)
        self.assertIn("startup_timeout_sec", self.reasons(again.ops))

    def test_remote_server_fields_with_no_place_are_reported(self):
        server = McpServer(
            name="r",
            url="https://x.example.test",
            args=("a",),
            env_vars=("V",),
            env=(("M", "1"),),
            cwd="w",
        )
        ops = self.plan_mcp({"r": server})
        self.assertIn("args, env, env_vars, cwd", self.reasons(ops))
        self.applier.apply(Plan(ops))
        self.assertEqual(
            self.doc()["mcp"]["servers"]["r"], {"type": "remote", "url": "https://x.example.test"}
        )

    def test_headers_on_a_local_server_are_reported(self):
        ops = self.plan_mcp({"s": self.stdio("s", headers=(HeaderRef("H", "V"),))})
        self.assertIn("s: headers not stored", self.reasons(ops))

    def test_servers_that_cannot_be_written_at_all_are_skipped_with_a_reason(self):
        bad = {
            "empty": McpServer(name="empty"),
            "badvar": self.stdio("badvar", env_vars=("NOT-A-NAME",)),
            "badhdr": McpServer(
                name="badhdr", url="https://x.example.test", headers=(HeaderRef("Bad Name", "V"),)
            ),
            "badscheme": McpServer(
                name="badscheme",
                url="https://x.example.test",
                headers=(HeaderRef("H", "V", "Be arer"),),
            ),
        }
        ops = self.plan_mcp(bad)
        self.assertEqual([type(o) for o in ops], [Unsupported])
        for name in bad:
            self.assertIn(f"{name}: not written", ops[0].reason)

    def test_a_server_that_became_unwritable_is_not_removed_from_the_file(self):
        self.sync_mcp({"s": self.stdio("s")})
        ops = self.plan_mcp({"s": McpServer(name="s")})
        self.assertEqual([type(o) for o in ops], [Unsupported])
        self.assertIn("s", self.doc()["mcp"]["servers"])


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
        with self.assertRaises(ConfigUnreadable):
            self.adapter.read_mcp(self.env, G)

    def test_syntax_errors_are_refused_not_overwritten(self):
        self.assert_refused("{ this is not json")
        self.assert_refused('{"mcp": {"servers": {}} /* never closed')
        self.assert_refused(b"\xff\xfe\x00bad bytes")

    def test_a_wrong_shape_is_refused(self):
        self.assert_refused("[1, 2, 3]")
        self.assert_refused('{"mcp": "oops"}')
        self.assert_refused('{"mcp": {"servers": []}}')

    def test_every_desired_and_owned_name_gets_its_own_conflict(self):
        self.sync_mcp({"owned-one": self.stdio("owned-one")})
        self.write_config("{ broken")
        ops = self.plan_mcp({"new-one": self.stdio("new-one")})
        self.assertEqual({o.key for o in ops}, {"owned-one", "new-one"})

    def test_an_unreadable_file_with_nothing_to_do_plans_nothing(self):
        self.write_config("{ broken")
        self.assertEqual(self.plan_mcp({}), [])

    def test_the_error_message_never_contains_file_content(self):
        self.write_config('{"secret": "FAKE-ENV-SECRET-aaa111", broken')
        with self.assertRaises(ConfigUnreadable) as ctx:
            self.adapter.read_mcp(self.env, G)
        self.assertNotIn("FAKE-ENV-SECRET", str(ctx.exception))


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

    def test_links_are_made_with_a_copy_fallback_and_claimed(self):
        ops = self.plan([self.source("demo-skill-a")])
        self.assertEqual(len(ops), 1)
        self.assertIsInstance(ops[0], Link)
        self.assertTrue(ops[0].allow_copy_fallback)
        self.assertEqual(ops[0].claims[0].key, "demo-skill-a")
        self.sync([self.source("demo-skill-a")])
        self.assertEqual(self.owned(Kind.SKILLS), {"demo-skill-a"})
        self.assertEqual(self.plan([self.source("demo-skill-a")]), [])

    def test_a_skill_installed_by_other_means_is_never_touched(self):
        seed_opencode_home(self.env)
        user = self.skills_dir / "user-skill"
        before = (user / "SKILL.md").read_bytes()
        self.sync([self.source("demo-skill-a")])
        self.assertEqual({o.releases[0].key for o in self.plan([])}, {"demo-skill-a"})
        self.sync([])
        self.assertEqual((user / "SKILL.md").read_bytes(), before)
        self.assertFalse((self.skills_dir / "demo-skill-a").exists())
        self.assertEqual([s.name for s in self.adapter.read_skills(self.env, G)], ["user-skill"])

    def test_a_clash_with_a_skill_of_the_user_is_a_conflict(self):
        seed_opencode_home(self.env)
        ops = self.plan([self.source("user-skill")])
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(ops[0].path, self.skills_dir / "user-skill")

    def test_an_unowned_link_to_the_same_folder_is_fine_and_to_another_is_a_conflict(self):
        skill = self.source("demo-skill-a")
        self.skills_dir.mkdir(parents=True)
        fsops.create_dir_link(self.skills_dir / "demo-skill-a", skill.path)
        self.assertEqual(self.plan([skill]), [])
        other = self.source("demo-skill-a", "-other")
        ops = self.plan([SkillRef("demo-skill-a", other.path)])
        self.assertEqual([type(o) for o in ops], [Conflict])

    def test_an_owned_link_is_repointed_when_the_source_moves(self):
        self.sync([self.source("demo-skill-a")])
        other = self.source("demo-skill-a", "-v2")
        ops = self.plan([SkillRef("demo-skill-a", other.path)])
        self.assertEqual([type(o) for o in ops], [Link])
        self.applier.apply(Plan(ops))
        self.assertTrue(fsops.same_target(self.skills_dir / "demo-skill-a", other.path))

    def test_withdrawal_releases_and_leaves_an_unowned_link_alone(self):
        self.sync([self.source("demo-skill-a")])
        user_target = self.source("elsewhere")
        fsops.create_dir_link(self.skills_dir / "elsewhere", user_target.path)
        ops = self.plan([])
        self.assertEqual(len(ops), 1)
        self.assertIsNone(ops[0].target)
        self.assertEqual(ops[0].releases[0].key, "demo-skill-a")
        self.applier.apply(Plan(ops))
        self.assertTrue((self.skills_dir / "elsewhere").exists())
        self.assertEqual(self.owned(Kind.SKILLS), set())

    def test_a_missing_source_or_a_bad_name_is_a_conflict(self):
        missing = SkillRef("gone", self.env.config_dir / "skills" / "gone")
        bad = self.source("x")
        ops = self.plan([missing, SkillRef("a/b", bad.path), SkillRef("..", bad.path)])
        self.assertEqual([type(o) for o in ops], [Conflict] * 3)

    def make_copy(self, skill):
        dest = self.skills_dir / skill.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        fsops.make_marked_copy(skill.path, dest)
        return dest

    def test_a_marked_copy_in_sync_is_equal_and_a_stale_unowned_one_is_a_conflict(self):
        skill = self.source("demo-skill-a")
        self.make_copy(skill)
        self.assertEqual(self.plan([skill]), [])  # equal, even though unowned
        (skill.path / "extra.md").write_text("new\n", "utf-8")
        ops = self.plan([skill])
        self.assertEqual([type(o) for o in ops], [Conflict])

    def test_an_owned_stale_copy_is_refreshed_and_an_edited_one_is_not(self):
        skill = self.source("demo-skill-a")
        dest = self.make_copy(skill)
        owned = {"demo-skill-a"}
        (skill.path / "extra.md").write_text("new\n", "utf-8")
        ops = self.plan([skill], owned)
        self.assertEqual([type(o) for o in ops], [Link])
        (dest / "SKILL.md").write_text("edited by the user\n", "utf-8")
        ops = self.plan([skill], owned)
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertIn("edited", ops[0].reason)
        self.assertEqual(
            [type(o) for o in self.plan([], owned)], [Conflict]
        )  # not withdrawn either

    def test_an_owned_unedited_copy_is_withdrawn(self):
        skill = self.source("demo-skill-a")
        self.make_copy(skill)
        ops = self.plan([], {"demo-skill-a"})
        self.assertEqual([type(o) for o in ops], [Link])
        self.assertIsNone(ops[0].target)

    def test_a_real_folder_or_file_never_counts_as_ours(self):
        skill = self.source("demo-skill-a")
        self.skills_dir.mkdir(parents=True)
        (self.skills_dir / "demo-skill-a").mkdir()
        owned = {"demo-skill-a"}
        self.assertEqual([type(o) for o in self.plan([skill], owned)], [Conflict])
        self.assertEqual([type(o) for o in self.plan([], owned)], [Conflict])

    def test_read_skills_lists_folders_and_marks_links(self):
        seed_opencode_home(self.env)
        self.sync([self.source("demo-skill-a")])
        read = {s.name: s for s in self.adapter.read_skills(self.env, G)}
        self.assertEqual(set(read), {"user-skill", "demo-skill-a"})
        self.assertFalse(read["user-skill"].is_link)
        self.assertTrue(read["demo-skill-a"].is_link)


class NotesTests(unittest.TestCase):
    def test_declares_the_three_v01_kinds_and_states_its_limits(self):
        adapter = OpenCodeAdapter()
        self.assertEqual(adapter.capabilities, {Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})
        text = " ".join(adapter.notes())
        for word in ("startup_timeout_sec", "{env:NAME}", ".cmd", "unverified"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
