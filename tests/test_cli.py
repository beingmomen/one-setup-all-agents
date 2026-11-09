"""The CLI entry point runs and lists exactly the four commands."""

import contextlib
import io
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
import one_setup_all_agents.cli as cli_mod
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


class CliExitCodesDocTests(unittest.TestCase):
    def test_cli_docstring_exit_codes_match_adr_0018_section_3(self):
        doc = cli_mod.__doc__ or ""
        adr_path = (
            Path(__file__).resolve().parent.parent
            / "docs"
            / "decisions"
            / "0018-commands-sync-status-undo.md"
        )
        adr_text = adr_path.read_text(encoding="utf-8")

        match = re.search(r"### 3\. Exit codes(.*?)(?:### 4|\Z)", adr_text, re.DOTALL)
        self.assertIsNotNone(match, "ADR 0018 section 3 not found")
        adr_section_3 = match.group(1)

        commands_in_doc = re.findall(
            r"- (General \(argparse\)|all-agents \w+):\s*\n((?:\s+- \d+: .*\n)+)", doc
        )
        self.assertEqual(len(commands_in_doc), 5)

        for cmd, codes_block in commands_in_doc:
            self.assertIn(cmd, adr_section_3, f"Command {cmd} missing in ADR 0018 §3")
            codes = re.findall(r"- (\d+):", codes_block)
            self.assertTrue(len(codes) > 0)
            for code in codes:
                self.assertIn(
                    f"`{code}`:", adr_section_3, f"Code {code} for {cmd} missing in ADR 0018 §3"
                )


if __name__ == "__main__":
    unittest.main()
