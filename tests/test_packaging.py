"""Offline packaging metadata and pyproject.toml verification tests."""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents import __version__, cli

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
README_PATH = REPO_ROOT / "README.md"


class PackagingMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(PYPROJECT_PATH.is_file(), f"Missing pyproject.toml at {PYPROJECT_PATH}")
        with open(PYPROJECT_PATH, "rb") as f:
            self.pyproject = tomllib.load(f)
        self.project = self.pyproject.get("project", {})

    def test_no_runtime_dependencies(self) -> None:
        deps = self.project.get("dependencies")
        self.assertEqual(deps, [], "Core must have zero runtime dependencies (stdlib only)")

    def test_console_scripts_registered(self) -> None:
        scripts = self.project.get("scripts", {})
        self.assertEqual(
            scripts.get("all-agents"),
            "one_setup_all_agents.cli:main",
            "all-agents script entry point missing or invalid",
        )
        self.assertEqual(
            scripts.get("one-setup-all-agents"),
            "one_setup_all_agents.cli:main",
            "one-setup-all-agents script alias entry point missing or invalid",
        )

    def test_requires_python(self) -> None:
        req = self.project.get("requires-python")
        self.assertEqual(req, ">=3.11")

    def test_dynamic_version_path(self) -> None:
        dynamic = self.project.get("dynamic", [])
        self.assertIn("version", dynamic)
        hatch_version_path = (
            self.pyproject.get("tool", {}).get("hatch", {}).get("version", {}).get("path")
        )
        self.assertEqual(hatch_version_path, "src/one_setup_all_agents/__init__.py")
        self.assertTrue(isinstance(__version__, str) and len(__version__) > 0)

    def test_cli_main_is_callable(self) -> None:
        self.assertTrue(callable(cli.main))

    def test_classifiers_cover_python_versions_and_target_os(self) -> None:
        classifiers = set(self.project.get("classifiers", []))
        expected = {
            "Programming Language :: Python :: 3.11",
            "Programming Language :: Python :: 3.12",
            "Programming Language :: Python :: 3.13",
            "Operating System :: MacOS",
            "Operating System :: Microsoft :: Windows",
            "Operating System :: POSIX :: Linux",
        }
        for item in expected:
            self.assertIn(item, classifiers, f"Classifier '{item}' missing from pyproject.toml")

    def test_keywords_contain_shared_agent_config(self) -> None:
        keywords = self.project.get("keywords", [])
        self.assertIn("shared agent config", keywords)

    def test_readme_mentions_uvx_and_pipx_install(self) -> None:
        self.assertTrue(README_PATH.is_file(), f"Missing README.md at {README_PATH}")
        readme_text = README_PATH.read_text(encoding="utf-8")
        self.assertIn("uvx one-setup-all-agents", readme_text)
        self.assertIn("pipx install one-setup-all-agents", readme_text)

    @unittest.skipUnless(os.environ.get("PACKAGING_TESTS") == "1", "PACKAGING_TESTS not set")
    def test_build_wheel_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res = subprocess.run(
                ["uv", "build", "--out-dir", tmp],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            whls = list(Path(tmp).glob("*.whl"))
            self.assertEqual(len(whls), 1)


if __name__ == "__main__":
    unittest.main()
