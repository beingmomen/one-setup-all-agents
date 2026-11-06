"""Tests of the ported core: neutral source, import, apply / verify / rollback on a FAKE home."""

import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import apply as applymod
from one_setup_all_agents._core import jsonc, jsonc_edit, locations, toml_edit
from one_setup_all_agents._core.parsers import load_all
from one_setup_all_agents._core.plan import build_plan
from one_setup_all_agents._core.source import Source, SourceError
from support import (
    FIXTURE_ENV_VALUES,
    FIXTURE_HOME,
    FIXTURE_SECRETS,
    WorkDirCase,
    cli_env,
    digest,
    legacy_command,
)

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "fixtures" / "servers.jsonc"
FILE_TOOLS = "opencode,codex,agy"
NEW_NAMES = {"new-stdio", "new-http"}


def cli(*args, home, source=SOURCE):
    # the MCP tests exercise only the MCP part; skills have their own tests
    only = (
        ["--only", "mcp"]
        if args and args[0] in ("apply", "verify") and "--only" not in args
        else []
    )
    cmd = [*legacy_command(*args), *only, "--home", str(home), "--source", str(source)]
    p = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", timeout=300, env=cli_env()
    )
    return p.returncode, p.stdout, p.stderr


def raw_of(home, tool):
    return load_all(home)[tool].raw


def write_source(dirpath, text):
    p = Path(dirpath) / "src.jsonc"
    p.write_text(text, encoding="utf-8")
    return p


def source_text(**overrides):
    base = {
        "version": 1,
        "servers": {"s1": {"transport": "stdio", "command": "node", "args": ["a.js"]}},
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------- the neutral source
class SourceTests(WorkDirCase):
    def load(self, text):
        return Source(write_source(self.make_home(), text))

    def test_fixture_source_loads(self):
        s = Source(SOURCE)
        self.assertEqual(
            s.servers["new-stdio"].args, ["C:\\fake\\tools\\server.js", "--flag"]
        )  # {DIR} expanded
        self.assertEqual(s.retired, {"opencode": ["playwright"]})
        self.assertEqual(s.managed_names("opencode").count("opencode-only"), 1)
        self.assertNotIn("opencode-only", s.managed_names("codex"))

    def test_rejects_secret_looking_values(self):
        for bad in (
            {"s1": {"command": "node", "args": ["--token", "x", "--api-key=FAKE-ARG"]}},
            {"s1": {"command": "node", "args": ["--api-key=FAKE-ARG"]}},
            {"s1": {"transport": "http", "url": "https://h.test/m?api_key=FAKE-URL"}},
            {"s1": {"command": "node", "env": {"MY_TOKEN": "FAKE-ENV"}}},
            {"s1": {"command": "node", "env": {"X": "Bearer FAKE-ENV"}}},
        ):
            with self.assertRaises(SourceError, msg=str(bad)) as cm:
                self.load(source_text(servers=bad))
            self.assertNotIn("FAKE", str(cm.exception))

    def test_env_references_and_plain_literals_are_allowed(self):
        s = self.load(
            source_text(
                servers={"s1": {"command": "node", "env": {"MODE": "x", "MY_TOKEN": "${MY_TOKEN}"}}}
            )
        )
        self.assertEqual(set(s.servers["s1"].env), {"MODE", "MY_TOKEN"})

    def test_rejects_bad_shapes(self):
        for bad in (
            {"s1": {"transport": "http"}},
            {"s1": {"transport": "stdio"}},
            {"s1": {"command": "x", "headers_env": {"A": "B"}}},
            {"s1": {"command": "x", "tools": ["nope"]}},
            {"s1": {"transport": "http", "url": "https://a", "args": ["x"]}},
            {"s1": {"command": "x", "auth": "bearer"}},
        ):
            with self.assertRaises(SourceError, msg=str(bad)):
                self.load(source_text(servers=bad))
        with self.assertRaises(SourceError):
            self.load(source_text(extra=1))
        with self.assertRaises(SourceError):
            self.load(source_text(retired={"opencode": ["s1"]}))
        with self.assertRaises(SourceError):
            self.load(source_text(unmanaged={"codex": ["s1"]}))

    def test_missing_and_broken_source(self):
        with self.assertRaises(SourceError):
            Source(self.make_home() / "none.jsonc")
        with self.assertRaises(SourceError):
            self.load("{ not json")


# ---------------------------------------------------------------- text editors
class JsoncEditTests(unittest.TestCase):
    DOC = (
        '{\n  "a": 1,\n  "mcp": {\n    "servers": {\n      // first\n      "one": {\n        "x": 1\n      }, // tail\n'
        '      /* second */\n      "two": { "x": 2 },\n      "three": { "x": 3 }\n    }\n  }\n}\n'
    )

    def servers(self, text):
        return jsonc.loads(text)["mcp"]["servers"]

    def test_remove_first_middle_last(self):
        for key, left in (
            ("one", ["two", "three"]),
            ("two", ["one", "three"]),
            ("three", ["one", "two"]),
        ):
            out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=[key])
            self.assertEqual(list(self.servers(out)), left)
            self.assertIn('"a": 1', out)
        out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=["one"])
        self.assertNotIn("// first", out)
        self.assertNotIn("// tail", out)  # the member's own comments go with it
        out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=["two"])
        self.assertNotIn("/* second */", out)
        self.assertIn("// tail", out)  # the previous member keeps its trailing comment

    def test_removing_last_member_keeps_strict_json(self):
        out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=["three"])
        import json

        json.loads(
            jsonc.strip_jsonc(out).replace(",\n    }", "\n    }")
        )  # parses even without the tolerant reader
        self.assertNotIn("}, }", out)

    def test_replace_keeps_comments_above(self):
        out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], replaces={"one": {"y": [1, 2]}})
        self.assertIn("// first", out)
        self.assertEqual(self.servers(out)["one"], {"y": [1, 2]})
        self.assertEqual(self.servers(out)["two"], {"x": 2})

    def test_add_at_end_adds_missing_comma_and_comment(self):
        out = jsonc_edit.edit_object(
            self.DOC, ["mcp", "servers"], adds=[("four", {"x": 4}, "added by test")]
        )
        self.assertEqual(list(self.servers(out)), ["one", "two", "three", "four"])
        self.assertIn("// added by test", out)
        self.assertTrue(out.startswith(self.DOC[: self.DOC.index('"three"')]))

    def test_add_to_empty_object_and_unchanged_rest(self):
        doc = '{\n  // keep\n  "mcp": {\n    "servers": {}\n  },\n  "z": "//not a comment"\n}\n'
        out = jsonc_edit.edit_object(doc, ["mcp", "servers"], adds=[("n", {"a": 1}, None)])
        self.assertEqual(jsonc.loads(out)["mcp"]["servers"], {"n": {"a": 1}})
        self.assertIn("// keep", out)
        self.assertIn('"//not a comment"', out)

    def test_braces_and_comment_markers_inside_strings(self):
        doc = '{"mcp": {"servers": {"a": {"u": "x}//y"}, "b": {"v": "{"}}}}'
        out = jsonc_edit.edit_object(doc, ["mcp", "servers"], removes=["a"])
        self.assertEqual(jsonc.loads(out)["mcp"]["servers"], {"b": {"v": "{"}})

    def test_nested_edit_keeps_inner_comments_and_other_keys(self):
        doc = (
            '{\n "mcp": {"servers": {\n  "a": {\n   "type": "local",\n   // explains the command\n'
            '   "command": ["x", "y"],\n   "extra": 1\n  }\n }}}\n'
        )
        out = jsonc_edit.edit_object(
            doc,
            ["mcp", "servers", "a"],
            removes=["extra"],
            replaces={"command": ["x", "z"]},
            adds=[("env", {"K": "v"}, None)],
        )
        self.assertIn("// explains the command", out)
        self.assertEqual(
            jsonc.loads(out)["mcp"]["servers"]["a"],
            {"type": "local", "command": ["x", "z"], "env": {"K": "v"}},
        )

    def test_missing_path_or_member(self):
        with self.assertRaises(jsonc_edit.EditError):
            jsonc_edit.edit_object(self.DOC, ["nope"])
        with self.assertRaises(jsonc_edit.EditError):
            jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=["zzz"])


class TomlEditTests(unittest.TestCase):
    DOC = (
        'model = "x"\nlist = [\n  ["a"]\n]\n\n# about a\n[mcp_servers.a]\ncommand = "c"\nenv_vars = ["X"]\n\n'
        '[mcp_servers.a.env]\nK = "v"\n\n[mcp_servers.keep]\nurl = "k"\n\n[projects.\'C:\\x\']\ntrust = "t"\n'
    )

    def parse(self, text):
        import tomllib

        return tomllib.loads(text)

    def test_blocks_round_trip_exactly(self):
        self.assertEqual(toml_edit.join_blocks(toml_edit.split_blocks(self.DOC)), self.DOC)

    def test_remove_drops_subtables_and_leading_comment_only(self):
        out = toml_edit.edit(self.DOC, ["a"], [])
        d = self.parse(out)
        self.assertEqual(list(d["mcp_servers"]), ["keep"])
        self.assertNotIn("about a", out)
        self.assertIn("[projects.'C:\\x']", out)
        self.assertEqual(d["list"], [["a"]])

    def test_upsert_moves_into_region_and_is_stable(self):
        entry = {
            "command": "z",
            "args": ["1", "C:\\p q"],
            "startup_timeout_sec": 120,
            "env": {"A": "b\\c"},
        }
        once = toml_edit.edit(self.DOC, [], [("a", entry, "why")])
        d = self.parse(once)
        self.assertEqual(d["mcp_servers"]["a"], entry)
        self.assertEqual(d["mcp_servers"]["keep"], {"url": "k"})
        self.assertEqual(once.count(toml_edit.BEGIN), 1)
        self.assertIn("# why", once)
        twice = toml_edit.edit(once, [], [("a", entry, "why")])
        self.assertEqual(twice, once)  # same input, same output
        other = toml_edit.edit(once, [], [("b", {"url": "u"}, None)])
        self.assertEqual(
            self.parse(other)["mcp_servers"]["a"], entry
        )  # the region keeps what it already had
        self.assertEqual(self.parse(other)["mcp_servers"]["b"], {"url": "u"})

    def test_unclosed_region_is_refused(self):
        with self.assertRaises(toml_edit.TomlEditError):
            toml_edit.edit(
                '# >>> one-setup-all-agents >>>\n[mcp_servers.a]\nurl = "u"\n',
                [],
                [("a", {"url": "v"}, None)],
            )

    def test_bare_mcp_servers_table_is_refused(self):
        with self.assertRaises(toml_edit.TomlEditError):
            toml_edit.edit('[mcp_servers]\na = { url = "u" }\n', [], [("a", {"url": "v"}, None)])


# ---------------------------------------------------------------- apply on a fake home
class ApplyTests(WorkDirCase):
    def setUp(self):
        self.home = self.copy_fixture_home()
        self.before = digest(self.home)

    def servers(self, tool):
        return load_all(self.home)[tool].servers

    def test_dry_run_is_the_default_and_writes_nothing(self):
        rc, out, err = cli("apply", "--tool", FILE_TOOLS, home=self.home)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("dry-run: nothing was written", out)
        self.assertEqual(digest(self.home), self.before)
        self.assertFalse(locations.state_root(self.home).exists())  # a dry run creates no state

    def test_plan_content(self):
        rc, out, _ = cli("diff", "--all", "--json", home=self.home)
        plan = json.loads(out)
        ops = {t: {(o["op"], o["server"]) for o in p["ops"]} for t, p in plan.items()}
        common = {("add", "new-stdio"), ("add", "new-http"), ("update", "demo-remote")}
        self.assertEqual(ops["claude"], common | {("update", "demo-npx")})
        self.assertEqual(ops["agy"], common | {("update", "demo-npx")})
        self.assertEqual(ops["codex"], common)  # keep_live_env: demo-npx stays as it is
        self.assertEqual(
            ops["opencode"], common | {("remove", "playwright"), ("add", "opencode-only")}
        )
        self.assertIn("github", plan["claude"]["unmanaged"])
        self.assertEqual(plan["codex"]["same"].count("demo-browser-a"), 1)
        host = next(o for o in plan["claude"]["ops"] if o["server"] == "demo-npx")
        self.assertEqual(
            host["fields"], [{"field": "env_keys", "live": ["PATH", "USER_AGENT"], "wanted": []}]
        )

    def test_apply_file_tools_end_to_end(self):
        raws_before = {t: raw_of(self.home, t) for t in FILE_TOOLS.split(",")}
        rc, out, err = cli("apply", "--apply", "--tool", FILE_TOOLS, home=self.home)
        self.assertEqual((rc, err), (0, ""), out)
        for t in FILE_TOOLS.split(","):
            self.assertIn(f"[{t}] applied and verified", out)
        # every managed entry now matches its expected form; a re-plan finds nothing to do
        plans = build_plan(
            Source(SOURCE),
            load_all(self.home),
            applymod.read_ledger(self.home),
            FILE_TOOLS.split(","),
        )
        for t, p in plans.items():
            self.assertEqual([o.describe() for o in p.ops], [], t)
        # untouched entries are identical, key for key
        for t in FILE_TOOLS.split(","):
            raws = raw_of(self.home, t)
            skip = {"demo-remote", "playwright"} | ({"demo-npx"} if t != "codex" else set())
            for name, entry in raws_before[t].items():
                if name not in skip:
                    self.assertEqual(raws[name], entry, f"{t}/{name} changed")
        self.assertNotIn("playwright", self.servers("opencode"))
        self.assertEqual(self.servers("opencode")["opencode-only"].command, "npx")
        self.assertNotIn("opencode-only", self.servers("codex"))
        self.assertEqual(self.servers("agy")["demo-npx"].env_keys, [])
        self.assertEqual(self.servers("codex")["demo-npx"].env_keys, ["PATH", "USER_AGENT"])
        self.assertEqual(self.servers("codex")["new-stdio"].env_keys, ["MODE"])
        self.assertEqual(
            self.servers("agy")["demo-remote"].url, "https://demo-remote.example.test/api/mcp/v2"
        )

    def test_opencode_file_keeps_comments_crlf_and_everything_else(self):
        path = self.home / ".config" / "opencode" / "opencode.jsonc"
        before = path.read_bytes().decode("utf-8")
        self.assertIn("\r\n", before)
        cli("apply", "--apply", "--tool", "opencode", home=self.home)
        after = path.read_bytes().decode("utf-8")
        self.assertEqual(after.count("\n"), after.count("\r\n"))  # still CRLF everywhere
        for needle in (
            "// JSONC fixture: comments",
            "/* inline block comment */",
            '"$schema": "https://opencode.ai/config.json"',
            "// remote server: OpenCode uses type",
            "// a trailing comma follows",
        ):  # updated entries are edited key by key
            self.assertIn(needle, after)
        self.assertNotIn("/* playwright exists only here", after)  # went with the retired entry
        head = before[: before.index('"demo-remote"')]
        self.assertTrue(after.startswith(head))  # nothing above the first managed entry moved
        self.assertIn(
            "// added to every tool", after
        )  # description became a comment on the new entry
        jsonc.loads(after)

    def test_codex_file_keeps_other_tables_and_internal_server(self):
        path = self.home / ".codex" / "config.toml"
        before = path.read_text(encoding="utf-8")
        cli("apply", "--apply", "--tool", "codex", home=self.home)
        after = path.read_text(encoding="utf-8")
        self.assertIn('model = "fake-model"', after)
        self.assertIn("[projects.'C:\\fake\\project']\ntrust_level = \"trusted\"", after)
        node_block = before[
            before.index("[mcp_servers.node_repl]") : before.index("# remote server")
        ]
        self.assertIn(node_block.strip(), after)  # tool-internal server kept byte for byte
        self.assertEqual(after.count(toml_edit.BEGIN), 1)
        self.assertEqual(after.count(toml_edit.END), 1)
        self.assertIn(
            "FAKE-PATH-MARKER", after
        )  # the kept env is still in the file (never printed)
        self.assertIn("[mcp_servers.literal-header]", after)  # unmanaged entry untouched

    def test_agy_file_is_edited_in_place(self):
        path = self.home / ".gemini" / "config" / "mcp_config.json"
        before = json.loads(path.read_text(encoding="utf-8"))
        cli("apply", "--apply", "--tool", "agy", home=self.home)
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            list(after["mcpServers"])[: len(before["mcpServers"])], list(before["mcpServers"])
        )
        self.assertNotIn("env", after["mcpServers"]["demo-npx"])
        self.assertEqual(after["mcpServers"]["secret-args"], before["mcpServers"]["secret-args"])
        self.assertIn("serverUrl", after["mcpServers"]["new-http"])

    def test_second_apply_changes_nothing(self):
        cli("apply", "--apply", "--tool", FILE_TOOLS, home=self.home)
        snap = digest(self.home)
        rc, out, _ = cli("apply", "--apply", "--tool", FILE_TOOLS, home=self.home)
        self.assertEqual(rc, 0)
        self.assertIn("no changes", out)
        self.assertEqual(digest(self.home), snap)  # not even a new backup folder
        rc, out, _ = cli("verify", home=self.home)
        self.assertIn(
            rc, (0, 2)
        )  # claude still differs here (not applied); the file tools must be clean
        self.assertNotIn("[opencode]\n  remove", out)
        rc, out, _ = cli("diff", "--all", "--json", home=self.home)
        plan = json.loads(out)
        for t in FILE_TOOLS.split(","):
            self.assertEqual(plan[t]["ops"], [], t)

    def test_backup_and_manifest(self):
        cli("apply", "--apply", "--tool", FILE_TOOLS, home=self.home)
        (b,) = list(locations.backups_dir(self.home).glob("backup-*-sync"))
        m = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["status"], "complete")
        for t in FILE_TOOLS.split(","):
            e = m["tools"][t]
            self.assertEqual(applymod.sha((b / e["backup"]).read_bytes()), e["sha_before"])
            self.assertEqual(applymod.sha(Path(e["path"]).read_bytes()), e["sha_after"])
            self.assertNotEqual(e["sha_before"], e["sha_after"])

    def test_rollback_restores_every_file_byte_for_byte(self):
        cli("apply", "--apply", "--tool", FILE_TOOLS, home=self.home)
        self.assertNotEqual(digest(self.home), self.before)
        (b,) = list(locations.backups_dir(self.home).glob("backup-*-sync"))
        rc, out, err = cli("rollback", str(b), home=self.home)
        self.assertEqual((rc, err), (0, ""), out)
        state_rel = locations.state_root(self.home).relative_to(self.home).as_posix()
        after = {
            k: v
            for k, v in digest(self.home).items()
            if not k.replace("\\", "/").startswith(state_rel)
        }
        self.assertEqual(after, self.before)
        self.assertFalse(applymod.ledger_path(self.home).exists())
        rc, out, _ = cli("apply", "--tool", FILE_TOOLS, home=self.home)  # and the plan is back
        self.assertIn("update  demo-remote", out)

    def test_rollback_refuses_when_a_file_changed_afterwards(self):
        cli("apply", "--apply", "--tool", "agy", home=self.home)
        path = self.home / ".gemini" / "config" / "mcp_config.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        (b,) = list(locations.backups_dir(self.home).glob("backup-*-sync"))
        rc, out, _ = cli("rollback", str(b), home=self.home)
        self.assertEqual(rc, 1)
        self.assertIn("changed after the apply", out)
        rc, out, _ = cli("rollback", str(b), "--force", home=self.home)
        self.assertEqual(rc, 0)

    def test_tool_and_server_filters(self):
        rc, out, _ = cli(
            "apply", "--apply", "--tool", "agy", "--server", "new-http", home=self.home
        )
        self.assertEqual(rc, 0)
        s = self.servers("agy")
        self.assertIn("new-http", s)
        self.assertNotIn("new-stdio", s)
        self.assertEqual(
            s["demo-remote"].url, "https://demo-remote.example.test/api/mcp"
        )  # not selected: unchanged
        self.assertEqual(
            self.servers("codex").keys(), load_all(FIXTURE_HOME)["codex"].servers.keys()
        )
        self.assertEqual(cli("apply", "--tool", "nope", home=self.home)[0], 1)

    def test_stale_servers_are_removed_only_if_the_tool_wrote_them(self):
        cli("apply", "--apply", "--tool", FILE_TOOLS, home=self.home)
        smaller = json.loads(jsonc.strip_jsonc(SOURCE.read_text(encoding="utf-8")))
        del smaller["servers"]["new-stdio"]
        del smaller["servers"][
            "demo-remote"
        ]  # demo-remote was in the live files before the tool, but the tool wrote it (ledger)
        src2 = write_source(self.make_home(), json.dumps(smaller))
        rc, out, _ = cli("apply", "--apply", "--tool", FILE_TOOLS, home=self.home, source=src2)
        self.assertEqual(rc, 0, out)
        self.assertIn("remove  new-stdio   (stale)", out)
        for t in FILE_TOOLS.split(","):
            s = self.servers(t)
            self.assertNotIn("new-stdio", s)
            self.assertNotIn("demo-remote", s)
            self.assertIn("new-http", s)
        self.assertIn(
            "github", self.servers("codex")
        )  # never in the source or the ledger: untouched
        self.assertIn("node_repl", self.servers("codex"))

    def test_protected_server_cannot_be_managed(self):
        bad = json.loads(jsonc.strip_jsonc(SOURCE.read_text(encoding="utf-8")))
        bad["unmanaged"] = {}
        bad["servers"]["node_repl"] = {"transport": "stdio", "command": "x"}
        rc, out, err = cli(
            "apply",
            "--tool",
            "codex",
            home=self.home,
            source=write_source(self.make_home(), json.dumps(bad)),
        )
        self.assertEqual(rc, 1)
        self.assertIn("protected", err)
        self.assertEqual(digest(self.home), self.before)

    def test_lock(self):
        lock = locations.lock_file(self.home)
        lock.parent.mkdir(parents=True)
        lock.write_text("1 1", encoding="utf-8")
        rc, out, err = cli("apply", "--apply", "--tool", "agy", home=self.home)
        self.assertEqual(rc, 1)
        self.assertIn("another run", err)
        self.assertTrue(lock.exists())
        old = time.time() - 3600
        os.utime(lock, (old, old))  # a stale lock is ignored
        self.assertEqual(cli("apply", "--apply", "--tool", "agy", home=self.home)[0], 0)
        self.assertFalse(lock.exists())

    def test_verify_exit_codes(self):
        self.assertEqual(cli("verify", home=self.home)[0], 2)
        cli("apply", "--apply", "--tool", FILE_TOOLS, home=self.home)
        rc, out, _ = cli("verify", home=self.home)
        self.assertEqual(
            out.count("  no changes"), 3
        )  # codex, opencode, agy; only claude was not applied

    # --- failure handling, in process
    def run_inprocess(self, tools="agy", mutate=None):
        lines = []
        if mutate:
            mutate()
        rc = applymod.run_apply(self.home, Source(SOURCE), [tools], None, True, None, lines.append)
        return rc, "\n".join(lines)

    def test_file_changed_between_read_and_write_is_not_overwritten(self):
        path = self.home / ".gemini" / "config" / "mcp_config.json"
        real_prepare = applymod.prepare

        def sneaky(tool, plan, source):
            ch = real_prepare(tool, plan, source)
            path.write_text(path.read_text(encoding="utf-8") + " \n", encoding="utf-8")
            return ch

        applymod.prepare = sneaky
        try:
            rc, out = self.run_inprocess("agy")
        finally:
            applymod.prepare = real_prepare
        self.assertEqual(rc, 1)
        self.assertIn("changed while all-agents was working", out)
        self.assertTrue(
            path.read_text(encoding="utf-8").endswith("} \n")
        )  # the other writer's change survived

    def test_failed_verification_restores_the_file(self):
        path = self.home / ".codex" / "config.toml"
        original = path.read_bytes()
        real = applymod._verify_after

        def boom(*a, **k):
            raise applymod.ApplyError("forced failure")

        applymod._verify_after = boom
        try:
            rc, out = self.run_inprocess("codex")
        finally:
            applymod._verify_after = real
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", out)
        self.assertEqual(path.read_bytes(), original)

    def test_a_bad_edit_is_caught_before_anything_is_written(self):
        real = applymod.edit_agy
        applymod.edit_agy = lambda text, plan: "{ broken"
        try:
            with self.assertRaises(applymod.ApplyError):
                self.run_inprocess("agy")
        finally:
            applymod.edit_agy = real
        self.assertEqual(digest(self.home), self.before)

    # --- secrecy
    def test_no_secret_or_env_value_in_any_output(self):
        outs = []
        for args in (
            ["apply", "--tool", FILE_TOOLS],
            ["apply", "--apply", "--tool", FILE_TOOLS],
            ["diff", "--all"],
            ["diff", "--all", "--json"],
            ["verify"],
        ):
            rc, o, e = cli(*args, home=self.home)
            outs += [o, e]
        (b,) = list(locations.backups_dir(self.home).glob("backup-*-sync"))
        outs += list(cli("rollback", str(b), home=self.home)[1:])
        text = "\n".join(outs)
        for secret in FIXTURE_SECRETS + FIXTURE_ENV_VALUES:
            self.assertNotIn(secret, text, f"leaked {secret}")
        self.assertIn("update  demo-remote", text)  # not vacuous


# ---------------------------------------------------------------- import
class ImportTests(WorkDirCase):
    def test_import_refuses_servers_with_masked_secrets(self):
        rc, out, err = cli("import", home=FIXTURE_HOME)
        self.assertEqual(rc, 1)
        self.assertIn("secret-args", err)
        for s in FIXTURE_SECRETS:
            self.assertNotIn(s, out + err)

    def test_import_draft_is_valid_and_has_names_only(self):
        args = [
            "import",
            "--exclude",
            "secret-args",
            "--exclude",
            "literal-header",
            "--retire",
            "opencode:playwright",
            "--keep-live-env",
            "codex",
            "--auth",
            "demo-remote=oauth",
        ]  # github/bearer-api come along (env names are listed)
        rc, out, err = cli(*args, home=FIXTURE_HOME)
        self.assertEqual(rc, 0, err)
        self.assertIn("note: github: env variable names not imported", err)
        for s in FIXTURE_SECRETS + FIXTURE_ENV_VALUES:
            self.assertNotIn(s, out)
        self.assertIn("PATH, USER_AGENT", out)  # names are listed in a comment
        self.assertIn('"unmanaged": {"codex": ["node_repl"]}', out)
        target = self.make_home() / "mcp" / "servers.jsonc"
        rc, out2, err = cli(*args, "--write", str(target), home=FIXTURE_HOME)
        self.assertEqual(rc, 0, err)
        src = Source(target)
        self.assertEqual(src.servers["demo-remote"].auth, "oauth")
        self.assertEqual(src.servers["demo-npx"].keep_live_env, ["codex"])
        self.assertEqual(src.servers["demo-browser-a"].startup_timeout_sec, 120)
        self.assertNotIn("node_repl", src.servers)
        self.assertNotIn("playwright", src.servers)
        self.assertEqual(src.retired, {"opencode": ["playwright"]})
        rc, _, err = cli(*args, "--write", str(target), home=FIXTURE_HOME)
        self.assertEqual(rc, 1)  # will not overwrite
        self.assertIn("already exists", err)

    def test_imported_source_round_trips_to_zero_diff(self):
        home = self.make_home() / "home"
        shutil.copytree(FIXTURE_HOME, home)
        target = self.make_home() / "servers.jsonc"
        args = [
            "import",
            "--exclude",
            "secret-args",
            "--exclude",
            "literal-header",
            "--exclude",
            "github",
            "--exclude",
            "bearer-api",
            "--retire",
            "opencode:playwright",
            "--keep-live-env",
            "codex",
            "--write",
            str(target),
        ]
        self.assertEqual(cli(*args, home=home)[0], 0)
        plan = json.loads(cli("diff", "--all", "--json", home=home, source=target)[1])
        # only what the decisions imply: playwright leaves OpenCode, the demo-npx env leaves Claude and agy
        got = {t: sorted((o["op"], o["server"]) for o in p["ops"]) for t, p in plan.items()}
        self.assertEqual(
            got,
            {
                "claude": [("update", "demo-npx")],
                "codex": [],
                "agy": [("update", "demo-npx")],
                "opencode": [("remove", "playwright")],
            },
        )


# ---------------------------------------------------------------- Claude Code through its real CLI


if __name__ == "__main__":
    unittest.main()
