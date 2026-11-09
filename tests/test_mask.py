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


if __name__ == "__main__":
    unittest.main()
