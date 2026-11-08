"""Tests for CommandContext, UI helpers, and injectable CLI main."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.engine_support import make_env

import _bootstrap  # noqa: F401
from one_setup_all_agents import commands
from one_setup_all_agents.cli import build_parser, main
from one_setup_all_agents.context import CommandContext, default_context
from one_setup_all_agents.ui import choose, confirm, say, warn

# Planted secret shaped like a standard token recognized by mask_text at runtime
PLANTED_SECRET = "sk-" + "a1b2c3d4" * 6


class ContextAndUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = make_env(self.home)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, is_interactive=False, read_line=lambda prompt: ""):
        return CommandContext(
            env=self.env,
            read_line=read_line,
            is_interactive=is_interactive,
            out=self.out,
            err=self.err,
        )

    def test_default_context_honors_config_dir(self):
        parser = build_parser()
        args = parser.parse_args(["--config-dir", str(self.home / "custom_config")])
        ctx = default_context(args)
        expected = (self.home / "custom_config").resolve()
        self.assertEqual(ctx.env.config_dir, expected)

    def test_confirm_non_interactive_returns_false_without_prompting(self):
        called = False

        def fake_read(prompt):
            nonlocal called
            called = True
            return "yes"

        ctx = self._ctx(is_interactive=False, read_line=fake_read)
        res = confirm(ctx, "Proceed?")
        self.assertFalse(res)
        self.assertFalse(called)
        self.assertIn("not asked: not interactive; pass --yes", self.out.getvalue())

    def test_confirm_interactive_handles_yes_and_no(self):
        for answer, expected in (
            ("y", True),
            ("Y", True),
            ("yes", True),
            ("YES", True),
            ("n", False),
            ("no", False),
            ("", False),
            ("maybe", False),
        ):
            ctx = self._ctx(is_interactive=True, read_line=lambda prompt, a=answer: a)
            res = confirm(ctx, "Question?")
            self.assertEqual(res, expected, f"Failed for answer {answer!r}")

    def test_choose_handles_interactive_and_non_interactive(self):
        ctx_non_int = self._ctx(is_interactive=False)
        self.assertIsNone(choose(ctx_non_int, "Pick:", ["a", "b"]))

        ctx_empty = self._ctx(is_interactive=True)
        self.assertIsNone(choose(ctx_empty, "Pick:", []))

        ctx_valid = self._ctx(is_interactive=True, read_line=lambda _: "2")
        self.assertEqual(choose(ctx_valid, "Pick:", ["a", "b", "c"]), 2)

        ctx_invalid = self._ctx(is_interactive=True, read_line=lambda _: "99")
        self.assertIsNone(choose(ctx_invalid, "Pick:", ["a", "b"]))

        ctx_not_num = self._ctx(is_interactive=True, read_line=lambda _: "abc")
        self.assertIsNone(choose(ctx_not_num, "Pick:", ["a", "b"]))

    def test_say_and_warn_mask_secrets_and_home_path(self):
        ctx = self._ctx()
        raw_msg = f"Path is {self.env.home / 'sub'} and secret is {PLANTED_SECRET}"
        say(ctx, raw_msg)
        warn(ctx, raw_msg)

        out_text = self.out.getvalue()
        err_text = self.err.getvalue()

        self.assertNotIn(PLANTED_SECRET, out_text)
        self.assertNotIn(PLANTED_SECRET, err_text)
        self.assertNotIn(str(self.env.home), out_text)
        self.assertNotIn(str(self.env.home), err_text)
        self.assertIn("~", out_text)
        self.assertIn("~", err_text)

    def test_no_output_to_real_stdout_when_injected(self):
        ctx = self._ctx()
        with patch("sys.stdout", new_callable=io.StringIO) as fake_stdout:
            say(ctx, "Hello injected context")
            self.assertEqual(fake_stdout.getvalue(), "")
        self.assertIn("Hello injected context", self.out.getvalue())

    def test_main_with_injected_context_reaches_init_run(self):
        ctx = self._ctx()
        called_args = []

        def fake_run(args, c):
            called_args.append((args, c))
            return 0

        with patch.object(commands.init, "run", side_effect=fake_run):
            rc = main(["init", "--yes"], context=ctx)
            self.assertEqual(rc, 0)
            self.assertEqual(len(called_args), 1)
            self.assertEqual(called_args[0][0].command, "init")
            self.assertTrue(called_args[0][0].yes)
            self.assertIs(called_args[0][1], ctx)


if __name__ == "__main__":
    unittest.main()
