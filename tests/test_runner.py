"""The runner (`all-agents _run`): derive variables, expand placeholders, start the command, never print a value."""

import subprocess
import sys
import unittest

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents._core import runner
from support import cli_env

PY = sys.executable


def run_runner(*argv, env_extra=None):
    cmd = [PY, "-m", "one_setup_all_agents", "_run", *argv]
    env = cli_env(env_extra)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)


class ParseDeriveTests(unittest.TestCase):
    def test_valid_and_invalid_specs(self):
        self.assertEqual(runner.parse_derive(["A_B=x{{C}}"]), [("A_B", "x{{C}}")])
        for bad in ("lower=x", "NOEQUALS", "A=", "A=x{{$B}}"):
            with self.assertRaises(runner.RunnerError, msg=bad):
                runner.parse_derive([bad])
        with self.assertRaises(runner.RunnerError):
            runner.parse_derive(["A=1", "A=2"])


class ExpandTests(unittest.TestCase):
    def test_both_reference_styles_and_the_passthrough(self):
        env = {"TOKEN": "tok-value"}
        self.assertEqual(
            runner.expand_args(["a-${TOKEN}", "b-{{TOKEN}}", "c-{{$TOKEN}}"], env),
            ["a-tok-value", "b-tok-value", "c-${TOKEN}"],
        )

    def test_a_missing_variable_is_a_refusal_naming_only_the_variable(self):
        with self.assertRaises(runner.RunnerError) as ctx:
            runner.expand_args(["{{DEFINITELY_NOT_SET_XYZ}}"], {})
        self.assertIn("DEFINITELY_NOT_SET_XYZ", str(ctx.exception))

    def test_derive_builds_a_value_with_a_space_without_a_space_in_the_argument(self):
        env = {"TOKEN": "tok-value"}
        names = runner.apply_derive(env, runner.parse_derive(["AUTH=Bearer{sp}{{TOKEN}}"]))
        self.assertEqual(names, ["AUTH"])
        self.assertEqual(env["AUTH"], "Bearer tok-value")


class EndToEndTests(unittest.TestCase):
    def test_the_child_sees_the_derived_value_and_the_argument_stays_unexpanded(self):
        code = "import os,sys; print(os.environ['MCP_HDR_1']); print(sys.argv[1])"
        r = run_runner(
            "--derive",
            "MCP_HDR_1=Bearer{sp}{{FAKE_RUN_TOKEN}}",
            "--",
            PY,
            "-c",
            code,
            "Authorization:{{$MCP_HDR_1}}",
            env_extra={"FAKE_RUN_TOKEN": "FAKE-RUN-SECRET-123"},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "Bearer FAKE-RUN-SECRET-123")
        self.assertEqual(
            lines[1], "Authorization:${MCP_HDR_1}"
        )  # the secret never travelled as an argument
        self.assertNotIn("FAKE-RUN-SECRET-123", r.stderr)

    def test_the_child_exit_code_is_passed_through(self):
        r = run_runner("--", PY, "-c", "import sys; sys.exit(7)")
        self.assertEqual(r.returncode, 7)

    def test_a_missing_variable_refuses_with_exit_2_and_no_value_in_the_message(self):
        r = run_runner("--", PY, "-c", "print(1)", "{{FAKE_RUN_MISSING}}")
        self.assertEqual(r.returncode, 2)
        self.assertIn("FAKE_RUN_MISSING", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_command_not_found_is_127(self):
        r = run_runner("--", "definitely-no-such-command-xyz")
        self.assertEqual(r.returncode, 127)

    def test_usage_errors_are_exit_2(self):
        self.assertEqual(run_runner().returncode, 2)
        self.assertEqual(run_runner("--").returncode, 2)
        self.assertEqual(run_runner("--bogus", "--", PY).returncode, 2)

    def test_stdin_and_stdout_pass_straight_through(self):
        code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
        cmd = [PY, "-m", "one_setup_all_agents", "_run", "--", PY, "-c", code]
        r = subprocess.run(
            cmd, input="stdio mcp", capture_output=True, text=True, env=cli_env(), timeout=60
        )
        self.assertEqual(r.stdout, "STDIO MCP")


if __name__ == "__main__":
    unittest.main()
