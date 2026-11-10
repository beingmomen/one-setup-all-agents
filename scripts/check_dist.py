#!/usr/bin/env python3
# Copyright (c) 2026 one-setup-all-agents contributors.
# SPDX-License-Identifier: MIT

"""Standalone stdlib validator for built release distribution artifacts (.whl and .tar.gz)."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path


def check_dist(dist_dir: Path) -> list[str]:
    errors: list[str] = []
    if not dist_dir.is_dir():
        return [f"dist directory not found: {dist_dir}"]

    wheels = list(dist_dir.glob("*.whl"))
    sdists = list(dist_dir.glob("*.tar.gz"))

    if not wheels:
        errors.append("no .whl artifact found in dist")
    if not sdists:
        errors.append("no .tar.gz artifact found in dist")

    for whl in wheels:
        try:
            with zipfile.ZipFile(whl, "r") as zf:
                names = zf.namelist()

                # Forbidden paths in wheel
                has_test_error = False
                for name in names:
                    if (name.startswith("tests/") or name.startswith("tests\\")) and not has_test_error:
                        errors.append(f"{whl.name}: contains test files: {name}")
                        has_test_error = True
                    if ".personal-patterns" in name:
                        errors.append(f"{whl.name}: contains forbidden personal patterns file")
                    if name.startswith(".github/") or name.startswith(".github\\"):
                        errors.append(f"{whl.name}: contains .github workflow files: {name}")


                # Must contain package modules
                has_package = any(n.startswith("one_setup_all_agents/") for n in names)
                if not has_package:
                    errors.append(f"{whl.name}: missing one_setup_all_agents package directory")

                # Check entry points
                ep_files = [n for n in names if n.endswith("entry_points.txt")]
                if not ep_files:
                    errors.append(f"{whl.name}: missing entry_points.txt")
                else:
                    ep_content = zf.read(ep_files[0]).decode("utf-8")
                    if "all-agents =" not in ep_content:
                        errors.append(f"{whl.name}: missing all-agents console script in entry_points.txt")
                    if "one-setup-all-agents =" not in ep_content:
                        errors.append(f"{whl.name}: missing one-setup-all-agents console script in entry_points.txt")

                # Check metadata
                meta_files = [n for n in names if n.endswith("METADATA")]
                if not meta_files:
                    errors.append(f"{whl.name}: missing METADATA file")
                else:
                    meta_content = zf.read(meta_files[0]).decode("utf-8")
                    if "Requires-Python: >=3.11" not in meta_content:
                        errors.append(f"{whl.name}: METADATA missing Requires-Python: >=3.11")
                    if "Requires-Dist:" in meta_content:
                        errors.append(f"{whl.name}: METADATA must not contain runtime dependencies (Requires-Dist)")
        except Exception as exc:
            errors.append(f"{whl.name}: failed to read zip archive: {exc}")

    for sdist in sdists:
        try:
            with tarfile.open(sdist, "r:gz") as tf:
                names = tf.getnames()
                for name in names:
                    if ".personal-patterns" in name:
                        errors.append(f"{sdist.name}: contains forbidden personal patterns file")
                    if "__pycache__" in name or ".pyc" in name:
                        errors.append(f"{sdist.name}: contains compiled cache files: {name}")
        except Exception as exc:
            errors.append(f"{sdist.name}: failed to read tar archive: {exc}")

    return errors


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: check_dist.py <dist_dir>", file=sys.stderr)
        return 1
    dist_dir = Path(sys.argv[1]).resolve()
    errors = check_dist(dist_dir)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print("check_dist: distribution artifacts verified cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
