"""Dynamic half of the "a new tool touches one file" proof (docs/decisions/0008, "Consequences").

`demo_tool_adapter.py` is a complete new tool in ONE module. Here it is added the way a
contributor adds a tool: one entry in the registry tuple (patched, so the real registry file is
not edited). With no other change it must

- be found by `all_adapters()` and `get_adapter()`;
- plan, apply and undo through the real engine in the same home as the four real tools, without
  touching their files;
- pass the shared contract suite through the existing seams (`contract_case_for`,
  `REGISTERED_SEEDS`); no shared harness code is edited;
- name only paths under its own config directory.

The static half (nothing outside the adapter modules names a tool) is `test_tool_boundary.py`.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
import adapter_contract
from antigravity_support import seed_antigravity_home
from claude_support import contract_namespace as claude_namespace
from claude_support import seed_claude_home
from codex_support import seed_codex_home
from demo_tool_adapter import ROOT_NAME, DemoToolAdapter, seed_demo_tool_home
from one_setup_all_agents import adapters
from one_setup_all_agents.adapters import all_adapters, get_adapter
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.plan import DesiredState, build_plan
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import (
    Conflict,
    EditFile,
    Env,
    Kind,
    Link,
    McpServer,
    Op,
    Scope,
    SkillRef,
)
from opencode_support import seed_opencode_home
from test_adapter_contract import REGISTERED_SEEDS, contract_case_for

REAL_IDS = ["opencode", "claude", "codex", "agy"]
REAL_ADAPTERS = tuple(adapters.ADAPTERS)

# -- the one registry line a contributor adds ------------------------------------------------------

#: The registry as it would read after the contributor's one-line edit to `adapters/__init__.py`.
REGISTRY_WITH_DEMO = (*REAL_ADAPTERS, DemoToolAdapter)


def with_demo_registered():
    return mock.patch.object(adapters, "ADAPTERS", REGISTRY_WITH_DEMO)


# -- the shared contract suite, wired through its existing seam ---------------------------------------

with with_demo_registered(), mock.patch.dict(REGISTERED_SEEDS, {"demo-tool": seed_demo_tool_home}):
    _adapter_class = type(get_adapter("demo-tool"))
    Contract_DemoToolAdapter = contract_case_for(_adapter_class)
Contract_DemoToolAdapter.__module__ = __name__
del _adapter_class


# -- helpers ----------------------------------------------------------------------------------------


def op_paths(op: Op) -> list[Path]:
    """Every file or link path an op names."""
    if isinstance(op, EditFile):
        return [op.path]
    if isinstance(op, Link):
        return [op.link]
    if isinstance(op, Conflict):
        return [op.path] if op.path else []
    return []


def is_under(path: Path, root: Path) -> bool:
    return adapter_contract.is_under(path, root)


class DropInHome:
    """One temporary home for the four real tools and the demo tool, and the means to drive it."""

    def __init__(self, case: unittest.TestCase) -> None:
        root = Path(tempfile.mkdtemp(prefix="osaa-dropin-")).resolve()
        case.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.root = root
        # Claude Code finds its (fake) binary through the environment and edits MCP via a CLI.
        hooks = claude_namespace()
        self.env: Env = hooks["make_env"](None, root)
        self.cli = hooks["make_cli"](None, self.env)
        for directory in (self.env.home, self.env.config_dir, self.env.state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        for seed in (
            seed_opencode_home,
            seed_claude_home,
            seed_codex_home,
            seed_antigravity_home,
            seed_demo_tool_home,
        ):
            try:
                seed(self.env)
            except FileExistsError:
                # OpenCode and Codex share `~/.agents/skills/user-skill`; the second seed has
                # written its own config by then and finds the shared folder already there.
                pass
        self.demo_root = self.env.home / ROOT_NAME
        self.demo_rel = self.demo_root.relative_to(root).as_posix()

    def in_demo_folder(self, rel: str) -> bool:
        return rel == self.demo_rel or rel.startswith(self.demo_rel + "/")

    def desired(self, *, skills: tuple[str, ...] = ("demo-skill-a", "demo-skill-b")):
        sources = []
        for name in skills:
            folder = self.env.config_dir / "skills" / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\nDemo.\n", "utf-8")
            sources.append(SkillRef(name=name, path=folder))
        return DesiredState(
            instructions="# Demo instructions\n\nBe brief.\n",
            mcp={"demo-server": McpServer(name="demo-server", command="demo-cmd", args=("-v",))},
            skills=sources,
        )

    def snapshot(self, *, outside_demo: bool = False) -> dict:
        """The home and config, without the engine state; optionally without the demo's folder."""
        snap = adapter_contract.snapshot(self.root, exclude=(self.env.state_dir,))
        if not outside_demo:
            return snap
        return {k: v for k, v in snap.items() if not self.in_demo_folder(k)}

    def apply(self, plan) -> str | None:
        return apply_plan(self.env, plan, label="drop-in", run_cli=self.cli)

    def revert(self, op_id: str) -> None:
        revert_operation(self.env, op_id, run_cli=self.cli)


# -- tests ------------------------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_the_real_registry_is_unchanged_by_these_tests(self):
        self.assertEqual([cls.id for cls in adapters.ADAPTERS], REAL_IDS)
        with self.assertRaises(KeyError):
            get_adapter("demo-tool")

    def test_the_one_registry_line_makes_the_tool_visible(self):
        with with_demo_registered():
            self.assertEqual([a.id for a in all_adapters()], [*REAL_IDS, "demo-tool"])
            self.assertIsInstance(get_adapter("demo-tool"), DemoToolAdapter)
            self.assertEqual(get_adapter("opencode").id, "opencode")
        self.assertEqual([a.id for a in all_adapters()], REAL_IDS)  # and only while patched

    def test_the_tool_id_is_not_taken_by_a_real_adapter(self):
        self.assertNotIn(DemoToolAdapter.id, REAL_IDS)


class SameHomeTests(unittest.TestCase):
    """The demo tool next to the four real tools, applied and undone by the real engine."""

    def test_demo_ops_touch_only_the_demo_folder_and_leave_the_real_tools_alone(self):
        home = DropInHome(self)
        with with_demo_registered():
            demo = [get_adapter("demo-tool")]
            desired = home.desired()  # creates the skill sources, so before the snapshot
            before = home.snapshot(outside_demo=True)
            plan = build_plan(home.env, demo, desired)
            self.assertTrue(plan.executable)
            for op in plan.executable:
                for path in op_paths(op):
                    self.assertTrue(is_under(path, home.demo_root), f"{path} is outside")
            op_id = home.apply(plan)
            self.assertIsNotNone(op_id)
            self.assertEqual(home.snapshot(outside_demo=True), before)  # the real tools' files
            self.assertTrue((home.demo_root / "DEMO.md").is_file())
            self.assertIn("demo-server", demo[0].read_mcp(home.env, Scope.GLOBAL))
            self.assertTrue(build_plan(home.env, demo, desired).is_empty)
            home.revert(op_id)
            self.assertEqual(home.snapshot(outside_demo=True), before)

    def test_all_five_tools_plan_apply_and_undo_together(self):
        home = DropInHome(self)
        with with_demo_registered():
            tools = all_adapters()
            self.assertEqual(len(tools), 5)
            desired = home.desired()
            before = home.snapshot()
            plan = build_plan(home.env, tools, desired)
            self.assertEqual(plan.conflicts, [], plan.conflicts)
            # an op under the demo folder is the demo tool's, and only the demo tool's
            for op in plan.executable:
                refs = getattr(op, "claims", ()) + getattr(op, "releases", ())
                under_demo = any(is_under(p, home.demo_root) for p in op_paths(op))
                self.assertEqual(under_demo, {r.tool for r in refs} == {"demo-tool"}, op)
            op_id = home.apply(plan)
            self.assertIsNotNone(op_id)
            for tool in tools:
                self.assertTrue(
                    tool.read_instructions(home.env, Scope.GLOBAL), f"{tool.id}: instructions"
                )
                self.assertIn("demo-server", tool.read_mcp(home.env, Scope.GLOBAL), tool.id)
                names = {s.name for s in tool.read_skills(home.env, Scope.GLOBAL)}
                self.assertTrue({"demo-skill-a", "demo-skill-b"} <= names, f"{tool.id}: skills")
            self.assertTrue(build_plan(home.env, tools, desired).is_empty)  # nothing left to do
            home.revert(op_id)
            self.assertEqual(home.snapshot(), before)

    def test_undoing_the_demo_tool_alone_leaves_the_four_real_tools_as_they_were(self):
        home = DropInHome(self)
        with with_demo_registered():
            desired = home.desired()
            real_id = home.apply(build_plan(home.env, all_adapters()[:4], desired))
            after_real = home.snapshot()
            demo_id = home.apply(build_plan(home.env, [get_adapter("demo-tool")], desired))
            outside = {k: v for k, v in after_real.items() if not home.in_demo_folder(k)}
            self.assertEqual(home.snapshot(outside_demo=True), outside)
            home.revert(demo_id)
            self.assertEqual(home.snapshot(), after_real)
            home.revert(real_id)


class OneConfigDirectoryTests(unittest.TestCase):
    """A tool with one config folder produces ops on its own files and nowhere else."""

    def test_every_op_in_every_state_names_a_path_in_its_own_folder(self):
        home = DropInHome(self)
        adapter = DemoToolAdapter()
        paths = adapter.paths(home.env, Scope.GLOBAL)
        own_files = {paths.instructions, paths.mcp}
        seen_edits: set[Path] = set()
        seen_links = 0
        states = [
            home.desired(),
            DesiredState(
                instructions="# Changed\n",
                mcp={"demo-server": McpServer(name="demo-server", command="other-cmd")},
                skills=home.desired(skills=("demo-skill-a",)).skills,
            ),
            DesiredState(instructions="# Changed\n", mcp={}, skills=[]),
        ]
        for desired in states:
            plan = build_plan(home.env, [adapter], desired)
            self.assertTrue(plan.executable)
            for op in plan.ops:
                for path in op_paths(op):
                    self.assertTrue(is_under(path, home.demo_root), f"{path} is outside")
                if isinstance(op, EditFile):
                    self.assertIn(op.path, own_files)
                    seen_edits.add(op.path)
                if isinstance(op, Link):
                    self.assertTrue(is_under(op.link, paths.skills_dir), f"{op.link}")
                    seen_links += 1
            home.apply(plan)
        self.assertEqual(seen_edits, own_files)  # both its files were edited, and no others
        self.assertGreater(seen_links, 0)

    def test_only_its_own_folder_changes_in_the_home(self):
        home = DropInHome(self)
        desired = home.desired()  # creates the skill sources, so before the snapshot
        before = home.snapshot()
        home.apply(build_plan(home.env, [DemoToolAdapter()], desired))
        after = home.snapshot()
        changed = {k for k in after if after[k] != before.get(k)} | (before.keys() - after.keys())
        self.assertTrue(changed)
        for rel in changed:
            self.assertTrue(home.in_demo_folder(rel), f"{rel} changed outside the tool's folder")


class ContractSeamTests(unittest.TestCase):
    def test_the_contract_class_is_the_full_suite_and_is_collected_by_discovery(self):
        # Contract_DemoToolAdapter is a module global, so `unittest discover` runs it.
        self.assertTrue(issubclass(Contract_DemoToolAdapter, adapter_contract.AdapterContract))
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Contract_DemoToolAdapter)
        self.assertGreater(suite.countTestCases(), 15)

    def test_the_shared_seed_table_is_unchanged(self):
        self.assertNotIn("demo-tool", REGISTERED_SEEDS)  # only patched while the class was built
        self.assertEqual(sorted(REGISTERED_SEEDS), sorted(REAL_IDS))

    def test_the_kinds_the_demo_tool_declares_are_the_three_core_kinds(self):
        self.assertEqual(
            DemoToolAdapter.capabilities, frozenset({Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS})
        )


if __name__ == "__main__":
    unittest.main()
