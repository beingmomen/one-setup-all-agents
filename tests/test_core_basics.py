"""Tests of the ported core: JSONC, masking, parsers, status, failures and safety (no output ever holds a secret).

Everything runs against the fake home in tests/fixtures/home; nothing reads or writes the real home.
"""

import json
import unittest

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import jsonc, mask, report
from one_setup_all_agents._core.parsers import load_all, load_tool
from support import (
    FIXTURE_ENV_VALUES,
    FIXTURE_HOME,
    FIXTURE_SECRETS,
    WORK,
    WorkDirCase,
    digest,
    run_cli,
)


# ---------------------------------------------------------------- JSONC
class JsoncTests(unittest.TestCase):
    def test_comments_and_trailing_commas(self):
        text = '{\n // line\n "a": 1, /* block */ "b": [1, 2,],\n "c": {"d": 3,},\n}\n'
        self.assertEqual(jsonc.loads(text), {"a": 1, "b": [1, 2], "c": {"d": 3}})

    def test_comment_markers_inside_strings_are_kept(self):
        text = '{"u": "https://x.test/a//b", "s": "/* not a comment */", "e": "q\\"//still string"}'
        self.assertEqual(jsonc.loads(text), json.loads(text))

    def test_crlf_and_bom(self):
        text = '\ufeff{\r\n  // c\r\n  "a": 1,\r\n}\r\n'
        self.assertEqual(jsonc.loads(text), {"a": 1})

    def test_comma_before_closer_inside_string_is_kept(self):
        self.assertEqual(jsonc.loads('{"a": "x,}"}'), {"a": "x,}"})

    def test_unterminated_block_comment(self):
        with self.assertRaises(ValueError):
            jsonc.loads('{"a": 1 /* oops')

    def test_error_line_number_survives_block_comments(self):
        try:
            jsonc.loads('{\n/* a\nb\nc */\n"x": nope\n}')
        except json.JSONDecodeError as e:
            self.assertEqual(e.lineno, 5)
        else:
            self.fail("expected a JSON error")


# ---------------------------------------------------------------- masking
class MaskTests(unittest.TestCase):
    def test_token_shapes_built_at_runtime(self):
        shapes = [
            "ghp_" + "Ab1" * 8,
            "github_pat_" + "Ab1_" * 6,
            "nfp_" + "Zy9" * 8,
            "sk-" + "Qw3" * 8,
            "AIza" + "Rt5" * 9,
            "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
        ]
        for t in shapes:
            out = mask.mask_text(f"prefix {t} suffix")
            self.assertNotIn(t, out)
            self.assertIn(mask.MASK, out)

    def test_bearer_and_userinfo_and_query(self):
        self.assertEqual(mask.mask_text("Bearer FAKE-1"), "Bearer ***")
        self.assertEqual(mask.mask_text("Bearer ${TOKEN_ENV}"), "Bearer ${TOKEN_ENV}")
        self.assertEqual(mask.mask_text("Bearer {env:TOKEN_ENV}"), "Bearer {env:TOKEN_ENV}")
        self.assertEqual(
            mask.mask_text("https://user:FAKE-PW@host.test/x"), "https://user:***@host.test/x"
        )
        self.assertEqual(
            mask.mask_text("https://h.test/m?api_key=FAKE-K&x=1"),
            "https://h.test/m?api_key=***&x=1",
        )
        self.assertEqual(mask.mask_text("--api-key=FAKE-K"), "--api-key=***")
        self.assertEqual(mask.mask_text("GITHUB_TOKEN=FAKE-T"), "GITHUB_TOKEN=***")
        self.assertEqual(
            mask.mask_text("GITHUB_TOKEN=${GITHUB_TOKEN}"), "GITHUB_TOKEN=${GITHUB_TOKEN}"
        )
        self.assertEqual(mask.mask_text("Authorization: FAKE-RAW"), "Authorization: ***")

    def test_value_after_secret_flag(self):
        self.assertEqual(
            mask.mask_args(["x", "--token", "FAKE-V", "--next"]), ["x", "--token", "***", "--next"]
        )
        self.assertEqual(mask.mask_args(["--password", "${PW}"]), ["--password", "${PW}"])
        self.assertEqual(mask.mask_args(["--token", "--other"]), ["--token", "--other"])

    def test_ordinary_values_are_untouched(self):
        keep = [
            "--package=@demo/mcp@latest",
            "demo-npx-mcp",
            "/c",
            "cmd",
            "npx.cmd",
            r"C:\demo\scripts\demo-browser-a-shared.cmd",
            "https://demo-remote.example.test/api/mcp",
            "@modelcontextprotocol/server-github",
            "--browserUrl=http://127.0.0.1:9222",
            "--author=someone",
        ]
        for k in keep:
            self.assertEqual(mask.mask_text(k), k)

    def test_long_opaque_argument_is_masked(self):
        self.assertEqual(mask.mask_text("a1" * 20), "***")


# ---------------------------------------------------------------- parsers
class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_all(FIXTURE_HOME)

    def srv(self, tool, name):
        return self.cfg[tool].servers[name]

    def test_all_files_parse(self):
        for t, c in self.cfg.items():
            self.assertIsNone(c.error, f"{t}: {c.error}")

    def test_server_counts(self):
        self.assertEqual(
            {t: c.count for t, c in self.cfg.items()},
            {"claude": 7, "codex": 8, "opencode": 7, "agy": 7},
        )

    def test_claude_ignores_project_entries(self):
        self.assertNotIn("project-only-server", self.cfg["claude"].servers)

    def test_wrapper_cmd_c_commands_are_the_same_everywhere(self):
        for name in ("demo-browser-a", "demo-browser-b"):
            for t in self.cfg:
                s = self.srv(t, name)
                self.assertEqual(
                    (s.transport, s.command, s.args[0]), ("stdio", "cmd", "/c"), f"{t}/{name}"
                )
            self.assertEqual(len({json.dumps(self.srv(t, name).args) for t in self.cfg}), 1, name)

    def test_remote_quirks_normalize_to_http_with_the_same_url(self):
        urls = set()
        for t in self.cfg:  # codex: url, opencode: type remote, agy: serverUrl, claude: type http
            s = self.srv(t, "demo-remote")
            self.assertEqual(s.transport, "http", t)
            self.assertIsNone(s.command, t)
            urls.add(s.url)
        self.assertEqual(urls, {"https://demo-remote.example.test/api/mcp"})

    def test_demo_remote_is_identical_in_all_four(self):
        sigs = {report._sig(self.srv(t, "demo-remote")) for t in self.cfg}
        self.assertEqual(len(sigs), 1)

    def test_big_env_keeps_names_only(self):
        s = self.srv("claude", "demo-npx")
        self.assertEqual(s.env_keys, ["PATH", "USER_AGENT"])
        self.assertGreater(s.env_size, 1500)
        self.assertEqual(self.srv("opencode", "demo-npx").env_keys, [])
        for t in ("codex", "agy"):
            self.assertEqual(self.srv(t, "demo-npx").env_keys, ["PATH", "USER_AGENT"])

    def test_codex_internal_server_and_extras(self):
        n = self.srv("codex", "node_repl")
        self.assertTrue(n.internal)
        self.assertEqual(
            n.env_pass, ["BROWSER_USE_ACCESS_PIPE_PATH", "CODEX_WINDOWS_REGISTERED_CORE"]
        )
        self.assertEqual(n.startup_timeout_sec, 120)
        self.assertEqual(self.srv("codex", "demo-browser-a").startup_timeout_sec, 120)

    def test_opencode_jsonc_details(self):
        pw = self.srv("opencode", "playwright")
        self.assertFalse(pw.enabled)
        self.assertEqual((pw.command, pw.args), ("npx", ["@playwright/mcp@latest"]))
        gh = self.srv("opencode", "github")
        self.assertEqual(gh.env_keys, ["GITHUB_PERSONAL_ACCESS_TOKEN"])

    def test_auth_detection(self):
        self.assertEqual(self.srv("claude", "bearer-api").auth, "bearer-env")
        self.assertEqual(self.srv("codex", "bearer-api").auth, "bearer-env")  # bearer_token_env_var
        self.assertEqual(self.srv("opencode", "bearer-api").auth, "bearer-env")  # {env:VAR}
        self.assertEqual(self.srv("agy", "bearer-api").auth, "bearer-env")
        self.assertEqual(self.srv("codex", "literal-header").auth, "header-literal")
        self.assertEqual(self.srv("claude", "demo-browser-a").auth, "none")
        self.assertIsNone(
            self.srv("claude", "demo-remote").auth
        )  # OAuth is not visible in the config files

    def test_literal_values_raise_warnings_without_the_value(self):
        warns = [w for c in self.cfg.values() for s in c.servers.values() for w in s.warnings]
        self.assertTrue(any("GITHUB_PERSONAL_ACCESS_TOKEN" in w for w in warns))
        self.assertTrue(any("Authorization" in w for w in warns))
        for w in warns:
            for secret in FIXTURE_SECRETS:
                self.assertNotIn(secret, w)

    def test_args_and_urls_are_masked_in_the_model(self):
        s = self.srv("claude", "secret-args")
        flat = json.dumps(s.to_dict())
        for secret in FIXTURE_SECRETS:
            self.assertNotIn(secret, flat)
        self.assertIn("--token", s.args)
        self.assertEqual(s.args[s.args.index("--token") + 1], "***")

    def test_opencode_v1_flat_layout_fallback(self):
        from one_setup_all_agents._core.parsers import parse_opencode

        out = parse_opencode(
            {
                "mcp": {
                    "x": {"type": "local", "command": ["node", "a.js"], "environment": {"K": "v"}}
                }
            }
        )
        self.assertEqual(
            (out["x"].command, out["x"].args, out["x"].env_keys), ("node", ["a.js"], ["K"])
        )


# ---------------------------------------------------------------- status / diff
class StatusTests(unittest.TestCase):
    def test_status_table_states(self):
        rc, out, err = run_cli("status")
        self.assertEqual((rc, err), (0, ""))
        data = json.loads(run_cli("status", "--json")[1])
        s = data["servers"]
        self.assertEqual(
            s["demo-npx"],
            {"claude": "present", "codex": "present", "opencode": "differs", "agy": "present"},
        )
        self.assertEqual(
            s["playwright"],
            {"claude": "missing", "codex": "missing", "opencode": "present", "agy": "missing"},
        )
        self.assertEqual(s["node_repl"]["codex"], "internal")
        self.assertEqual(s["literal-header"]["claude"], "missing")
        for name in ("demo-browser-a", "demo-browser-b", "demo-remote", "github", "bearer-api"):
            self.assertEqual(set(s[name].values()), {"present"}, name)
        self.assertEqual(
            {t: v["count"] for t, v in data["tools"].items()},
            {"claude": 7, "codex": 8, "opencode": 7, "agy": 7},
        )
        for word in ("present", "missing", "DIFFERS", "internal"):
            self.assertIn(word, out)

    def test_status_explains_the_difference(self):
        d = json.loads(run_cli("status", "--json")[1])["differences"]
        self.assertEqual(len(d), 1)
        self.assertEqual(
            (d[0]["tool"], d[0]["field"], d[0]["servers"]), ("opencode", "env_keys", ["demo-npx"])
        )
        self.assertEqual(d[0]["reference"], ["PATH", "USER_AGENT"])
        self.assertEqual(d[0]["value"], [])

    def test_count_explanation(self):
        c = json.loads(run_cli("status", "--json")[1])["counts"]
        self.assertEqual(c["codex"]["internal"], ["node_repl"])
        self.assertEqual(c["opencode"]["extra"], ["playwright"])
        self.assertEqual(c["claude"]["shared"], 6)

    def test_flags_work_before_and_after_the_subcommand(self):
        a = run_cli("status", "--json")[1]
        rc, b, _ = run_cli("--json", "status")
        self.assertEqual(json.loads(a), json.loads(b))

    def test_home_flag_beats_the_environment(self):
        bogus = WORK / "no-such-home"
        rc, out, _ = run_cli("status", "--json", env_home=bogus)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["tools"]["claude"]["count"], 7)

    def test_diff_shows_field_differences(self):
        rc, out, _ = run_cli("diff", "demo-npx")
        self.assertEqual(rc, 0)
        self.assertIn("env_keys  [DIFFERS]", out)
        self.assertIn("command  [same]", out)
        self.assertIn('["PATH", "USER_AGENT"]', out)

    def test_diff_json_for_a_single_tool_server(self):
        rc, out, _ = run_cli("diff", "playwright", "--json")
        d = json.loads(out)
        self.assertEqual(d["not_configured_in"], ["claude", "codex", "agy"])
        self.assertEqual(d["tools"]["opencode"]["enabled"], False)

    def test_diff_unknown_server(self):
        rc, out, err = run_cli("diff", "demo-remot")
        self.assertEqual(rc, 1)
        self.assertIn("demo-remote", err)

    def test_diff_internal_server_is_marked(self):
        out = run_cli("diff", "node_repl")[1]
        self.assertIn("tool-internal in: codex", out)


# ---------------------------------------------------------------- failures
class FailureTests(WorkDirCase):
    def test_missing_files_exit_1_but_still_report(self):
        home = self.make_home()
        rc, out, _ = run_cli("status", home=home)
        self.assertEqual(rc, 1)
        self.assertIn("file not found", out)

    def test_broken_json_reports_position_not_content(self):
        home = self.copy_fixture_home()
        (home / ".gemini" / "config" / "mcp_config.json").write_text(
            '{"mcpServers": {"x": {"command": "FAKE-BROKEN-SECRET-eee555"', encoding="utf-8"
        )
        (home / ".codex" / "config.toml").write_text(
            '[mcp_servers.x]\ncommand = "FAKE-BROKEN-SECRET-fff666', encoding="utf-8"
        )
        rc, out, err = run_cli("status", home=home)
        self.assertEqual(rc, 1)
        self.assertIn("cannot parse", out)
        self.assertNotIn("FAKE-BROKEN-SECRET", out + err)
        self.assertEqual(
            json.loads(run_cli("status", "--json", home=home)[1])["servers"]["demo-remote"]["agy"],
            "error",
        )

    def test_opencode_json_fallback_name(self):
        home = self.copy_fixture_home()
        oc = home / ".config" / "opencode"
        (oc / "opencode.jsonc").rename(oc / "opencode.json")
        self.assertEqual(load_tool("opencode", home).count, 7)


# ---------------------------------------------------------------- safety
class SafetyTests(WorkDirCase):
    def all_outputs(self, home, run=run_cli):
        outs = []
        for flags in (["status"], ["status", "--json"]):
            rc, o, e = run(*flags, home=home)
            outs += [o, e]
        names = json.loads(run("status", "--json", home=home)[1])["servers"]
        for n in names:
            for flags in ([], ["--json"]):
                rc, o, e = run("diff", n, *flags, home=home)
                outs += [o, e]
        return "\n".join(outs)

    def test_no_fixture_secret_or_env_value_appears_in_any_output(self):
        text = self.all_outputs(FIXTURE_HOME)
        for secret in FIXTURE_SECRETS + FIXTURE_ENV_VALUES:
            self.assertNotIn(secret, text, f"leaked: {secret}")
        # the check is not vacuous: names and masks are visible
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", text)
        self.assertIn("--token", text)
        self.assertIn("***", text)

    def test_runtime_built_token_shapes_never_appear(self):
        home = self.copy_fixture_home()
        gh = "ghp_" + "Ab1" * 8
        pat = "github_pat_" + "Cd2_" * 6
        nfp = "nfp_" + "Ef3" * 8
        cfg = home / ".gemini" / "config" / "mcp_config.json"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data["mcpServers"]["extra"] = {
            "command": "npx",
            "args": ["srv", f"--auth={gh}", f"--url=https://u:{pat}@h.test/p?token={nfp}"],
            "env": {"SOME_TOKEN": nfp, "OTHER": "v" * 40},
        }
        data["mcpServers"]["extra-remote"] = {
            "serverUrl": f"https://h.test/m?access_token={gh}",
            "headers": {"X-Key": pat},
        }
        cfg.write_text(json.dumps(data), encoding="utf-8")
        text = self.all_outputs(home)
        for secret in (gh, pat, nfp, "v" * 40):
            self.assertNotIn(secret, text)
        self.assertIn("extra-remote", text)

    def test_commands_do_not_modify_the_config_files(self):
        home = self.copy_fixture_home()
        before = digest(home)

        def run_read_only(*args, home):
            target = home
            before_command = digest(target)
            rc, out, err = run_cli(*args, home=home)
            after_command = digest(target)
            added = sorted(after_command.keys() - before_command.keys())
            removed = sorted(before_command.keys() - after_command.keys())
            modified = sorted(
                path
                for path in before_command.keys() & after_command.keys()
                if before_command[path] != after_command[path]
            )
            self.assertEqual(
                (rc, added, removed, modified),
                (0, [], [], []),
                f"argv={args!r}; expected exit 0 and no fixture changes; "
                f"actual exit={rc}, added={added!r}, removed={removed!r}, modified={modified!r}",
            )
            return rc, out, err

        self.all_outputs(home, run=run_read_only)
        self.assertEqual(digest(home), before)

    def test_fixture_files_are_untouched_by_the_run(self):
        before = digest(FIXTURE_HOME)
        run_cli("status")
        run_cli("diff", "github")
        self.assertEqual(digest(FIXTURE_HOME), before)


if __name__ == "__main__":
    unittest.main()
