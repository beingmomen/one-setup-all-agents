"""Tests for _core.mask: credential and path masking."""

import unittest

import _bootstrap  # noqa: F401
from one_setup_all_agents._core import mask


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
            mask.mask_args(["x", "--token", "FAKE-V", "--next"]),
            ["x", "--token", "***", "--next"],
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

    def test_a_fallback_after_a_reference_is_masked_and_a_plain_reference_is_kept(self):
        for text, want in (
            ("Bearer ${T}", "Bearer ${T}"),
            ("Bearer ${T:-demo-pass}", "Bearer ***"),
            ("token=${T}&x=1", "token=${T}&x=1"),
            ("token=${T:-demo-pass}&x=1", "token=***&x=1"),
            ("https://u:${P}@host", "https://u:${P}@host"),
            ("https://u:${P:-demo}@host", "https://u:***@host"),
            ("token=${TOKEN:-demo&42}", "token=***"),
            ("${UPSTREAM:-demo42}", "${UPSTREAM:-***}"),
            ("https://u:${PREFIX};demo-suffix@host", "https://u:***@host"),
            ("${PRIMARY:-${PREFIX}demo-suffix}", "${PRIMARY:-***}"),
            ("${UNCLOSED:-demo", "${UNCLOSED:-***}"),
            ("Bearer ${T}x", "Bearer ***"),
            ("Bearer %20demo%", "Bearer ***"),
            ("https://u:%2Fdemo%@host", "https://u:***@host"),
            ("Bearer %TOKEN%", "Bearer %TOKEN%"),
        ):
            with self.subTest(text=text):
                self.assertEqual(mask.mask_text(text), want)
        self.assertEqual(mask.mask_args(["--token", "${T:-demo-pass}"]), ["--token", "***"])
        self.assertEqual(mask.mask_args(["--token", "${T}"]), ["--token", "${T}"])

    def test_a_fallback_is_masked_across_escaped_braces_and_lines(self):
        self.assertEqual(mask.mask_text(r"${V:-prefix\}demo-tail}"), "${V:-***}")
        self.assertEqual(mask.mask_fallbacks("a ${V:-x\ndemo-tail} b"), "a ${V:-***} b")

    def test_a_json_argument_is_masked_field_by_field(self):
        self.assertEqual(
            mask.mask_args(['{"api_key":"demo-short"}', '{"mode":"x"}']),
            ['{"api_key": "***"}', '{"mode":"x"}'],
        )

    def test_mask_json_allows_a_scheme_only_in_headers_and_reads_lists_as_command_lines(self):
        cases = (
            ({"env": {"API_TOKEN": "demo2 ${FOO}"}}, {"env": {"API_TOKEN": "***"}}),
            ({"headers": {"Authorization": "Bearer ${T}"}}, {"headers": {"Authorization": "Bearer ${T}"}}),
            ({"headers": {"Authorization": "demo\n${X}"}}, {"headers": {"Authorization": "***"}}),
            ({"env": {"API_TOKEN": ""}}, {"env": {"API_TOKEN": ""}}),
            ({"args": ["--token", "demo2", {"a": 1}]}, {"args": ["--token", "***", {"a": 1}]}),
            ({"extra": ["--token", {"v": "demo2"}]}, {"extra": ["--token", "***"]}),
        )
        for doc, want in cases:
            with self.subTest(doc=doc):
                self.assertEqual(mask.mask_json(doc), want)

    def test_the_scheme_rule_matches_the_model(self):
        from one_setup_all_agents.model import HEADER_SCHEME_PATTERN

        self.assertEqual(mask._SCHEME.pattern, r"\A" + HEADER_SCHEME_PATTERN + r"\Z")

    def test_a_json_key_written_twice_masks_the_whole_argument(self):
        doc = '{"api_key":"demo-literal","api_key":"${API_KEY}"}'
        self.assertEqual(mask.mask_args([doc]), ["***"])
        with self.assertRaises(mask.DuplicateKeyError):
            mask.loads_strict(doc)

    def test_cookie_and_authorization_flags_and_assignments_are_secret(self):
        self.assertEqual(mask.mask_args(["--cookie", "session=demo"]), ["--cookie", "***"])
        self.assertEqual(mask.mask_args(["--authorization", "demo"]), ["--authorization", "***"])
        self.assertEqual(mask.mask_text("cookie=demo-session"), "cookie=***")

    def test_json_the_parser_cannot_inspect_is_masked_whole(self):
        huge = '{"api_key": "demo-value", "n": ' + "9" * 4400 + "}"
        self.assertEqual(mask.mask_args([huge]), ["***"])
        deep = "[" * 1100 + "]" * 1100
        self.assertEqual(mask.mask_args([deep]), ["***"])

    def test_a_dash_value_after_a_secret_flag_is_masked_but_a_flag_is_not(self):
        self.assertEqual(mask.mask_args(["--token", "-123456"]), ["--token", "***"])
        self.assertEqual(mask.mask_args(["--token", "--verbose"]), ["--token", "--verbose"])
        self.assertEqual(mask.mask_args(["--token", "-v"]), ["--token", "-v"])
        self.assertEqual(mask.mask_args(["--token", "-demo-value"]), ["--token", "***"])
        self.assertEqual(mask.mask_json({"args": ["--token", "-123456"]})["args"][1], "***")

    def test_json_attached_to_a_flag_is_judged_field_by_field(self):
        self.assertEqual(
            mask.mask_args(['--config={"api_key": "demo"}', '--config={"mode": "x"}']),
            ['--config={"api_key": "***"}', '--config={"mode": "x"}'],
        )
        self.assertEqual(mask.mask_args(['--token={"a": "demo"}']), ["--token=***"])
        token = "ghp_" + "Ab1" * 8  # a token misfiled as the flag name
        self.assertNotIn(token, mask.mask_args([f"--{token}=" + '{"mode": "x"}'])[0])
        self.assertNotIn(token, mask.mask_args([f"--token_{token}=" + '{"a": 1}'])[0])
        self.assertEqual(mask.mask_args(["--token={env:DEMO_TOKEN}"]), ["--token={env:DEMO_TOKEN}"])
        self.assertEqual(mask.mask_args(['{"pwd": "demo"}']), ['{"pwd": "***"}'])
        self.assertEqual(mask.mask_args(['{"env": {"PWD": "/work"}}']), ['{"env": {"PWD": "/work"}}'])
        self.assertTrue(mask.env_value_looks_secret("X", '--headers={"X-Api-Key": "demo"}'))

    def test_a_clean_json_document_is_never_changed_by_text_patterns(self):
        doc = '{"url":"https://demo.example.test:3000","pkg":"@scope/demo-server"}'
        self.assertEqual(mask.mask_args([doc]), [doc])
        self.assertFalse(mask.env_value_looks_secret("APP_CONFIG", doc))

    def test_a_credential_header_value_is_masked_whole_unless_it_is_a_reference(self):
        for text, want in (
            ("Authorization: Token demo_value_1", "Authorization: ***"),
            ("Authorization: ApiKey demo_value_1", "Authorization: ***"),
            ("Cookie: sid=demo_value_1; b=2", "Cookie: ***"),
            ("curl -H 'Cookie: sid=demo' https://h.test", "curl -H 'Cookie: ***' https://h.test"),
            ("Authorization: Token ${DEMO_TOKEN}", "Authorization: Token ${DEMO_TOKEN}"),
            ("Authorization: Bearer ${DEMO_TOKEN}", "Authorization: Bearer ${DEMO_TOKEN}"),
            ("Cookie: ${DEMO_COOKIE}", "Cookie: ${DEMO_COOKIE}"),
            ("`Authorization: Bearer ${NAME}` (header X)", "`Authorization: Bearer ${NAME}` (header X)"),
        ):
            with self.subTest(text=text):
                self.assertEqual(mask.mask_text(text), want)
        self.assertEqual(mask.mask_args(["-H", "Cookie: sid=demo"]), ["-H", "Cookie: ***"])
        for text, want in (
            ('Authorization: "demo-value"', 'Authorization: "***"'),
            ("Authorization: 'Bearer ${DEMO_TOKEN}'", "Authorization: 'Bearer ${DEMO_TOKEN}'"),
            ('Authorization: demo"tail', "Authorization: ***"),
        ):
            with self.subTest(text=text):
                self.assertEqual(mask.mask_text(text), want)
        self.assertTrue(mask.env_value_looks_secret("EXTRA", 'Authorization: "demo-value"'))
        self.assertTrue(mask.env_value_looks_secret("EXTRA", "Cookie: sid=demo"))

    def test_an_underscore_flag_after_a_secret_flag_is_a_flag(self):
        self.assertEqual(
            mask.mask_args(["--no-password", "--api_key", "demo-value"]),
            ["--no-password", "--api_key", "***"],
        )
        self.assertEqual(
            mask.mask_args(["--no-password", "--api.key", "demo-value"]),
            ["--no-password", "--api.key", "***"],
        )
        for flag in ("--auth.key", "--auth_key", "--auth-key"):
            with self.subTest(flag=flag):
                self.assertEqual(mask.mask_args([flag, "demo"]), [flag, "***"])
        self.assertEqual(mask.mask_json({"auth.key": "demo"}), {"auth.key": "***"})
        self.assertTrue(mask.env_value_looks_secret("AUTH_KEY", "demo"))

    def test_a_header_value_with_more_after_a_quote_is_masked_whole(self):
        for text, want in (
            ('Authorization: "Bearer ${DEMO_TOKEN}" demo-tail', "Authorization: ***"),
            ('Cookie: "a" "demo-tail"', "Cookie: ***"),
            ('Cookie: a="1" b="demo-tail"', "Cookie: ***"),
            ('Authorization: Digest realm="x" response="demo-tail"', "Authorization: ***"),
            ("curl -H 'Cookie: a=\"1\" b=demo-tail' https://h.test", "curl -H 'Cookie: ***' https://h.test"),
        ):
            with self.subTest(text=text):
                self.assertEqual(mask.mask_text(text), want)

    def test_a_quoted_value_after_a_scheme_or_a_secret_name_is_judged(self):
        for text, want in (
            ('api_key="demo-tail"', 'api_key="***"'),
            ("token='demo-tail'", "token='***'"),
            ('Bearer "demo-tail"', 'Bearer "***"'),
            ('token="${DEMO_TOKEN}"', 'token="${DEMO_TOKEN}"'),
            ('token="a\\"b" tail', 'token="***" tail'),
            ('token="never closed', 'token="***'),
            ('token="never closed ends in \\', 'token="***'),
            ('Bearer "never closed ends in \\', 'Bearer "***'),
            ('token="${DEMO_TOKEN}"demo-tail', 'token="***'),
            ('Bearer "${DEMO_TOKEN}"demo-tail', 'Bearer "***'),
            ('token="a"demo-tail', 'token="***'),
            ('token=abc"demo-tail', "token=***"),
            ('{"h": "Bearer demo"}', '{"h": "Bearer ***"}'),
            ("curl -H 'X-Api-Key: 'demo-tail", "curl -H 'X-Api-Key: ***"),
            ('curl -H "Authorization: ${DEMO_TOKEN}"demo-tail', 'curl -H "Authorization: ***'),
            ('curl -H "Authorization: Token "demo-tail', 'curl -H "Authorization: ***'),
            ('token=abc"my demo-tail"', "token=***"),
            ('token=abc" demo-tail"', "token=***"),
            ('token=abc"a\\"b demo-tail"', "token=***"),
            ('"token=abc" next', '"token=***" next'),
            ('curl "token=${DEMO_TOKEN}" "https://h.test"', 'curl "token=${DEMO_TOKEN}" "https://h.test"'),
            ('{"a": "Bearer ${DEMO_TOKEN}", "b": "plain"}', '{"a": "Bearer ${DEMO_TOKEN}", "b": "plain"}'),
            ('curl "token=${DEMO_TOKEN}" "password=demo"', 'curl "token=${DEMO_TOKEN}" "password=***"'),
            ("https://h.test?token={env:DEMO_TOKEN}&x=1", "https://h.test?token={env:DEMO_TOKEN}&x=1"),
            ("'--token={env:DEMO_TOKEN}' --mode=1", "'--token={env:DEMO_TOKEN}' --mode=1"),
            ('Bearer abc"my demo-tail"', "Bearer ***"),
            ("X-Auth-Key: demo-tail", "X-Auth-Key: ***"),
            ("-H 'Auth-Key: demo-tail'", "-H 'Auth-Key: ***'"),
            ('token="abc\\\r\nnext line', 'token="***\r\nnext line'),
            ('--token={"a": "demo-tail"}', "--token=***"),
        ):
            with self.subTest(text=text):
                self.assertEqual(mask.mask_text(text), want)

    def test_authorization_and_cookie_names_hold_secrets(self):
        self.assertTrue(mask.env_value_looks_secret("COOKIE", "session123"))
        self.assertTrue(mask.env_value_looks_secret("AUTHORIZATION", "demo"))
        self.assertFalse(mask.env_value_looks_secret("COOKIE", "${COOKIE}"))

    def test_a_json_env_value_holding_a_secret_looks_secret(self):
        self.assertTrue(mask.env_value_looks_secret("APP_CONFIG", '{"api_key":"demo-literal"}'))
        self.assertFalse(mask.env_value_looks_secret("APP_CONFIG", '{"mode":"test"}'))

    def test_a_reference_with_a_fallback_is_not_a_plain_reference(self):
        self.assertTrue(mask.env_value_looks_secret("DB_PASSWORD", "${DB_PASSWORD:-demo-pass}"))
        self.assertTrue(mask.env_value_looks_secret("API_TOKEN", "{env:API_TOKEN:demo}"))
        self.assertTrue(mask.env_value_looks_secret("LOG_LEVEL", "${LOG_LEVEL:-***}"))
        self.assertTrue(mask.env_value_looks_secret("API_KEY", "%not a var%"))
        self.assertTrue(mask.env_value_looks_secret("ghp_" + "d4" * 12, "demo"))
        long_name = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"
        self.assertTrue(mask.env_value_looks_secret("API_KEY", "${" + long_name + "}"))
        self.assertEqual(mask.mask_text("Bearer ${" + long_name + "}"), "Bearer ***")
        for text, want in (
            ("--key=${" + long_name + "}", "--key=${***}"),
            ("{env:" + long_name + "}", "{env:***}"),
            ("$" + long_name, "$***"),
            ("%" + long_name + "%", "%***%"),
            ("${PATH}/bin and $HOME/x", "${PATH}/bin and $HOME/x"),
            ("${" + long_name + ":-fb}", "${***:-***}"),
            ("${" + long_name + "-fb}", "${***-***}"),
            ("{env:" + long_name + ":fb}", "{env:***:***}"),
            ("prefix$" + long_name, "prefix$***"),
            ("${VAR:-'demo}tail-value'}", "${VAR:-***}"),
            ("US$100 and $HOME", "US$100 and $HOME"),
        ):
            with self.subTest(text=text):
                self.assertEqual(mask.mask_text(text), want)
        self.assertTrue(mask.looks_like_credential("  " + long_name + "  "))
        self.assertEqual(mask.mask_name(" " + long_name + " "), "***")
        self.assertEqual(mask.mask_name("API_KEY"), "API_KEY")
        self.assertEqual(mask.mask_text('${V:-"a\\"b}demo-tail"}'), "${V:-***}")
        self.assertEqual(
            mask.mask_fallbacks("line1 ${V:-don't}\nline2 kept"), "line1 ${V:-***}\nline2 kept"
        )
        self.assertEqual(mask.mask_fallbacks("${V:-'a\n}demo-marker'} x"), "${V:-***} x")
        # a stray apostrophe never swallows the lines after it
        text = "a ${V:-don't} b\nkey 'c' stays\nlast"
        self.assertEqual(mask.mask_fallbacks(text), "a ${V:-***} b\nkey 'c' stays\nlast")
        self.assertFalse(mask.looks_like_credential("  API_KEY  "))
        for ref in ("${HOME}", "{env:TOKEN}", "%PATH%", "$USER"):
            with self.subTest(ref=ref):
                self.assertTrue(mask.is_env_ref(ref))
        self.assertFalse(mask.ENV_REF.match("${TOKEN}" + chr(10)))
        for ref in ("${DB_PASSWORD}", "{env:DB_PASSWORD}", "$DB_PASSWORD", "%DB_PASSWORD%"):
            with self.subTest(ref=ref):
                self.assertFalse(mask.env_value_looks_secret("DB_PASSWORD", ref))


if __name__ == "__main__":
    unittest.main()
