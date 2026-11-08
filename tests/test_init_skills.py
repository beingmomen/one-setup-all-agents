"""Tests for skills import in `all-agents init` (Phase 4 M04-T2.3)."""

from __future__ import annotations

import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.engine_support import FakeRunner, can_make_dir_link, make_env

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from one_setup_all_agents.adapters import all_adapters
from one_setup_all_agents.adapters.base import ToolAdapter
from one_setup_all_agents.cli import build_parser
from one_setup_all_agents.commands import init
from one_setup_all_agents.config_source import desired_all, load_source
from one_setup_all_agents.context import CommandContext
from one_setup_all_agents.engine import fsops, journal
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.plan import build_plan
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.importer import (
    propose_skills,
    skill_copy_ops,
)
from one_setup_all_agents.model import (
    Detection,
    Kind,
    Link,
    Paths,
    Plan,
    Scope,
    SkillRef,
)
from one_setup_all_agents.reader import ToolReading


def can_symlink(tmp: Path) -> bool:
    probe = tmp / "probe-link"
    try:
        os.symlink(tmp, probe)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


class InitSkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = make_env(self.root)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, is_interactive: bool = False, read_line=lambda prompt: "") -> CommandContext:
        return CommandContext(
            env=self.env,
            read_line=read_line,
            is_interactive=is_interactive,
            out=self.out,
            err=self.err,
        )

    def _dummy_adapter(
        self,
        tool_id: str,
        tool_label: str,
        skills_dir: Path | None = None,
        manifest_skills: list[SkillRef] | None = None,
    ) -> ToolAdapter:
        is_manifest = manifest_skills is not None
        return type(
            "DummyAdapter",
            (ToolAdapter,),
            {
                "id": tool_id,
                "label": tool_label,
                "capabilities": frozenset({Kind.SKILLS}),
                "detect": lambda self, env: Detection(
                    installed=True, tool=tool_id, label=tool_label
                ),
                "paths": lambda self, env, scope: Paths(skills_dir=skills_dir),
                "read_instructions": lambda self, env, scope: None,
                "read_mcp": lambda self, env, scope: {},
                "read_skills": lambda self, env, scope: list(manifest_skills or []),
                "wanted_links": lambda self, env, scope, desired: (
                    []
                    if is_manifest
                    else [
                        type(
                            "WantedLink",
                            (),
                            {"link": skills_dir / s.name, "target": s.path},
                        )()
                        for s in desired
                    ]
                ),
                "plan_instructions": lambda self, env, scope, desired, owned: [],
                "plan_mcp": lambda self, env, scope, desired, owned: [],
                "plan_skills": lambda self, env, scope, desired, owned: [],
                "import_notes": lambda self, env, scope=Scope.GLOBAL: [],
            },
        )()

    def test_real_folder_in_one_tool_copied_not_adopted_conflict_message(self):
        skills_dir = self.root / "tool_skills"
        skills_dir.mkdir(parents=True)
        real_skill = skills_dir / "my-skill"
        real_skill.mkdir()
        (real_skill / "SKILL.md").write_text("# My Skill\n", encoding="utf-8")
        (real_skill / "helper.py").write_text("print('ok')\n", encoding="utf-8")

        adapter = self._dummy_adapter("tool-a", "Tool A", skills_dir=skills_dir)
        reading = ToolReading(
            "tool-a",
            "Tool A",
            None,
            {},
            (SkillRef("my-skill", real_skill, is_link=False),),
            (),
        )

        prop = propose_skills([reading], [adapter], self.env)
        self.assertEqual(len(prop.copies), 1)
        self.assertEqual(prop.copies[0].name, "my-skill")
        # Assert not adopted
        self.assertEqual(prop.adopt, [])
        # Assert conflict message
        expected_conflict = (
            "Tool A: skill my-skill exists as a real folder; rename or remove it, then run sync"
        )
        self.assertIn(expected_conflict, prop.conflicts)
        self.assertIn(expected_conflict, prop.messages)

    def test_junction_chain_copied_once_and_adopted(self):
        if not can_make_dir_link(self.root):
            self.skipTest("directory links not supported in this environment")

        shared_real = self.root / "shared_skills" / "shared-skill"
        shared_real.mkdir(parents=True)
        (shared_real / "SKILL.md").write_text("# Shared Skill\n", encoding="utf-8")

        intermediate = self.root / "intermediate_link"
        fsops.create_dir_link(intermediate, shared_real)

        dir_a = self.root / "tool_a_skills"
        dir_a.mkdir(parents=True)
        link_a = dir_a / "shared-skill"
        fsops.create_dir_link(link_a, intermediate)

        dir_b = self.root / "tool_b_skills"
        dir_b.mkdir(parents=True)
        link_b = dir_b / "shared-skill"
        fsops.create_dir_link(link_b, shared_real)

        a1 = self._dummy_adapter("tool-a", "Tool A", skills_dir=dir_a)
        a2 = self._dummy_adapter("tool-b", "Tool B", skills_dir=dir_b)
        r1 = ToolReading(
            "tool-a", "Tool A", None, {}, (SkillRef("shared-skill", link_a, is_link=True),), ()
        )
        r2 = ToolReading(
            "tool-b", "Tool B", None, {}, (SkillRef("shared-skill", link_b, is_link=True),), ()
        )

        prop = propose_skills([r1, r2], [a1, a2], self.env)
        # Copied once
        self.assertEqual(len(prop.copies), 1)
        self.assertEqual(prop.copies[0].name, "shared-skill")
        # Adopted for both tools
        adopted = {(r.tool, r.kind, r.key) for r in prop.adopt}
        self.assertIn(("tool-a", Kind.SKILLS, "shared-skill"), adopted)
        self.assertIn(("tool-b", Kind.SKILLS, "shared-skill"), adopted)
        self.assertEqual(prop.conflicts, [])

    def test_same_name_different_content_without_from(self):
        dir_a = self.root / "tool_a"
        dir_a.mkdir(parents=True)
        skill_a = dir_a / "diff-skill"
        skill_a.mkdir()
        (skill_a / "SKILL.md").write_text("# Skill Version A\n", encoding="utf-8")

        dir_b = self.root / "tool_b"
        dir_b.mkdir(parents=True)
        skill_b = dir_b / "diff-skill"
        skill_b.mkdir()
        (skill_b / "SKILL.md").write_text("# Skill Version B\n", encoding="utf-8")

        a1 = self._dummy_adapter("tool-a", "Tool A", skills_dir=dir_a)
        a2 = self._dummy_adapter("tool-b", "Tool B", skills_dir=dir_b)
        r1 = ToolReading("tool-a", "Tool A", None, {}, (SkillRef("diff-skill", skill_a),), ())
        r2 = ToolReading("tool-b", "Tool B", None, {}, (SkillRef("diff-skill", skill_b),), ())

        prop = propose_skills([r1, r2], [a1, a2], self.env, from_tool=None)
        self.assertEqual(len(prop.copies), 0)
        self.assertEqual(len(prop.adopt), 0)
        self.assertTrue(any("skill diff-skill differs between tools" in c for c in prop.conflicts))

    def test_same_name_different_content_with_from(self):
        if not can_make_dir_link(self.root):
            self.skipTest("directory links not supported in this environment")

        source_a = self.root / "real_a"
        source_a.mkdir(parents=True)
        (source_a / "SKILL.md").write_text("# Skill Version A\n", encoding="utf-8")

        source_b = self.root / "real_b"
        source_b.mkdir(parents=True)
        (source_b / "SKILL.md").write_text("# Skill Version B\n", encoding="utf-8")

        dir_a = self.root / "tool_a"
        dir_a.mkdir(parents=True)
        link_a = dir_a / "diff-skill"
        fsops.create_dir_link(link_a, source_a)

        dir_b = self.root / "tool_b"
        dir_b.mkdir(parents=True)
        link_b = dir_b / "diff-skill"
        fsops.create_dir_link(link_b, source_b)

        a1 = self._dummy_adapter("tool-a", "Tool A", skills_dir=dir_a)
        a2 = self._dummy_adapter("tool-b", "Tool B", skills_dir=dir_b)
        r1 = ToolReading(
            "tool-a", "Tool A", None, {}, (SkillRef("diff-skill", link_a, is_link=True),), ()
        )
        r2 = ToolReading(
            "tool-b", "Tool B", None, {}, (SkillRef("diff-skill", link_b, is_link=True),), ()
        )

        prop = propose_skills([r1, r2], [a1, a2], self.env, from_tool="tool-a")
        self.assertEqual(len(prop.copies), 1)
        self.assertEqual(prop.copies[0].name, "diff-skill")
        content = dict((f[0].as_posix(), f[1]) for f in prop.copies[0].files)
        self.assertIn(b"Skill Version A", content["SKILL.md"])

        # Tool A adopted, Tool B not adopted
        adopted = [ref.tool for ref in prop.adopt if ref.key == "diff-skill"]
        self.assertEqual(adopted, ["tool-a"])

    def test_missing_skill_md_skipped(self):
        dir_a = self.root / "tool_a"
        dir_a.mkdir(parents=True)
        skill_a = dir_a / "no-skill-md"
        skill_a.mkdir()
        (skill_a / "README.md").write_text("Not a skill\n", encoding="utf-8")

        a1 = self._dummy_adapter("tool-a", "Tool A", skills_dir=dir_a)
        r1 = ToolReading("tool-a", "Tool A", None, {}, (SkillRef("no-skill-md", skill_a),), ())

        prop = propose_skills([r1], [a1], self.env)
        self.assertEqual(len(prop.copies), 0)
        self.assertEqual(len(prop.adopt), 0)
        self.assertIn("skill no-skill-md has no SKILL.md; skipped", prop.messages)

    def test_existing_source_folder_never_overwritten(self):
        existing = self.env.config_dir / "skills" / "prior-skill"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("original", encoding="utf-8")

        dir_a = self.root / "tool_a"
        dir_a.mkdir(parents=True)
        skill_a = dir_a / "prior-skill"
        skill_a.mkdir()
        (skill_a / "SKILL.md").write_text("from tool", encoding="utf-8")

        a1 = self._dummy_adapter("tool-a", "Tool A", skills_dir=dir_a)
        r1 = ToolReading("tool-a", "Tool A", None, {}, (SkillRef("prior-skill", skill_a),), ())

        prop = propose_skills([r1], [a1], self.env)
        self.assertEqual(len(prop.copies), 0)
        self.assertEqual(len(prop.adopt), 0)
        self.assertIn("skill prior-skill already exists in shared source; skipped", prop.messages)
        self.assertEqual(existing.joinpath("SKILL.md").read_text(encoding="utf-8"), "original")

    @unittest.skipUnless(sys.platform != "win32", "executable bits are a POSIX concept")
    def test_executable_bit_preserved_on_posix(self):
        skill_dir = self.root / "posix_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Posix Skill\n", encoding="utf-8")
        script = skill_dir / "run.sh"
        script.write_bytes(b"#!/bin/sh\necho hi\n")
        os.chmod(script, 0o755)

        adapter = self._dummy_adapter("tool-a", "Tool A", skills_dir=skill_dir.parent)
        reading = ToolReading(
            "tool-a", "Tool A", None, {}, (SkillRef("posix-skill", skill_dir),), ()
        )

        prop = propose_skills([reading], [adapter], self.env)
        self.assertEqual(len(prop.copies), 1)
        files = {f[0].as_posix(): f for f in prop.copies[0].files}
        self.assertIsNotNone(files["run.sh"][2])
        self.assertEqual(files["run.sh"][2], 0o755)

        ops = skill_copy_ops(self.env.config_dir, prop)
        apply_plan(self.env, Plan(tuple(ops)), label="test_mode", run_cli=FakeRunner())
        target_script = self.env.config_dir / "skills" / "posix-skill" / "run.sh"
        self.assertEqual(stat.S_IMODE(target_script.stat().st_mode), 0o755)

    def test_excluded_names_ignored(self):
        skills_dir = self.root / "tool_skills"
        skills_dir.mkdir(parents=True)
        for name in [".trash", "synced", ".system", ".hidden"]:
            folder = skills_dir / name
            folder.mkdir()
            (folder / "SKILL.md").write_text("text", encoding="utf-8")

        adapter = self._dummy_adapter("tool-a", "Tool A", skills_dir=skills_dir)
        reading = ToolReading(
            "tool-a",
            "Tool A",
            None,
            {},
            tuple(SkillRef(n, skills_dir / n) for n in [".trash", "synced", ".system", ".hidden"]),
            (),
        )

        prop = propose_skills([reading], [adapter], self.env)
        self.assertEqual(len(prop.copies), 0)
        self.assertEqual(len(prop.adopt), 0)

    def test_skill_with_internal_symlink_refused(self):
        if not can_symlink(self.root):
            self.skipTest("symlinks not supported in this environment")

        skill_dir = self.root / "bad_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Bad Skill\n", encoding="utf-8")
        target_file = skill_dir / "data.txt"
        target_file.write_text("data", encoding="utf-8")
        os.symlink(target_file, skill_dir / "link_to_data")

        adapter = self._dummy_adapter("tool-a", "Tool A", skills_dir=skill_dir.parent)
        reading = ToolReading("tool-a", "Tool A", None, {}, (SkillRef("bad-skill", skill_dir),), ())

        prop = propose_skills([reading], [adapter], self.env)
        self.assertEqual(len(prop.copies), 0)
        self.assertEqual(len(prop.adopt), 0)
        self.assertIn("skill bad-skill contains an internal link; skipped", prop.messages)

    def test_idempotence_build_plan_relink_and_empty_second_plan(self):
        if not can_make_dir_link(self.root):
            self.skipTest("directory links not supported in this environment")

        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        self.assertIsNotNone(paths.skills_dir)
        paths.skills_dir.mkdir(parents=True, exist_ok=True)

        # Create external real skill folder
        external = self.root / "external_skills" / "demo-skill"
        external.mkdir(parents=True)
        (external / "SKILL.md").write_text("# Demo Skill\n", encoding="utf-8")

        # Create link in tool's skills_dir
        tool_link = paths.skills_dir / "demo-skill"
        fsops.create_dir_link(tool_link, external)

        detections = [
            Detection(
                installed=True, tool=adapter.id, label=adapter.label, binary=Path("/bin/adapter")
            )
        ]
        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)

        # Check source skill created
        shared_skill = self.env.config_dir / "skills" / "demo-skill"
        self.assertTrue(shared_skill.is_dir())
        self.assertTrue((shared_skill / "SKILL.md").is_file())

        # Check owned in ledger
        owned = journal.owned_items(self.env.state_dir)
        self.assertIn((adapter.id, "skills", "demo-skill"), owned)

        # Build plan for sync: should produce a relink Link op, not a conflict
        source = load_source(self.env.config_dir, [adapter.id])
        desired = desired_all(source, [adapter.id])
        plan = build_plan(self.env, [adapter], desired)
        self.assertEqual(plan.conflicts, [])
        skill_ops = [
            op for op in plan.executable if isinstance(op, Link) and "demo-skill" in str(op.link)
        ]
        self.assertEqual(len(skill_ops), 1)
        self.assertIn("relink", skill_ops[0].description)

        # Apply the relink op
        sync_op_id = apply_plan(self.env, plan, label="sync", run_cli=FakeRunner())
        self.assertIsNotNone(sync_op_id)

        # Second plan must be empty for skills
        plan2 = build_plan(self.env, [adapter], desired)
        skill_ops2 = [
            op for op in plan2.executable if isinstance(op, Link) and "demo-skill" in str(op.link)
        ]
        self.assertEqual(skill_ops2, [], "second plan must be empty for relinked skill")

    def test_undo_revert_restores_previous_link_target(self):
        if not can_make_dir_link(self.root):
            self.skipTest("directory links not supported in this environment")

        adapter = all_adapters()[0]
        paths = adapter.paths(self.env, Scope.GLOBAL)
        self.assertIsNotNone(paths.skills_dir)
        paths.skills_dir.mkdir(parents=True, exist_ok=True)

        external = self.root / "external_skills" / "undo-skill"
        external.mkdir(parents=True)
        (external / "SKILL.md").write_text("# Undo Skill\n", encoding="utf-8")

        tool_link = paths.skills_dir / "undo-skill"
        fsops.create_dir_link(tool_link, external)

        detections = [
            Detection(
                installed=True, tool=adapter.id, label=adapter.label, binary=Path("/bin/adapter")
            )
        ]
        ctx = self._ctx()
        with patch("one_setup_all_agents.commands.init.detect_all", return_value=detections):
            parser = build_parser()
            args = parser.parse_args(["init", "--yes"])
            code = init.run(args, ctx)
        self.assertEqual(code, 0)

        # Apply relink via sync
        source = load_source(self.env.config_dir, [adapter.id])
        desired = desired_all(source, [adapter.id])
        plan = build_plan(self.env, [adapter], desired)
        sync_op_id = apply_plan(self.env, plan, label="sync", run_cli=FakeRunner())

        # Link now points to shared config
        shared_skill = self.env.config_dir / "skills" / "undo-skill"
        self.assertTrue(fsops.same_target(fsops.read_link(tool_link), shared_skill))

        # Revert sync operation: restores link to external target
        revert_operation(self.env, sync_op_id, run_cli=FakeRunner())
        self.assertTrue(fsops.same_target(fsops.read_link(tool_link), external))


if __name__ == "__main__":
    unittest.main()
