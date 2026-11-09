"""The CLI entry point runs and lists exactly the four commands."""

import contextlib
import io
import os
import subprocess
import sys
import unittest

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents import __version__
from one_setup_all_agents.cli import build_parser, main

COMMANDS = ("init", "sync", "status", "undo")


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:  # argparse exits on --help and --version
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class HelpTests(unittest.TestCase):
    def test_help_lists_the_four_commands(self):
        code, out, _ = _run_main(["--help"])
        self.assertEqual(code, 0)
        for command in COMMANDS:
            self.assertIn(command, out)

    def test_parser_has_exactly_the_four_commands(self):
        parser = build_parser()
        subparsers = [a for a in parser._actions if a.dest == "command"][0]
        self.assertEqual(tuple(subparsers.choices), COMMANDS)

    def test_help_states_the_positioning_phrase(self):
        _, out, _ = _run_main(["--help"])
        self.assertIn("shared agent config", " ".join(out.split()))

    def test_no_arguments_prints_help(self):
        code, out, _ = _run_main([])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_version(self):
        code, out, _ = _run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    def test_every_command_has_help(self):
        for command in COMMANDS:
            code, out, _ = _run_main([command, "--help"])
            self.assertEqual(code, 0, command)
            self.assertIn("usage:", out)

    def test_advanced_options_group_appears_in_help_for_all_commands(self):
        for command in COMMANDS:
            code, out, _ = _run_main([command, "--help"])
            self.assertEqual(code, 0, command)
            self.assertIn(
                "advanced options:", out.lower(), f"missing advanced options group in {command}"
            )

    def test_epilog_status(self):
        _, out, _ = _run_main(["--help"])
        self.assertIn("Status: pre-alpha.", out)
        self.assertNotIn("The commands above are not usable yet.", out)


class HiddenCommandTests(unittest.TestCase):
    def test_internal_commands_are_not_listed_in_help(self):
        _, out, _ = _run_main(["--help"])
        self.assertNotIn("_run", out)
        self.assertNotIn("_legacy", out)

    def test_internal_commands_dispatch(self):
        code, _, err = _run_main(["_run"])
        self.assertEqual(code, 2)  # usage error from the runner, not "not implemented"
        self.assertIn("usage", err)


class ModuleEntryPointTests(unittest.TestCase):
    def test_python_dash_m_help(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_bootstrap.SRC), *filter(None, [env.get("PYTHONPATH")])]
        )
        result = subprocess.run(
            [sys.executable, "-m", "one_setup_all_agents", "--help"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in COMMANDS:
            self.assertIn(command, result.stdout)


if __name__ == "__main__":
    unittest.main()
