"""Focused tests of the Antigravity adapter (the generic contract suite is in test_adapter_contract).

Everything runs in a temporary home and applies ops with the real engine (the same seam as the
contract suite). The platform is always passed explicitly, so the path logic is tested the same way
on Windows, Linux and macOS. Names are neutral; the secret-looking strings are fakes.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
import contract_applier
from antigravity_support import FIXTURE, seed_antigravity_home
from one_setup_all_agents._core import jsonc
from one_setup_all_agents.adapters import get_adapter
from one_setup_all_agents.adapters.antigravity import (
    INSTRUCTIONS_LIMIT,
    AntigravityAdapter,
    ConfigUnreadable,
    ManifestUnreadable,
    decode_server,
    encode_server,
    load_manifest,
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


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osaa-agy-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.adapter = AntigravityAdapter()
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
    def mcp_file(self):
        return self.adapter.paths(self.env, G).mcp

    @property
    def manifest_file(self):
        return self.mcp_file.with_name("skills.json")

    def owned(self, kind):
        return frozenset(journal.owned_for(self.env.state_dir, self.adapter.id, kind.value))

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    @staticmethod
    def stdio(name="demo-server", **kw):
        return McpServer(name=name, command=kw.pop("command", "demo-cmd"), **kw)

    # -- mcp
    def plan_mcp(self, desired, owned=None, env=None):
        owned = self.owned(Kind.MCP) if owned is None else frozenset(owned)
        return self.adapter.plan_mcp(env or self.env, G, desired, owned)

    def sync_mcp(self, desired):
        plan = Plan(self.plan_mcp(desired))
        self.applier.apply(plan)
        return plan

    def servers(self):
        return jsonc.loads(self.mcp_file.read_text("utf-8-sig"))["mcpServers"]

    # -- skills
    def source(self, name):
        folder = self.env.config_dir / "skills" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\nDemo {name}.\n", "utf-8")
        return SkillRef(name=name, path=folder)

    def plan_skills(self, desired, owned=None):
        owned = self.owned(Kind.SKILLS) if owned is None else frozenset(owned)
        return self.adapter.plan_skills(self.env, G, desired, owned)

    def sync_skills(self, desired):
        ops = self.plan_skills(desired)
        self.applier.apply(Plan(ops))
        return ops

    def entries(self):
        return jsonc.loads(self.manifest_file.read_text("utf-8-sig"))["entries"]


class RegistryTests(unittest.TestCase):
    def test_agy_is_registered_with_the_three_v01_kinds(self):
        adapter = get_adapter("agy")
        self.assertIsInstance(adapter, AntigravityAdapter)
        self.assertEqual(adapter.capabilities, {Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})


class PathTests(Base):
    def test_locations_are_the_same_on_every_platform(self):
        for platform in ("windows", "linux", "macos"):
            with self.subTest(platform=platform):
                env = self.make_env(platform)
                paths = self.adapter.paths(env, G)
                gemini = env.home / ".gemini"
                self.assertEqual(paths.instructions, gemini / "GEMINI.md")
                self.assertEqual(paths.mcp, gemini / "config" / "mcp_config.json")
                self.assertEqual(paths.skills_dir, gemini / "config" / "skills")

    def test_no_environment_variable_moves_anything(self):
        environ = {
            "XDG_CONFIG_HOME": "/xdg-demo",
            "GEMINI_HOME": "/gemini-demo",
            "APPDATA": "C:\\appdata-demo",
        }
        env = self.make_env("linux", environ)
        self.assertEqual(self.adapter.paths(env, G), self.adapter.paths(self.env, G))

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
        self.assertEqual(paths.mcp.parent, self.env.home / ".gemini" / "config")

    def test_detect_uses_the_given_path_and_home_only(self):
        self.assertFalse(self.adapter.detect(self.env).installed)
        (self.env.home / ".gemini").mkdir(parents=True)
        found = self.adapter.detect(self.env)
        self.assertTrue(found.installed)
        self.assertEqual(found.config_roots, (self.env.home / ".gemini",))
        self.assertIsNone(found.version)  # detect never starts a program

    def test_detect_finds_the_binary_on_the_given_path(self):
        import os

        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        binary = bin_dir / ("agy.exe" if os.name == "nt" else "agy")
        binary.write_bytes(b"")
        binary.chmod(0o755)
        env = self.make_env("windows" if os.name == "nt" else "linux", {"PATH": str(bin_dir)})
        found = self.adapter.detect(env)
        self.assertTrue(found.installed)
        self.assertEqual(Path(found.binary).resolve(), binary.resolve())

    def test_detect_knows_the_default_windows_install_from_the_given_environ(self):
        local = self.root / "local"
        exe = local / "agy" / "bin" / "agy.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        env = self.make_env("windows", {"LOCALAPPDATA": str(local)})
        self.assertEqual(self.adapter.detect(env).binary, exe)
        self.assertIsNone(
            self.adapter.detect(self.make_env("linux", {"LOCALAPPDATA": str(local)})).binary
        )

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


class InstructionsTests(Base):
    def path(self):
        return self.adapter.paths(self.env, G).instructions

    def test_a_new_file_is_created_and_claimed(self):
        (op,) = self.adapter.plan_instructions(self.env, G, "# Demo\n", frozenset())
        self.assertIsInstance(op, EditFile)
        self.assertIsNone(op.expect_sha256)
        self.assertEqual(op.path, self.path())
        self.assertEqual(op.claims[0].key, INSTRUCTIONS_KEY)
        self.applier.apply(Plan([op]))
        self.assertEqual(self.adapter.read_instructions(self.env, G).text, "# Demo\n")

    def test_unowned_different_file_is_a_conflict_and_equal_file_is_left_alone(self):
        self.write(self.path(), "mine\n")
        (conflict,) = self.adapter.plan_instructions(self.env, G, "# Demo\n", frozenset())
        self.assertIsInstance(conflict, Conflict)
        self.assertEqual(self.adapter.plan_instructions(self.env, G, "mine\n", frozenset()), [])

    def test_the_limit_is_in_bytes_and_a_text_over_it_is_a_conflict(self):
        exact = "x" * INSTRUCTIONS_LIMIT
        (op,) = self.adapter.plan_instructions(self.env, G, exact, frozenset())
        self.assertIsInstance(op, EditFile)
        for text in ("x" * (INSTRUCTIONS_LIMIT + 1), chr(0x639) * (INSTRUCTIONS_LIMIT // 2 + 1)):
            (conflict,) = self.adapter.plan_instructions(self.env, G, text, frozenset())
            self.assertIsInstance(conflict, Conflict)
            self.assertEqual(conflict.key, INSTRUCTIONS_KEY)
            self.assertIn("24000", conflict.reason)
            self.assertFalse(Plan([conflict]).is_empty)
        self.assertFalse(self.path().exists())

    def test_an_owned_text_over_the_limit_leaves_the_file_as_it_was(self):
        self.write(self.path(), "old\n")
        ops = self.adapter.plan_instructions(
            self.env, G, "y" * (INSTRUCTIONS_LIMIT + 1), frozenset({INSTRUCTIONS_KEY})
        )
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(self.path().read_text("utf-8"), "old\n")


class McpFormatTests(Base):
    def test_encoding_of_stdio_http_and_disabled_servers(self):
        local = self.stdio(
            args=("--flag",), env=(("DEMO_MODE", "test"),), env_vars=("DEMO_TOKEN",), cwd="work"
        )
        self.assertEqual(
            encode_server(local, windows=False),
            {
                "command": "demo-cmd",
                "args": ["--flag"],
                "env": {"DEMO_MODE": "test"},
                "cwd": "work",
            },
        )
        remote = McpServer("r", url="https://demo.example.test/mcp")
        self.assertEqual(
            encode_server(remote, windows=False), {"serverUrl": "https://demo.example.test/mcp"}
        )
        off = self.stdio(enabled=False)
        self.assertEqual(
            encode_server(off, windows=False),
            {"command": "demo-cmd", "args": [], "disabled": True},
        )

    def test_a_secret_is_never_written_by_name_or_by_value(self):
        server = self.stdio(env_vars=("DEMO_TOKEN",), env=(("DEMO_MODE", "test"),))
        text = json.dumps(encode_server(server, windows=True))
        self.assertNotIn("DEMO_TOKEN", text)  # agy cannot reference a variable
        self.assertNotIn("${", text)

    def test_reading_never_copies_a_secret_literal_into_the_model(self):
        server = decode_server(
            "x",
            {
                "command": "c",
                "env": {"USER_TOKEN": "FAKE-ENV-SECRET-aaa111", "USER_MODE": "demo"},
                "headers": {"Authorization": "Bearer FAKE-HDR-SECRET-bbb222"},
            },
        )
        self.assertEqual(server.env, (("USER_MODE", "demo"),))
        self.assertNotIn("FAKE-", repr(server))
        remote = decode_server("y", {"serverUrl": "https://u.example.test", "headers": {"A": "b"}})
        self.assertEqual(remote.headers, ())

    def test_decode_reads_the_url_alias_and_the_disabled_switch(self):
        self.assertEqual(decode_server("a", {"url": "https://u.example.test"}).transport, "http")
        self.assertEqual(
            decode_server("a", {"serverUrl": "https://u.example.test"}).transport, "http"
        )
        self.assertFalse(decode_server("a", {"command": "c", "disabled": True}).enabled)
        self.assertTrue(decode_server("a", {"command": "c", "disabled": False}).enabled)

    def test_normalize_mcp_names_exactly_what_does_not_survive(self):
        rich = self.stdio(
            args=("--flag",),
            env=(("DEMO_MODE", "test"),),
            env_vars=("DEMO_TOKEN",),
            cwd="work",
            startup_timeout_sec=30,
            auth="oauth",
        )
        got = self.adapter.normalize_mcp({"a": rich})["a"]
        self.assertEqual(
            got,
            self.stdio(args=("--flag",), env=(("DEMO_MODE", "test"),), cwd="work"),
        )
        remote = McpServer(
            "r", url="https://u.example.test", command="c", args=("a",), cwd="w", env_vars=("T",)
        )
        self.assertEqual(
            self.adapter.normalize_mcp({"r": remote})["r"],
            McpServer("r", url="https://u.example.test"),
        )
        with_headers = McpServer("h", url="https://u.example.test", headers=(HeaderRef("A", "T"),))
        self.assertEqual(self.adapter.normalize_mcp({"h": with_headers}), {})  # not written
        off = self.stdio(enabled=False)
        self.assertEqual(self.adapter.normalize_mcp({"o": off})["o"], off)  # written natively


class McpPlanTests(Base):
    def seed(self):
        seed_antigravity_home(self.env)

    def test_round_trip_read_then_plan_gives_no_ops(self):
        self.seed()
        desired = {
            "demo-server": self.stdio(
                args=("--flag",), env=(("DEMO_MODE", "test"),), env_vars=("DEMO_TOKEN",), cwd="w"
            ),
            "demo-remote": McpServer("demo-remote", url="https://demo.example.test/mcp"),
            "demo-off": self.stdio("demo-off", enabled=False),
        }
        self.sync_mcp(desired)
        read = self.adapter.read_mcp(self.env, G)
        self.assertEqual({n: read[n] for n in desired}, self.adapter.normalize_mcp(desired))
        ops = self.plan_mcp(desired)
        self.assertEqual([o for o in ops if not isinstance(o, Unsupported)], [])
        # feeding back what was read plans nothing either
        ours = {n: read[n] for n in desired}
        self.assertEqual(
            [o for o in self.plan_mcp(ours, owned=set(ours)) if not isinstance(o, Unsupported)], []
        )

    def test_a_new_file_has_only_mcp_servers(self):
        self.sync_mcp({"demo-server": self.stdio()})
        self.assertEqual(
            json.loads(self.mcp_file.read_text("utf-8")),
            {"mcpServers": {"demo-server": {"command": "demo-cmd", "args": []}}},
        )

    def test_comments_and_the_users_other_keys_survive_add_update_remove_and_undo(self):
        self.seed()
        original = self.mcp_file.read_bytes()
        receipts = []
        for desired in (
            {"demo-server": self.stdio(args=("--one",))},
            {"demo-server": self.stdio(args=("--two",)), "second": self.stdio("second")},
            {"second": self.stdio("second")},
        ):
            plan = Plan(self.plan_mcp(desired))
            receipts.append(self.applier.apply(plan))
            text = self.mcp_file.read_text("utf-8")
            for kept in ("// the user's own remote server", "// keep this comment", '"theme"'):
                self.assertIn(kept, text)
            self.assertEqual(self.servers()["user-local"]["env"]["USER_MODE"], "demo")
        self.assertNotIn("demo-server", self.servers())
        self.assertIn("second", self.servers())
        for receipt in reversed(receipts):
            self.applier.undo(receipt)
        self.assertEqual(self.mcp_file.read_bytes(), original)

    def test_updating_an_owned_entry_keeps_the_keys_we_do_not_own(self):
        self.write(
            self.mcp_file,
            '{"mcpServers": {"demo-server": {"command": "old", "args": [], '
            '"headers": {"X-A": "b"}, "authProviderType": "google_credentials"}}}',
        )
        ops = self.plan_mcp({"demo-server": self.stdio()}, owned={"demo-server"})
        self.applier.apply(Plan(ops))
        entry = self.servers()["demo-server"]
        self.assertEqual(entry["command"], "demo-cmd")
        self.assertEqual(entry["authProviderType"], "google_credentials")
        self.assertEqual(entry["headers"], {"X-A": "b"})

    def test_a_secret_literal_in_an_owned_entry_never_reaches_an_op_or_the_plan_text(self):
        from one_setup_all_agents.engine.plan import render_plan

        self.seed()
        ops = self.plan_mcp({"user-local": self.stdio("user-local")}, owned={"user-local"})
        plan = Plan(ops)
        self.assertEqual(len(plan.executable), 1)
        self.assertNotIn("FAKE-ENV-SECRET", repr(ops))
        self.assertNotIn("FAKE-ENV-SECRET", render_plan(plan))
        self.applier.apply(plan)  # the source wins: the owned entry is rewritten from it
        self.assertEqual(self.servers()["user-local"], {"command": "demo-cmd", "args": []})

    def test_the_url_alias_is_replaced_by_server_url_when_an_owned_entry_changes(self):
        self.write(
            self.mcp_file,
            '{"mcpServers": {"r": {"url": "https://old.example.test", "httpUrl": "https://x.test"}}}',
        )
        server = McpServer("r", url="https://new.example.test/mcp")
        self.applier.apply(Plan(self.plan_mcp({"r": server}, owned={"r"})))
        self.assertEqual(self.servers()["r"], {"serverUrl": "https://new.example.test/mcp"})

    def test_a_url_alias_with_the_same_meaning_is_left_alone(self):
        self.write(self.mcp_file, '{"mcpServers": {"r": {"url": "https://u.example.test"}}}')
        ops = self.plan_mcp({"r": McpServer("r", url="https://u.example.test")})
        self.assertEqual(ops, [])

    def test_enabling_a_server_removes_the_disabled_key(self):
        self.sync_mcp({"a": self.stdio("a", enabled=False)})
        self.assertTrue(self.servers()["a"]["disabled"])
        self.sync_mcp({"a": self.stdio("a")})
        self.assertNotIn("disabled", self.servers()["a"])
        self.sync_mcp({"a": self.stdio("a", enabled=False)})
        self.assertTrue(self.servers()["a"]["disabled"])
        self.assertTrue(Plan(self.plan_mcp({"a": self.stdio("a", enabled=False)})).is_empty)

    def test_a_file_without_servers_gets_them_added_and_comments_stay(self):
        self.write(self.mcp_file, '{\n  // keep me\n  "theme": "demo"\n}\n')
        self.sync_mcp({"a": self.stdio("a")})
        text = self.mcp_file.read_text("utf-8")
        self.assertIn("// keep me", text)
        self.assertEqual(self.servers()["a"]["command"], "demo-cmd")

    def test_a_blank_existing_file_is_filled_in(self):
        self.write(self.mcp_file, "  \n")
        self.sync_mcp({"a": self.stdio("a")})
        self.assertEqual(self.servers()["a"]["command"], "demo-cmd")

    def test_crlf_and_bom_are_preserved(self):
        raw = b'\xef\xbb\xbf{\r\n  "mcpServers": {\r\n    "u": {"command": "c"}\r\n  }\r\n}\r\n'
        self.write(self.mcp_file, raw)
        self.sync_mcp({"a": self.stdio("a")})
        data = self.mcp_file.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        body = data[3:]
        self.assertIn(b"\r\n", body)
        self.assertNotIn(b"\n", body.replace(b"\r\n", b""))
        self.assertIn("u", self.servers())

    def test_unowned_differing_entry_is_a_conflict_and_nothing_is_written(self):
        self.seed()
        before = self.mcp_file.read_bytes()
        ops = self.plan_mcp({"user-local": self.stdio("user-local", command="other")})
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(ops[0].key, "user-local")
        self.assertEqual(Plan(ops).executable, [])
        self.assertEqual(self.mcp_file.read_bytes(), before)

    def test_only_owned_entries_are_removed(self):
        self.seed()
        self.sync_mcp({"mine": self.stdio("mine")})
        self.assertEqual(self.owned(Kind.MCP), {"mine"})
        self.sync_mcp({})
        self.assertEqual(set(self.servers()), {"user-remote", "user-local"})

    def test_an_owned_entry_that_vanished_is_not_released_or_recreated_from_nothing(self):
        self.write(self.mcp_file, '{"mcpServers": {}}')
        self.assertEqual(self.plan_mcp({}, owned={"gone"}), [])

    def test_a_non_object_member_of_an_owned_name_is_replaced_but_not_an_unowned_one(self):
        self.write(self.mcp_file, '{"mcpServers": {"a": 1, "b": 2}}')
        ops = self.plan_mcp({"a": self.stdio("a"), "b": self.stdio("b")}, owned={"a"})
        self.assertEqual([type(o).__name__ for o in ops], ["Conflict", "EditFile"])
        self.applier.apply(Plan(ops))
        self.assertEqual(self.servers()["b"], 2)
        self.assertEqual(self.servers()["a"]["command"], "demo-cmd")


class McpWindowsShimTests(Base):
    def test_cmd_is_added_on_windows_only(self):
        for platform, want in (("windows", "npx.cmd"), ("linux", "npx"), ("macos", "npx")):
            with self.subTest(platform=platform):
                self.assertEqual(
                    encode_server(self.stdio(command="npx"), windows=platform == "windows")[
                        "command"
                    ],
                    want,
                )
        other = encode_server(self.stdio(command="demo-cmd"), windows=True)
        self.assertEqual(other["command"], "demo-cmd")

    def test_the_plan_follows_env_platform_not_the_host(self):
        env = self.make_env("windows")
        (op, *_) = self.plan_mcp({"a": self.stdio("a", command="npm")}, env=env)
        written = jsonc.loads(op.transform(None).decode("utf-8"))
        self.assertEqual(written["mcpServers"]["a"]["command"], "npm.cmd")

    def test_reading_strips_cmd_and_an_existing_npx_is_not_rewritten_on_windows(self):
        self.write(self.mcp_file, '{"mcpServers": {"a": {"command": "npx", "args": ["-y", "p"]}}}')
        read = self.adapter.read_mcp(self.env, G)["a"]
        self.assertEqual(read.command, "npx")
        env = self.make_env("windows")
        self.assertEqual(
            self.plan_mcp({"a": self.stdio("a", command="npx", args=("-y", "p"))}, env=env), []
        )
        self.write(self.mcp_file, '{"mcpServers": {"a": {"command": "npx.cmd", "args": []}}}')
        self.assertEqual(self.adapter.read_mcp(self.env, G)["a"].command, "npx")


class McpUnsupportedTests(Base):
    def gaps(self, desired):
        return [o for o in self.plan_mcp(desired) if isinstance(o, Unsupported)]

    def test_startup_timeout_is_reported_not_silently_dropped(self):
        (gap,) = self.gaps({"a": self.stdio("a", startup_timeout_sec=30)})
        self.assertIn("a: startup_timeout_sec not stored", gap.reason)
        self.assertEqual(gap.kind, Kind.MCP)

    def test_env_vars_are_reported_because_the_file_cannot_show_them(self):
        (gap,) = self.gaps({"a": self.stdio("a", env_vars=("DEMO_TOKEN",))})
        self.assertIn("a: env_vars not stored", gap.reason)
        self.assertNotIn("DEMO_TOKEN", gap.reason)

    def test_remote_server_fields_with_no_place_are_reported(self):
        server = McpServer(
            "r",
            url="https://u.example.test",
            command="c",
            args=("a",),
            env=(("M", "1"),),
            env_vars=("T",),
            cwd="w",
        )
        (gap,) = self.gaps({"r": server})
        for field in ("command", "args", "env", "env_vars", "cwd"):
            self.assertIn(field, gap.reason)

    def test_a_remote_server_with_a_header_is_not_written(self):
        server = McpServer(
            "r",
            url="https://u.example.test",
            headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),),
        )
        ops = self.plan_mcp({"r": server})
        self.assertEqual([type(o) for o in ops], [Unsupported])
        self.assertIn("r: not written", ops[0].reason)
        self.assertNotIn("DEMO_TOKEN", ops[0].reason)
        self.assertFalse(self.mcp_file.exists())

    def test_a_server_that_became_unwritable_is_not_removed_from_the_file(self):
        self.sync_mcp({"r": McpServer("r", url="https://u.example.test")})
        became = McpServer("r", url="https://u.example.test", headers=(HeaderRef("A", "T"),))
        ops = self.plan_mcp({"r": became})
        self.assertEqual([type(o) for o in ops], [Unsupported])
        self.assertIn("r", self.servers())
        self.assertEqual(self.owned(Kind.MCP), {"r"})

    def test_a_server_with_neither_command_nor_url_is_skipped_with_a_reason(self):
        ops = self.plan_mcp({"x": McpServer("x")})
        self.assertEqual([type(o) for o in ops], [Unsupported])
        self.assertIn("neither a command nor a url", ops[0].reason)


class UnreadableConfigTests(Base):
    def test_syntax_errors_are_refused_not_overwritten(self):
        self.write(self.mcp_file, '{"mcpServers": {')
        before = self.mcp_file.read_bytes()
        with self.assertRaises(ConfigUnreadable):
            self.adapter.read_mcp(self.env, G)
        ops = self.plan_mcp({"a": self.stdio("a")})
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(Plan(ops).executable, [])
        self.assertEqual(self.mcp_file.read_bytes(), before)

    def test_a_wrong_shape_is_refused(self):
        for text in ("[]", '{"mcpServers": []}', '{"mcpServers": "x"}'):
            with self.subTest(text=text):
                self.write(self.mcp_file, text)
                with self.assertRaises(ConfigUnreadable):
                    self.adapter.read_mcp(self.env, G)
                self.assertEqual(
                    [type(o) for o in self.plan_mcp({"a": self.stdio("a")})], [Conflict]
                )

    def test_every_desired_and_owned_name_gets_its_own_conflict(self):
        self.write(self.mcp_file, "not json")
        ops = self.plan_mcp({"a": self.stdio("a"), "b": self.stdio("b")}, owned={"c"})
        self.assertEqual(sorted(o.key for o in ops), ["a", "b", "c"])

    def test_an_unreadable_file_with_nothing_to_do_plans_nothing(self):
        self.write(self.mcp_file, "not json")
        self.assertEqual(self.plan_mcp({}), [])

    def test_the_error_message_never_contains_file_content(self):
        self.write(
            self.mcp_file, '{"mcpServers": {"a": {"env": {"K": "FAKE-ENV-SECRET-aaa111"}}} oops'
        )
        ops = self.plan_mcp({"a": self.stdio("a")})
        self.assertNotIn("FAKE-ENV-SECRET", ops[0].reason)
        with self.assertRaises(ConfigUnreadable) as ctx:
            self.adapter.read_mcp(self.env, G)
        self.assertNotIn("FAKE-ENV-SECRET", str(ctx.exception))


class SkillTests(Base):
    def test_a_new_manifest_lists_the_source_folder_with_forward_slashes_and_claims_it(self):
        skill = self.source("demo-skill-a")
        (op,) = self.plan_skills([skill])
        self.assertIsInstance(op, EditFile)
        self.assertIsNone(op.expect_sha256)
        self.assertEqual(op.path, self.manifest_file)
        self.assertEqual([r.key for r in op.claims], ["demo-skill-a"])
        self.assertEqual(op.releases, ())
        self.applier.apply(Plan([op]))
        (entry,) = self.entries()
        self.assertEqual(entry, {"path": skill.path.as_posix()})
        self.assertNotIn("\\", entry["path"])
        self.assertEqual(self.owned(Kind.SKILLS), {"demo-skill-a"})

    def test_no_link_is_made_and_nothing_is_written_to_the_shared_agents_folder(self):
        ops = self.plan_skills([self.source("demo-skill-a")])
        self.assertFalse([o for o in ops if isinstance(o, Link)])
        self.sync_skills([self.source("demo-skill-a")])
        self.assertFalse((self.env.home / ".agents").exists())
        self.assertFalse((self.env.home / ".gemini" / "config" / "skills").exists())
        self.assertEqual(self.adapter.wanted_links(self.env, G, [self.source("demo-skill-a")]), [])

    def test_a_second_plan_is_empty_and_a_move_of_the_source_is_repointed(self):
        a = self.source("demo-skill-a")
        self.sync_skills([a])
        self.assertEqual(self.plan_skills([a]), [])
        moved = self.root / "elsewhere" / "demo-skill-a"
        moved.mkdir(parents=True)
        (moved / "SKILL.md").write_text("x\n", "utf-8")
        (op,) = self.plan_skills([SkillRef("demo-skill-a", moved)])
        self.assertEqual([r.key for r in op.claims], ["demo-skill-a"])
        self.applier.apply(Plan([op]))
        self.assertEqual(self.entries(), [{"path": moved.as_posix()}])

    def test_other_members_comments_bom_and_crlf_of_the_manifest_survive(self):
        raw = (
            b'\xef\xbb\xbf{\r\n  // keep me\r\n  "exclude": ["hidden"],\r\n  "entries": [\r\n'
            b'    {"path": "/example/user-skills", "exclude": ["x"]}\r\n  ]\r\n}\r\n'
        )
        self.write(self.manifest_file, raw)
        self.assertEqual(self.plan_skills([]), [])
        skill = self.source("demo-skill-a")
        receipt = self.applier.apply(Plan(self.plan_skills([skill])))
        data = self.manifest_file.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"// keep me", data)
        self.assertNotIn(b"\n", data[3:].replace(b"\r\n", b""))
        doc = jsonc.loads(data.decode("utf-8-sig"))
        self.assertEqual(doc["exclude"], ["hidden"])
        self.assertEqual(doc["entries"][0], {"path": "/example/user-skills", "exclude": ["x"]})
        self.assertEqual(doc["entries"][1], {"path": skill.path.as_posix()})
        self.applier.undo(receipt)
        self.assertEqual(self.manifest_file.read_bytes(), raw)

    def test_a_manifest_without_entries_gets_the_array_added(self):
        self.write(self.manifest_file, '{\n  // keep me\n  "exclude": []\n}\n')
        self.sync_skills([self.source("demo-skill-a")])
        self.assertIn("// keep me", self.manifest_file.read_text("utf-8"))
        self.assertEqual(len(self.entries()), 1)

    def test_an_entry_of_the_user_for_the_same_folder_or_its_parent_is_enough(self):
        skill = self.source("demo-skill-a")
        for entry in (
            {"path": skill.path.as_posix()},
            {"path": skill.path.as_posix(), "exclude": ["z"]},
            {"path": skill.path.parent.as_posix()},
        ):
            with self.subTest(entry=entry):
                self.write(self.manifest_file, json.dumps({"entries": [entry]}))
                self.assertEqual(self.plan_skills([skill]), [])

    def test_a_relative_entry_is_resolved_against_the_manifest_folder(self):
        skill = self.source("demo-skill-a")
        import os

        try:
            rel = os.path.relpath(skill.path, self.manifest_file.parent).replace("\\", "/")
        except ValueError:  # another drive on Windows
            self.skipTest("no relative path between the drives")
        self.write(self.manifest_file, json.dumps({"entries": [{"path": rel}]}))
        self.assertEqual(self.plan_skills([skill]), [])

    def test_the_same_name_from_another_path_is_a_conflict(self):
        other = self.root / "other" / "demo-skill-a"
        other.mkdir(parents=True)
        self.write(self.manifest_file, json.dumps({"entries": [{"path": other.as_posix()}]}))
        ops = self.plan_skills([self.source("demo-skill-a")])
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(ops[0].key, "demo-skill-a")
        # also when the name is "owned" but the entry is customised
        self.write(
            self.manifest_file,
            json.dumps({"entries": [{"path": other.as_posix(), "exclude": ["q"]}]}),
        )
        ops = self.plan_skills([self.source("demo-skill-a")], owned={"demo-skill-a"})
        self.assertEqual([type(o) for o in ops], [Conflict])

    def test_a_real_folder_of_the_same_name_in_the_folder_agy_reads_is_a_conflict(self):
        folder = self.adapter.paths(self.env, G).skills_dir / "demo-skill-a"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("mine\n", "utf-8")
        ops = self.plan_skills([self.source("demo-skill-a"), self.source("demo-skill-b")])
        self.assertEqual(
            sorted(
                (type(o).__name__, getattr(o, "key", "")) for o in ops if isinstance(o, Conflict)
            ),
            [("Conflict", "demo-skill-a")],
        )
        self.assertEqual(len([o for o in ops if isinstance(o, EditFile)]), 1)
        self.applier.apply(Plan(ops))
        self.assertEqual((folder / "SKILL.md").read_text("utf-8"), "mine\n")
        self.assertEqual(self.owned(Kind.SKILLS), {"demo-skill-b"})

    def test_a_bad_name_a_missing_source_and_a_folder_name_mismatch_are_conflicts(self):
        good = self.source("demo-skill-a")
        missing = SkillRef("demo-skill-m", self.env.config_dir / "skills" / "demo-skill-m")
        renamed = SkillRef("other-name", good.path)
        relative = SkillRef("rel", Path("rel"))
        for skill in (
            missing,
            renamed,
            relative,
            SkillRef("a/b", good.path),
            SkillRef("..", good.path),
        ):
            with self.subTest(skill=skill.name):
                (conflict,) = self.plan_skills([skill])
                self.assertIsInstance(conflict, Conflict)
                self.assertTrue(conflict.reason)
        self.assertFalse(self.manifest_file.exists())

    def test_withdrawal_releases_only_a_plain_owned_entry_and_not_the_users(self):
        seed_antigravity_home(self.env)
        a, b = self.source("demo-skill-a"), self.source("demo-skill-b")
        self.sync_skills([a, b])
        before = self.entries()
        self.assertEqual(len(before), 3)  # the user's entry first, then ours
        (op,) = self.plan_skills([a])
        self.assertEqual([r.key for r in op.releases], ["demo-skill-b"])
        self.applier.apply(Plan([op]))
        self.assertEqual(self.entries(), [before[0], before[1]])
        self.assertEqual(self.owned(Kind.SKILLS), {"demo-skill-a"})
        # an entry for a skill that is not owned is never removed
        (cleared,) = self.plan_skills([], owned={"demo-skill-a"})
        self.assertEqual([r.key for r in cleared.releases], ["demo-skill-a"])
        self.assertEqual(self.plan_skills([], owned=set()), [])

    def test_withdrawal_works_after_the_source_folder_is_gone(self):
        a = self.source("demo-skill-a")
        self.sync_skills([a])
        shutil.rmtree(a.path)
        (op,) = self.plan_skills([])
        self.applier.apply(Plan([op]))
        self.assertEqual(self.entries(), [])

    def test_a_customised_owned_entry_is_not_removed(self):
        a = self.source("demo-skill-a")
        self.sync_skills([a])
        self.write(
            self.manifest_file,
            json.dumps({"entries": [{"path": a.path.as_posix(), "exclude": ["z"]}]}),
        )
        ops = self.plan_skills([])
        self.assertEqual([type(o) for o in ops], [Conflict])

    def test_an_unreadable_manifest_is_refused_for_every_name_and_never_written(self):
        for text in ("{", "[]", '{"entries": {}}'):
            with self.subTest(text=text):
                self.write(self.manifest_file, text)
                before = self.manifest_file.read_bytes()
                ops = self.plan_skills([self.source("demo-skill-a")], owned={"old-skill"})
                self.assertEqual(sorted(o.key for o in ops), ["demo-skill-a", "old-skill"])
                self.assertTrue(all(isinstance(o, Conflict) for o in ops))
                self.assertEqual(self.manifest_file.read_bytes(), before)
                with self.assertRaises(ManifestUnreadable):
                    load_manifest(self.manifest_file)
                with self.assertRaises(ManifestUnreadable):
                    self.adapter.read_skills(self.env, G)

    def test_comments_inside_the_entries_array_are_refused_instead_of_lost(self):
        raw = '{\n  "entries": [\n    // the user\'s note\n    {"path": "/example/x"}\n  ]\n}\n'
        self.write(self.manifest_file, raw)
        (conflict,) = self.plan_skills([self.source("demo-skill-a")])
        self.assertIsInstance(conflict, Conflict)
        self.assertIn("comments", conflict.reason)
        self.assertEqual(self.manifest_file.read_text("utf-8"), raw)
        self.assertEqual(self.plan_skills([]), [])

    def test_read_skills_lists_the_folder_agy_reads_and_what_the_manifest_names(self):
        seed_antigravity_home(self.env)
        mine = self.source("demo-skill-a")
        self.sync_skills([mine])
        got = {s.name: s for s in self.adapter.read_skills(self.env, G)}
        self.assertEqual(set(got), {"user-skill", "shared-skill", "demo-skill-a"})
        self.assertEqual(got["demo-skill-a"].path, mine.path)
        self.assertFalse(got["demo-skill-a"].is_link)

    def test_the_users_skills_are_never_touched(self):
        seed_antigravity_home(self.env)
        manifest = self.manifest_file.read_bytes()
        skill_file = self.adapter.paths(self.env, G).skills_dir / "user-skill" / "SKILL.md"
        content = skill_file.read_bytes()
        self.sync_skills([self.source("demo-skill-a")])
        self.sync_skills([])
        self.assertEqual(skill_file.read_bytes(), content)
        self.assertEqual(json.loads(self.manifest_file.read_text("utf-8")), json.loads(manifest))


class SharedSkillsTests(Base):
    def test_the_shared_skill_links_of_the_other_tools_are_not_touched_by_agy(self):
        from one_setup_all_agents.engine.plan import DesiredState, build_plan

        skill = self.source("demo-skill-a")
        plan = build_plan(
            self.env, [self.adapter], DesiredState(skills=[skill]), kinds=[Kind.SKILLS]
        )
        self.assertEqual([type(op) for op in plan.executable], [EditFile])


class NotesTests(unittest.TestCase):
    def test_declares_the_three_v01_kinds_and_states_its_limits(self):
        adapter = AntigravityAdapter()
        self.assertEqual(adapter.capabilities, {Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})
        text = "\n".join(adapter.notes())
        for needle in ("24000", "headers", "env_vars", "skills.json", ".cmd", "unverified"):
            self.assertIn(needle, text)

    def test_the_fixture_is_neutral(self):
        text = FIXTURE.read_text("utf-8")
        self.assertNotIn("C:\\", text)
        self.assertNotIn("@", text)


if __name__ == "__main__":
    unittest.main()
