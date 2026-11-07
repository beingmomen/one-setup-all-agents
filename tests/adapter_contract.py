"""The reusable adapter contract suite (docs/decisions/0008 and 0009).

Not collected on its own (the name does not start with `test`). A test module mixes
`AdapterContract` into a `unittest.TestCase` and supplies the adapter:

    class OpenCodeContract(AdapterContract, unittest.TestCase):
        def make_adapter(self):
            return OpenCodeAdapter()

        def seed_home(self, env):  # optional: the user's own native config, before all-agents
            ...

`tests/test_adapter_contract.py` does this for `ExampleAdapter`, for deliberately broken
adapters (to prove each check can fail) and for every registered adapter.

What it checks, per capability the adapter declares:

- reading what a plan produces returns the desired state, after the adapter's `normalize_*`
- a second plan after the apply is empty (`Plan.is_empty`)
- undo returns the whole home to its earlier bytes (files, folders, links); absent stays absent
- `detect`, `paths`, `read_*` and every `plan_*` write nothing
- entries the user made (not owned) are left alone; an owned entry that left `desired` goes
- an existing, unowned destination is a `Conflict`, never an overwrite
- every path an adapter names stays under the `Env` it was given
- every kind outside `capabilities` plans as `Unsupported`; none is silently dropped
- every `RunCli` has a non-empty `inverse_argv`
- ops claim exactly the items they create (that is how the ledger learns about them)

- the ledger's `owned` table follows the ops: claims add, releases remove, and an undo puts it back

The ops are applied by `contract_applier.make_applier`, which is the REAL engine (`engine/`):
journal, backups, atomic writes, drift preflight, ledger. The harness reads `owned` from the
engine's ledger, not from bookkeeping of its own. What a pass proves and what it does not is in
docs/decisions/0010-engine-v1.md ("What the contract suite proves").
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
import contract_applier
from contract_applier import is_link, link_target, sha256_of
from one_setup_all_agents.adapters.base import ToolAdapter
from one_setup_all_agents.engine import journal
from one_setup_all_agents.model import (
    INSTRUCTIONS_KEY,
    EditFile,
    Env,
    HeaderRef,
    Kind,
    Link,
    McpServer,
    Plan,
    RunCli,
    Scope,
    SkillRef,
    Unsupported,
)
from one_setup_all_agents.paths import detect_platform

CORE_KINDS = (Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS)
OPTIONAL_KINDS = (Kind.HOOKS, Kind.PERMISSIONS, Kind.MEMORY)


def snapshot(root: Path, exclude: tuple[Path, ...] = ()) -> dict[str, tuple]:
    """Every folder, file (with bytes) and link (with target) under `root`, by relative path.

    `exclude` lists folders to leave out (the engine's own state directory: the journal of an
    undone operation is meant to stay, it is not part of "the home").
    """
    out: dict[str, tuple] = {}
    skip = {os.path.normcase(os.path.abspath(p)) for p in exclude}

    def walk(directory: Path) -> None:
        for name in sorted(os.listdir(directory)):
            path = directory / name
            if os.path.normcase(os.path.abspath(path)) in skip:
                continue
            rel = path.relative_to(root).as_posix()
            if is_link(path):
                out[rel] = ("link", str(link_target(path)))
            elif path.is_dir():
                out[rel] = ("dir",)
                walk(path)
            else:
                out[rel] = ("file", path.read_bytes())

    walk(root)
    return out


def is_under(path: Path, root: Path) -> bool:
    norm = lambda p: Path(os.path.normcase(os.path.abspath(p)))  # noqa: E731
    return norm(path).is_relative_to(norm(root))


@dataclass
class Desired:
    """The neutral desired state for the three v0.1 kinds."""

    instructions: str
    mcp: dict[str, McpServer]
    skills: list[SkillRef]
    #: MCP names this adapter deliberately does not write (see `AdapterContract.unwritten_mcp`)
    unwritten: frozenset[str] = frozenset()

    def keys(self, kind: Kind) -> set[str]:
        return {
            Kind.INSTRUCTIONS: {INSTRUCTIONS_KEY},
            Kind.MCP: set(self.mcp) - self.unwritten,
            Kind.SKILLS: {s.name for s in self.skills},
        }[kind]


class Harness:
    """One fresh fake home, applied to and undone by the real engine.

    `owned` is read from the engine's ledger (and, for the tests that simulate a ledger that
    forgot something, written to it); the harness keeps no list of its own.
    """

    def __init__(self, case: AdapterContract, seed: bool) -> None:
        root = Path(tempfile.mkdtemp(prefix="osaa-contract-")).resolve()
        case.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.root = root
        self.case = case
        self.env = case.make_env(root)
        for directory in (self.env.home, self.env.config_dir, self.env.state_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.adapter = case.make_adapter()
        if seed:
            case.seed_home(self.env)
        self.cli = case.make_cli(self.env)
        self.applier = contract_applier.make_applier(self.env, self.cli)
        self.plans: list[Plan] = []  # every plan made, for the checks over all ops
        # (applier receipt, snapshot of the home before, ledger `owned` before)
        self.receipts: list[tuple[str | None, dict, dict]] = []

    # -- the home and the ledger ---------------------------------------------------------------

    def snap(self) -> dict[str, tuple]:
        """The whole fake home and config, without the engine's own state directory."""
        return snapshot(self.root, exclude=(self.env.state_dir,))

    @property
    def owned(self) -> dict[Kind, set[str]]:
        """The adapter's owned keys per core kind, as the engine's ledger records them."""
        return {
            kind: set(journal.owned_for(self.env.state_dir, self.adapter.id, kind.value))
            for kind in CORE_KINDS
        }

    @owned.setter
    def owned(self, value: dict[Kind, set[str]]) -> None:
        for kind in CORE_KINDS:
            journal.set_owned(self.env.state_dir, self.adapter.id, kind.value, value[kind])

    # -- desired state ---------------------------------------------------------------------

    def skill_source(self, name: str, variant: str = "") -> SkillRef:
        folder = self.env.config_dir / "skills" / f"{name}{variant}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\nDemo skill {name}.\n", "utf-8")
        return SkillRef(name=name, path=folder)

    def desired(self) -> Desired:
        return Desired(
            instructions="# Demo instructions\n\nBe brief.\n",
            mcp={
                "demo-server": McpServer(
                    name="demo-server",
                    command="demo-cmd",
                    args=("--flag", "value"),
                    env_vars=("DEMO_TOKEN",),
                    env=(("DEMO_MODE", "test"), ("DEMO_LEVEL", "2")),
                    cwd="work",
                    startup_timeout_sec=60,
                ),
                "demo-remote": McpServer(
                    name="demo-remote",
                    url="https://demo.example.test/mcp",
                    headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),),
                    auth="oauth",
                ),
                "demo-off": McpServer(name="demo-off", command="demo-off-cmd", enabled=False),
            },
            skills=[self.skill_source("demo-skill-a"), self.skill_source("demo-skill-b")],
            unwritten=frozenset(self.case.unwritten_mcp()),
        )

    def changed(self, d: Desired) -> Desired:
        """The same state with one change in each kind."""
        mcp = dict(d.mcp)
        mcp["demo-server"] = McpServer(
            name="demo-server",
            command="demo-cmd",
            args=("--other",),
            env_vars=("DEMO_TOKEN",),
            env=(("DEMO_MODE", "other"),),
        )
        return Desired(d.instructions + "More.\n", mcp, list(d.skills), d.unwritten)

    def reduced(self, d: Desired) -> Desired:
        """The same state with one MCP server (`removable_mcp`) and one skill gone."""
        mcp = {k: v for k, v in d.mcp.items() if k != self.case.removable_mcp()}
        skills = [s for s in d.skills if s.name != "demo-skill-b"]
        return Desired(d.instructions, mcp, skills, d.unwritten)

    # -- planning and applying ---------------------------------------------------------------

    def plan_kind(self, kind: Kind, desired: Desired, owned: dict | None = None) -> list:
        owned = self.owned if owned is None else owned
        a, env, scope = self.adapter, self.env, Scope.GLOBAL
        if kind is Kind.INSTRUCTIONS:
            return a.plan_instructions(env, scope, desired.instructions, frozenset(owned[kind]))
        if kind is Kind.MCP:
            return a.plan_mcp(env, scope, desired.mcp, frozenset(owned[kind]))
        if kind is Kind.SKILLS:
            return a.plan_skills(env, scope, desired.skills, frozenset(owned[kind]))
        method = {
            Kind.HOOKS: a.plan_hooks,
            Kind.PERMISSIONS: a.plan_permissions,
            Kind.MEMORY: a.plan_memory,
        }[kind]
        return method(env, scope, None, frozenset())

    def plan(self, desired: Desired, owned: dict | None = None) -> Plan:
        """What the engine's `build_plan` will do: ask for every kind, in a fixed order."""
        ops: list = []
        for kind in (*CORE_KINDS, *OPTIONAL_KINDS):
            ops.extend(self.plan_kind(kind, desired, owned))
        plan = Plan(ops)
        self.plans.append(plan)
        return plan

    def sync(self, desired: Desired) -> Plan:
        """Plan, then apply with the engine (which updates the ledger from the ops' claims)."""
        before, owned_before = self.snap(), self.sorted_owned()
        plan = self.plan(desired)
        receipt = self.applier.apply(plan)
        self.receipts.append((receipt, before, owned_before))
        return plan

    def undo_last(self) -> dict:
        """Undo the latest sync with the engine; return the snapshot taken before it."""
        receipt, before, _owned_before = self.receipts.pop()
        self.applier.undo(receipt)
        return before

    def all_ops(self) -> list:
        return [op for plan in self.plans for op in plan.ops]

    def sorted_owned(self) -> dict:
        return {kind.value: sorted(keys) for kind, keys in self.owned.items()}


class AdapterContract:
    """Mixin: hooks to override at the top, the checks below. Combine with `unittest.TestCase`."""

    # -- hooks -------------------------------------------------------------------------------

    def make_adapter(self) -> ToolAdapter:
        raise NotImplementedError("the test class must provide make_adapter()")

    def seed_home(self, env: Env) -> None:
        """Create the user's own native config (for example one MCP server of the user's own)."""

    def unwritten_mcp(self) -> frozenset[str]:
        """Names of the harness's desired MCP servers that this adapter deliberately does not write
        (for example `demo-off`, which has `enabled=False`, for a tool with no per-server switch).
        They must be absent from `normalize_mcp`, from the claims and from `owned`; nothing else
        may be dropped."""
        return frozenset()

    def removable_mcp(self) -> str:
        """The harness server that `reduced` removes. It must be one the adapter writes (and so
        owns): the default is `demo-remote`; an adapter that cannot write that one names another."""
        return "demo-remote"

    def make_cli(self, env: Env) -> Callable | None:
        """A fake for the adapter's CLI if it plans `RunCli` ops; it must never run a program."""
        return None

    def make_env(self, root: Path) -> Env:
        return Env(
            home=root / "home",
            platform=detect_platform(),
            environ={},
            config_dir=root / "config",
            state_dir=root / "state",
        )

    # -- helpers -------------------------------------------------------------------------------

    def caps(self, h: Harness) -> frozenset:
        return h.adapter.capabilities

    # -- the checks -----------------------------------------------------------------------------

    def test_paths_stay_under_env(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                paths = h.adapter.paths(h.env, Scope.GLOBAL)
                for name in ("instructions", "mcp", "skills_dir", "settings"):
                    value = getattr(paths, name)
                    if value is not None:
                        self.assertTrue(
                            is_under(value, h.env.home),
                            f"paths().{name} = {value} is outside the given Env home {h.env.home}",
                        )
                needed = {
                    Kind.INSTRUCTIONS: paths.instructions,
                    Kind.MCP: paths.mcp,
                    Kind.SKILLS: paths.skills_dir,
                }
                for kind in self.caps(h) & set(needed):
                    self.assertIsNotNone(needed[kind], f"paths() has no location for {kind.value}")
                for root in h.adapter.detect(h.env).config_roots:
                    self.assertTrue(is_under(root, h.env.home), f"detect() root {root}")

    def test_paths_follow_the_env_they_are_given(self):
        a, b = Harness(self, False), Harness(self, False)
        self.assertNotEqual(a.env.home, b.env.home)
        self.assertEqual(
            a.adapter.paths(a.env, Scope.GLOBAL).skills_dir is None,
            b.adapter.paths(b.env, Scope.GLOBAL).skills_dir is None,
        )
        for name in ("instructions", "mcp", "skills_dir"):
            pa = getattr(a.adapter.paths(a.env, Scope.GLOBAL), name)
            pb = getattr(b.adapter.paths(b.env, Scope.GLOBAL), name)
            if pa is not None:
                self.assertTrue(is_under(pb, b.env.home), f"{name} is not derived from the Env")
                self.assertFalse(is_under(pb, a.env.home), f"{name} leaks the other Env")

    def test_reading_and_planning_write_nothing(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                env, scope, a = h.env, Scope.GLOBAL, h.adapter
                d = h.desired()  # creates the skill sources in the config dir
                before = h.snap()

                def touch_everything(desired: Desired):
                    a.detect(env)
                    a.paths(env, scope)
                    a.read_instructions(env, scope)
                    a.read_mcp(env, scope)
                    a.read_skills(env, scope)
                    h.plan(desired)
                    h.plan(Desired("", {}, []))

                touch_everything(d)
                self.assertEqual(h.snap(), before, "detect/read/plan changed the home")
                h.sync(d)
                after_sync = h.snap()
                touch_everything(h.changed(d))
                touch_everything(h.reduced(d))
                self.assertEqual(h.snap(), after_sync, "an update/removal plan wrote to disk")

    def test_reading_what_a_plan_produces_returns_the_desired_state(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                a, d = h.adapter, h.desired()
                h.sync(d)
                if Kind.INSTRUCTIONS in self.caps(h):
                    got = a.read_instructions(h.env, Scope.GLOBAL)
                    self.assertIsNotNone(got, "no instructions after the apply")
                    self.assertEqual(got.text, a.normalize_instructions(d.instructions))
                    self.assertEqual(got.path, a.paths(h.env, Scope.GLOBAL).instructions)
                if Kind.MCP in self.caps(h):
                    expected = a.normalize_mcp(d.mcp)
                    self.assertEqual(
                        set(expected),
                        set(d.mcp) - d.unwritten,
                        "normalize_mcp must keep the names, except the declared unwritten ones",
                    )
                    read = a.read_mcp(h.env, Scope.GLOBAL)
                    for name, server in expected.items():
                        self.assertIn(name, read, f"MCP server {name} was not written")
                        self.assertEqual(
                            read[name], server, f"MCP server {name} did not round-trip"
                        )
                if Kind.SKILLS in self.caps(h):
                    read = {s.name: s for s in a.read_skills(h.env, Scope.GLOBAL)}
                    for skill in d.skills:
                        self.assertIn(skill.name, read, f"skill {skill.name} is not visible")
                        got = read[skill.name]
                        if got.is_link:
                            want = Path(os.path.realpath(skill.path))
                            self.assertEqual(link_target(got.path), want)

    def test_every_declared_kind_acts_on_an_empty_home(self):
        h = Harness(self, False)
        d = h.desired()
        for kind in CORE_KINDS:
            if kind in self.caps(h):
                ops = h.plan_kind(kind, d)
                self.assertTrue(
                    any(isinstance(o, (EditFile, RunCli, Link)) for o in ops),
                    f"{kind.value} is declared but nothing is planned for it",
                )

    def test_ops_claim_the_items_they_create(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                d = h.desired()
                plan = h.sync(d)
                claimed = {kind: set() for kind in CORE_KINDS}
                for op in plan.executable:
                    self.assertEqual(op.releases, (), "the first sync must not release anything")
                    for ref in op.claims:
                        self.assertEqual(ref.tool, h.adapter.id, "a claim names another tool")
                        claimed[ref.kind].add(ref.key)
                for kind in CORE_KINDS:
                    if kind in self.caps(h):
                        self.assertEqual(claimed[kind], d.keys(kind), f"claims for {kind.value}")

    def test_ops_are_well_formed_and_inside_the_env(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                d = h.desired()
                for desired in (d, h.changed(d), h.reduced(d)):
                    plan = h.plan(desired)
                    for op in plan.executable:
                        self.assertTrue(op.description, "an op has no description")
                        if isinstance(op, EditFile):
                            self.assertTrue(is_under(op.path, h.env.home), f"{op.path} is outside")
                            on_disk = Path(op.path)
                            want = sha256_of(on_disk.read_bytes()) if on_disk.is_file() else None
                            self.assertEqual(
                                op.expect_sha256, want, f"stale expect_sha256 for {op.path}"
                            )
                        if isinstance(op, Link):
                            self.assertTrue(is_under(op.link, h.env.home), f"{op.link} is outside")
                    h.sync(desired)

    def test_second_plan_after_apply_is_empty(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                d = h.desired()
                for step, desired in enumerate((d, h.changed(d), h.reduced(d))):
                    first = h.sync(desired)
                    self.assertTrue(first.executable, f"step {step}: nothing was planned")
                    again = h.plan(desired)
                    self.assertTrue(
                        again.is_empty, f"step {step}: second plan is not empty: {again.ops}"
                    )

    def test_undo_restores_the_original_bytes(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                d = h.desired()
                original = h.snap()
                for desired in (d, h.changed(d), h.reduced(d)):
                    h.sync(desired)
                while h.receipts:
                    expected, expected_owned = h.receipts[-1][1], h.receipts[-1][2]
                    h.undo_last()
                    self.assertEqual(h.snap(), expected, "undo did not restore the files")
                    self.assertEqual(
                        h.sorted_owned(), expected_owned, "undo did not restore the ownership"
                    )
                self.assertEqual(h.snap(), original, "undoing everything did not restore the home")

    def test_the_ledger_owns_exactly_what_the_ops_claimed(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                d = h.desired()
                h.sync(d)
                for kind in CORE_KINDS:
                    if kind in self.caps(h):
                        self.assertEqual(h.owned[kind], d.keys(kind), kind.value)
                h.sync(h.reduced(d))
                if Kind.MCP in self.caps(h):
                    self.assertEqual(h.owned[Kind.MCP], h.reduced(d).keys(Kind.MCP))
                if Kind.SKILLS in self.caps(h):
                    self.assertEqual(h.owned[Kind.SKILLS], {"demo-skill-a"})

    def test_undo_leaves_absent_files_absent(self):
        h = Harness(self, False)
        home_before = snapshot(h.env.home)
        self.assertEqual(home_before, {})
        h.sync(h.desired())
        self.assertNotEqual(snapshot(h.env.home), {}, "the apply created nothing in the home")
        h.undo_last()
        self.assertEqual(snapshot(h.env.home), {}, "undo left files or folders in the home")

    def test_entries_the_user_made_are_untouched(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                a, d = h.adapter, h.desired()
                user_mcp = a.read_mcp(h.env, Scope.GLOBAL)
                user_skills = {s.name for s in a.read_skills(h.env, Scope.GLOBAL)}
                h.sync(d)
                # Pretend the ledger only knows part of what was written: the rest now looks like
                # the user's own entries (an unowned entry that is not in `desired`).
                only_server = {"demo-server": d.mcp["demo-server"]}
                partial = Desired(d.instructions, only_server, d.skills[:1])
                owned = {
                    Kind.INSTRUCTIONS: {INSTRUCTIONS_KEY},
                    Kind.MCP: {"demo-server"},
                    Kind.SKILLS: {"demo-skill-a"},
                }
                h.owned = owned
                others_before = a.read_mcp(h.env, Scope.GLOBAL)
                skills_before = {s.name: s for s in a.read_skills(h.env, Scope.GLOBAL)}
                plan = h.plan(partial)
                self.assertTrue(plan.is_empty, f"nothing should change: {plan.ops}")
                h.sync(h.changed(partial))
                read = a.read_mcp(h.env, Scope.GLOBAL)
                for name in set(user_mcp) | ({"demo-remote", "demo-off"} - d.unwritten):
                    self.assertIn(name, read, f"unowned MCP server {name} was removed")
                    self.assertEqual(read[name], others_before[name], f"unowned {name} was changed")
                names = {s.name: s for s in a.read_skills(h.env, Scope.GLOBAL)}
                for name in user_skills | {"demo-skill-b"}:
                    self.assertIn(name, names, f"unowned skill {name} was removed")
                    self.assertEqual(names[name].path, skills_before[name].path)

    def test_owned_entry_that_leaves_desired_is_removed(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                a, d = h.adapter, h.desired()
                h.sync(d)
                plan = h.sync(h.reduced(d))
                released = {kind: set() for kind in CORE_KINDS}
                for op in plan.executable:
                    for ref in op.releases:
                        released[ref.kind].add(ref.key)
                if Kind.MCP in self.caps(h):
                    gone = self.removable_mcp()
                    self.assertNotIn(gone, a.read_mcp(h.env, Scope.GLOBAL))
                    self.assertEqual(released[Kind.MCP], {gone})
                    self.assertNotIn(gone, h.owned[Kind.MCP])
                if Kind.SKILLS in self.caps(h):
                    names = {s.name for s in a.read_skills(h.env, Scope.GLOBAL)}
                    self.assertNotIn("demo-skill-b", names)
                    self.assertEqual(released[Kind.SKILLS], {"demo-skill-b"})
                    self.assertIn("demo-skill-a", names)

    def test_unowned_existing_destination_is_a_conflict_not_an_overwrite(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                a, d = h.adapter, h.desired()
                h.sync(d)
                h.owned = {kind: set() for kind in CORE_KINDS}  # the ledger forgot: all unowned now
                other_a = h.skill_source("demo-skill-a", "-other")
                wanted = h.changed(d)
                wanted.skills = [SkillRef("demo-skill-a", other_a.path), *wanted.skills[1:]]
                before = h.snap()
                plan = h.plan(wanted)
                conflicts = {(c.kind, c.key): c for c in plan.conflicts}
                expected = {(Kind.INSTRUCTIONS, INSTRUCTIONS_KEY), (Kind.MCP, "demo-server")}
                expected.add((Kind.SKILLS, "demo-skill-a"))
                for kind, key in expected:
                    if kind in self.caps(h):
                        self.assertIn((kind, key), conflicts, f"no Conflict for {kind.value}/{key}")
                        c = conflicts[(kind, key)]
                        self.assertEqual(c.tool, a.id)
                        self.assertTrue(c.reason)
                self.assertEqual(plan.executable, [], f"unowned items would be changed: {plan.ops}")
                self.assertFalse(plan.is_empty, "a plan with conflicts must not look empty")
                h.applier.apply(plan)
                self.assertEqual(h.snap(), before, "a conflict changed the home")

    def test_unowned_real_skill_folder_is_a_conflict(self):
        h = Harness(self, False)
        if Kind.SKILLS not in self.caps(h):
            self.skipTest("the adapter does not declare skills")
        d = h.desired()
        dest = h.adapter.paths(h.env, Scope.GLOBAL).skills_dir / "demo-skill-a"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("the user's own skill\n", "utf-8")
        before = h.snap()
        plan = h.plan(d)
        self.assertIn(
            (Kind.SKILLS, "demo-skill-a"),
            {(c.kind, c.key) for c in plan.conflicts},
            "an unowned skill folder is not reported as a conflict",
        )
        for op in plan.executable:
            self.assertNotIn(dest, [getattr(op, "link", None)], "the folder would be replaced")
        h.applier.apply(plan)
        self.assertEqual((dest / "SKILL.md").read_text("utf-8"), "the user's own skill\n")
        self.assertEqual(
            {k: v for k, v in h.snap().items() if k.endswith("demo-skill-a/SKILL.md")},
            {k: v for k, v in before.items() if k.endswith("demo-skill-a/SKILL.md")},
        )

    def test_kinds_outside_capabilities_plan_as_unsupported(self):
        h = Harness(self, False)
        d = h.desired()
        plan = h.plan(d)
        for kind in Kind:
            if kind in self.caps(h):
                continue
            ops = h.plan_kind(kind, d)
            self.assertTrue(ops, f"{kind.value} is not declared and is silently dropped")
            for op in ops:
                self.assertIsInstance(op, Unsupported, f"{kind.value}: {op}")
                self.assertEqual(op.kind, kind)
                self.assertEqual(op.tool, h.adapter.id)
                self.assertTrue(op.reason)
            self.assertTrue(
                any(isinstance(o, Unsupported) and o.kind is kind for o in plan.ops),
                f"{kind.value} is missing from the full plan",
            )

    def test_run_cli_ops_have_an_inverse(self):
        for seed in (False, True):
            with self.subTest(seed=seed):
                h = Harness(self, seed)
                d = h.desired()
                for desired in (d, h.changed(d), h.reduced(d)):
                    self.assert_inverses(h.plan(desired))  # before the engine would refuse it
                    h.sync(desired)
                h.owned = {kind: set() for kind in CORE_KINDS}
                h.plan(h.changed(d))
                self.assert_inverses(Plan(h.all_ops()))

    def assert_inverses(self, plan: Plan) -> None:
        for op in plan.ops:
            if isinstance(op, RunCli):
                self.assertTrue(list(op.argv), "RunCli with no argv")
                self.assertTrue(list(op.inverse_argv), f"RunCli {list(op.argv)} has no inverse")
                self.assertNotEqual(list(op.argv), list(op.inverse_argv))
                self.assertTrue(all(isinstance(x, str) for x in op.argv))
                self.assertTrue(all(isinstance(x, str) for x in op.inverse_argv))

    # for the negative tests: how many RunCli ops the runs above produced
    def run_cli_count(self, h: Harness) -> int:
        return sum(isinstance(op, RunCli) for op in h.all_ops())


__all__ = ["AdapterContract", "Desired", "Harness", "snapshot", "is_under"]
