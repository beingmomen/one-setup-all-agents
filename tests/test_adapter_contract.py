"""The adapter contract suite, applied to a neutral example, to broken adapters and to the registry.

1. `ExampleAdapter` and `ExampleCliAdapter` (tests/example_adapter.py, id `example` and
   `example-cli`, not registered) pass the whole suite: the suite can be satisfied.
2. Deliberately broken adapters each fail the check that is meant to catch them: a contract that
   cannot fail proves nothing.
3. Every registered adapter gets a test class that runs the full suite, with no edit here (an
   optional seed function for its fake home goes in `REGISTERED_SEEDS`).

Ops are applied and undone by the REAL engine (see tests/contract_applier.py), so a pass here
exercises the undo guarantee of docs/decisions/0005; docs/decisions/0010 says exactly what that
proves and what it does not.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from adapter_contract import AdapterContract, Harness, is_under
from antigravity_support import contract_namespace as antigravity_namespace
from antigravity_support import seed_antigravity_home
from claude_support import contract_namespace as claude_namespace
from claude_support import seed_claude_home
from codex_support import seed_codex_home
from example_adapter import (
    ExampleAdapter,
    ExampleCliAdapter,
    FakeExampleCli,
    seed_example_home,
)
from one_setup_all_agents.adapters import ADAPTERS
from one_setup_all_agents.model import (
    INSTRUCTIONS_KEY,
    Conflict,
    EditFile,
    Env,
    HeaderRef,
    ItemRef,
    Kind,
    Link,
    McpServer,
    Plan,
    RunCli,
    Scope,
    Unsupported,
)
from opencode_support import seed_opencode_home

# -- 1. the example adapters pass ----------------------------------------------------------------


class ExampleContract(AdapterContract, unittest.TestCase):
    def make_adapter(self):
        return ExampleAdapter()

    def seed_home(self, env):
        seed_example_home(env)


class ExampleCliContract(AdapterContract, unittest.TestCase):
    def make_adapter(self):
        return ExampleCliAdapter()

    def seed_home(self, env):
        seed_example_home(env, cli=True)

    def make_cli(self, env):
        return FakeExampleCli(env.home)

    def test_this_variant_really_plans_run_cli_ops(self):
        h = Harness(self, True)
        d = h.desired()
        h.sync(d)
        h.sync(h.changed(d))
        h.sync(h.reduced(d))
        self.assertGreater(self.run_cli_count(h), 0)
        self.assertGreater(len(h.cli.calls), 0)  # went through the fake, no process


class ExampleSpecificTests(unittest.TestCase):
    """Behavior of the example adapter that the generic suite cannot know about."""

    # the hooks `Harness` asks its test case for
    make_env = AdapterContract.make_env
    unwritten_mcp = AdapterContract.unwritten_mcp
    removable_mcp = AdapterContract.removable_mcp

    def make_adapter(self):
        return ExampleAdapter()

    def seed_home(self, env):
        seed_example_home(env)

    def make_cli(self, env):
        return None

    def harness(self, seed=True):
        return Harness(self, seed)

    def test_instructions_over_the_limit_are_reported_not_written(self):
        h = self.harness(seed=False)
        too_long = "x" * (ExampleAdapter.INSTRUCTIONS_LIMIT + 1)
        ops = h.adapter.plan_instructions(h.env, Scope.GLOBAL, too_long, frozenset())
        self.assertEqual([type(o) for o in ops], [Conflict])
        self.assertEqual(ops[0].key, INSTRUCTIONS_KEY)
        self.assertIn("accepts", ops[0].reason)
        self.assertFalse(Plan(ops).is_empty)
        self.assertEqual(Plan(ops).executable, [])

    def test_other_top_level_keys_of_the_mcp_file_survive_an_update_and_undo(self):
        h = self.harness(seed=True)
        path = h.adapter.paths(h.env, Scope.GLOBAL).mcp
        original = path.read_bytes()
        h.sync(h.desired())
        self.assertEqual(json.loads(path.read_text("utf-8"))["theme"], "dark")
        h.undo_last()
        self.assertEqual(path.read_bytes(), original)

    def test_normalize_mcp_drops_exactly_what_the_format_cannot_store(self):
        h = self.harness(seed=False)
        server = h.desired().mcp["demo-server"]
        self.assertEqual(server.startup_timeout_sec, 60)
        normalized = h.adapter.normalize_mcp({"demo-server": server})["demo-server"]
        self.assertIsNone(normalized.startup_timeout_sec)
        self.assertEqual(replace(normalized, startup_timeout_sec=60), server)


# -- 2. broken adapters are caught ---------------------------------------------------------------


class NonIdempotent(ExampleAdapter):
    """Always plans a write, even when the file already has the wanted bytes."""

    def plan_instructions(self, env, scope, desired, owned):
        ops = super().plan_instructions(env, scope, desired, owned)
        if ops:
            return ops
        path = self.paths(env, scope).instructions
        return [
            EditFile(
                path,
                lambda _old: desired.encode("utf-8"),
                expect_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                description="rewrite the same text",
                claims=(ItemRef(self.id, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY),),
            )
        ]


class WritesWhilePlanning(ExampleAdapter):
    """Breaks the rule that adapters never write."""

    def plan_instructions(self, env, scope, desired, owned):
        scratch = self.paths(env, scope).instructions.with_name("scratch.tmp")
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text("planned", "utf-8")
        return super().plan_instructions(env, scope, desired, owned)


class OverwritesUnownedServers(ExampleAdapter):
    """Treats every desired MCP server as its own."""

    def plan_mcp(self, env, scope, desired, owned):
        return super().plan_mcp(env, scope, desired, frozenset(owned) | frozenset(desired))


class OverwritesUnownedSkills(ExampleAdapter):
    """Treats every desired skill as its own."""

    def plan_skills(self, env, scope, desired, owned):
        names = frozenset(s.name for s in desired)
        return super().plan_skills(env, scope, desired, frozenset(owned) | names)


class ReplacesRealSkillFolders(ExampleAdapter):
    """Turns the conflict on a user's own skill folder into a link over it."""

    def plan_skills(self, env, scope, desired, owned):
        sources = {s.name: s.path for s in desired}
        return [
            Link(
                op.path,
                sources[op.key],
                "replace",
                claims=(ItemRef(self.id, Kind.SKILLS, op.key),),
            )
            if isinstance(op, Conflict)
            else op
            for op in super().plan_skills(env, scope, desired, owned)
        ]


class PathsOutsideEnv(ExampleAdapter):
    """Computes a location that is not under the Env it was given."""

    def paths(self, env, scope):
        base = super().paths(env, scope)
        return replace(base, skills_dir=env.home.parent / "outside-the-env" / "skills")


class LossyRoundTrip(ExampleAdapter):
    """Drops `args` when writing, and does not say so in `normalize_mcp`."""

    @staticmethod
    def _encode(server):
        entry = ExampleAdapter._encode(server)
        entry.pop("args", None)
        return entry


class DropsLiteralEnv(ExampleAdapter):
    """Drops the literal `env` values when writing, and does not say so in `normalize_mcp`."""

    @staticmethod
    def _encode(server):
        entry = ExampleAdapter._encode(server)
        entry.pop("env", None)
        return entry


class DropsUnsupportedKinds(ExampleAdapter):
    def plan_hooks(self, env, scope, desired, owned):
        return []


class DeletesTheUsersEntries(ExampleAdapter):
    """Rewrites the whole servers table, so entries it does not own disappear."""

    def plan_mcp(self, env, scope, desired, owned):
        ops = super().plan_mcp(env, scope, desired, owned)
        edits = [op for op in ops if isinstance(op, EditFile)]
        if not edits:
            return ops
        table = {name: self._encode(server) for name, server in desired.items()}
        wiped = replace(
            edits[0],
            transform=lambda _old: (json.dumps({"servers": table}) + "\n").encode("utf-8"),
        )
        return [wiped]


class NeverClaims(ExampleAdapter):
    """Does the work but does not tell the ledger which items it created."""

    def plan_mcp(self, env, scope, desired, owned):
        return [
            replace(op, claims=(), releases=()) if isinstance(op, EditFile) else op
            for op in super().plan_mcp(env, scope, desired, owned)
        ]


class CliWithoutInverse(ExampleCliAdapter):
    def plan_mcp(self, env, scope, desired, owned):
        return [
            replace(op, inverse_argv=()) if isinstance(op, RunCli) else op
            for op in super().plan_mcp(env, scope, desired, owned)
        ]


class CliWithWrongInverse(ExampleCliAdapter):
    """The inverse of a removal runs but does not bring the server back as it was."""

    @staticmethod
    def _remove(name, existing, description, releases=()):
        op = ExampleCliAdapter._remove(name, existing, description, releases)
        return replace(op, inverse_argv=["example-cli", "mcp", "add", name, "{}"])


def probe(adapter_cls, test_name, *, cli=False):
    """Run ONE contract check against `adapter_cls` and return the unittest result."""
    namespace = {
        "make_adapter": lambda self: adapter_cls(),
        "seed_home": lambda self, env: seed_example_home(env, cli=cli),
        "make_cli": lambda self, env: FakeExampleCli(env.home) if cli else None,
    }
    case = type("Probe", (AdapterContract, unittest.TestCase), namespace)
    result = unittest.TestResult()
    case(test_name).run(result)
    return result


class BrokenAdaptersAreCaught(unittest.TestCase):
    def assert_caught(self, adapter_cls, test_name, needle, *, cli=False):
        result = probe(adapter_cls, test_name, cli=cli)
        self.assertEqual(result.errors, [], "the check crashed instead of failing")
        self.assertTrue(result.failures, f"{adapter_cls.__name__} slipped through {test_name}")
        text = "\n".join(trace for _, trace in result.failures)
        self.assertIn(needle, text)

    def test_the_good_adapters_pass_the_same_probe(self):
        for adapter_cls, cli in ((ExampleAdapter, False), (ExampleCliAdapter, True)):
            for name in (n for n in dir(AdapterContract) if n.startswith("test_")):
                result = probe(adapter_cls, name, cli=cli)
                self.assertEqual((result.errors, result.failures), ([], []), f"{name}")

    def test_non_idempotent_plan(self):
        self.assert_caught(NonIdempotent, "test_second_plan_after_apply_is_empty", "not empty")

    def test_plan_that_writes_to_disk(self):
        self.assert_caught(
            WritesWhilePlanning, "test_reading_and_planning_write_nothing", "changed the home"
        )

    def test_overwrite_of_an_unowned_mcp_server(self):
        self.assert_caught(
            OverwritesUnownedServers,
            "test_unowned_existing_destination_is_a_conflict_not_an_overwrite",
            "no Conflict for mcp/demo-server",
        )

    def test_overwrite_of_an_unowned_skill(self):
        self.assert_caught(
            OverwritesUnownedSkills,
            "test_unowned_existing_destination_is_a_conflict_not_an_overwrite",
            "no Conflict for skills/demo-skill-a",
        )

    def test_overwrite_of_an_unowned_real_skill_folder(self):
        self.assert_caught(
            ReplacesRealSkillFolders,
            "test_unowned_real_skill_folder_is_a_conflict",
            "not reported as a conflict",
        )

    def test_paths_outside_the_env(self):
        self.assert_caught(PathsOutsideEnv, "test_paths_stay_under_env", "outside the given Env")

    def test_lossy_round_trip(self):
        self.assert_caught(
            LossyRoundTrip,
            "test_reading_what_a_plan_produces_returns_the_desired_state",
            "did not round-trip",
        )

    def test_literal_env_values_that_are_dropped(self):
        self.assert_caught(
            DropsLiteralEnv,
            "test_reading_what_a_plan_produces_returns_the_desired_state",
            "did not round-trip",
        )

    def test_unsupported_kind_that_is_silently_dropped(self):
        self.assert_caught(
            DropsUnsupportedKinds,
            "test_kinds_outside_capabilities_plan_as_unsupported",
            "silently dropped",
        )

    def test_deleting_the_users_own_entries(self):
        self.assert_caught(
            DeletesTheUsersEntries, "test_entries_the_user_made_are_untouched", "was removed"
        )

    def test_ops_that_do_not_claim_what_they_create(self):
        self.assert_caught(NeverClaims, "test_ops_claim_the_items_they_create", "claims for mcp")

    def test_run_cli_without_an_inverse(self):
        self.assert_caught(
            CliWithoutInverse, "test_run_cli_ops_have_an_inverse", "inverse", cli=True
        )

    def test_an_inverse_that_does_not_restore_the_bytes(self):
        self.assert_caught(
            CliWithWrongInverse,
            "test_undo_restores_the_original_bytes",
            "did not restore",
            cli=True,
        )


# -- model: conflicts and the empty-plan rule -----------------------------------------------------


class PlanSemanticsTests(unittest.TestCase):
    CONFLICT = Conflict("example", Kind.MCP, "demo-server", "exists and is not owned")
    UNSUPPORTED = Unsupported("example", Kind.HOOKS, "no hooks")
    EDIT = EditFile(Path("f"), lambda old: b"", None, "write f")

    def test_empty_and_unsupported_only_plans_are_empty(self):
        self.assertTrue(Plan().is_empty)
        self.assertTrue(Plan([self.UNSUPPORTED]).is_empty)

    def test_a_conflict_only_plan_is_not_empty_and_has_nothing_to_run(self):
        plan = Plan([self.UNSUPPORTED, self.CONFLICT])
        self.assertFalse(plan.is_empty)
        self.assertEqual(plan.executable, [])
        self.assertEqual(plan.conflicts, [self.CONFLICT])

    def test_executable_ops_make_a_plan_non_empty(self):
        link = Link(Path("a"), Path("b"), "link")
        run = RunCli(["x"], ["y"], "run")
        for op in (self.EDIT, link, run):
            self.assertFalse(Plan([op]).is_empty)
            self.assertEqual(Plan([self.CONFLICT, op]).executable, [op])

    def test_a_link_can_be_a_removal(self):
        self.assertIsNone(Link(Path("a"), None, "withdraw").target)

    def test_ops_default_to_no_claims_and_no_releases(self):
        for op in (self.EDIT, Link(Path("a"), Path("b"), "l"), RunCli(["x"], ["y"], "r")):
            self.assertEqual((op.claims, op.releases), ((), ()))

    def test_item_refs_are_hashable_values(self):
        a = ItemRef("example", Kind.SKILLS, "demo-skill")
        self.assertEqual(a, ItemRef("example", Kind.SKILLS, "demo-skill"))
        self.assertEqual(len({a, ItemRef("example", Kind.SKILLS, "demo-skill")}), 1)


class McpServerShapeTests(unittest.TestCase):
    def test_transport_follows_the_fields(self):
        self.assertEqual(McpServer("a", command="demo-cmd").transport, "stdio")
        self.assertEqual(McpServer("b", url="https://demo.example.test/mcp").transport, "http")

    def test_headers_hold_variable_names_not_values(self):
        server = McpServer(
            "b",
            url="https://demo.example.test/mcp",
            headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),),
        )
        self.assertEqual(server.headers[0].env_var, "DEMO_TOKEN")
        self.assertFalse(hasattr(server.headers[0], "value"))

    def test_defaults(self):
        server = McpServer("a", command="c")
        self.assertEqual((server.enabled, server.auth, server.cwd), (True, None, None))
        self.assertEqual((server.headers, server.env_vars, server.env), ((), (), ()))

    def test_literal_env_is_sorted_so_equality_is_stable(self):
        one = McpServer("a", command="c", env=(("B_MODE", "x"), ("A_MODE", "y")))
        two = McpServer("a", command="c", env={"A_MODE": "y", "B_MODE": "x"})
        self.assertEqual(one.env, (("A_MODE", "y"), ("B_MODE", "x")))
        self.assertEqual(one, two)
        self.assertEqual(hash(one), hash(two))

    def test_a_secret_looking_value_is_rejected(self):
        for name, value in (
            ("DEMO_API_KEY", "plain-literal"),  # secret-sounding name, not a reference
            ("DEMO_NOTE", "ghp_" + "a1" * 12),  # a known token shape
            ("DEMO_URL", "https://demo.example.test/x?token=abcdef123456"),
        ):
            with self.assertRaises(ValueError, msg=name):
                McpServer("a", command="c", env=((name, value),))

    def test_a_reference_to_a_variable_is_allowed_for_a_secret_sounding_name(self):
        server = McpServer("a", command="c", env=(("DEMO_API_KEY", "${DEMO_API_KEY}"),))
        self.assertEqual(server.env, (("DEMO_API_KEY", "${DEMO_API_KEY}"),))

    def test_a_name_in_both_env_and_env_vars_is_rejected(self):
        with self.assertRaises(ValueError):
            McpServer("a", command="c", env_vars=("DEMO_MODE",), env=(("DEMO_MODE", "test"),))

    def test_a_repeated_env_name_is_rejected(self):
        with self.assertRaises(ValueError):
            McpServer("a", command="c", env=(("DEMO_MODE", "1"), ("DEMO_MODE", "2")))

    def test_env_value_looks_secret_check(self):
        from one_setup_all_agents._core.mask import env_value_looks_secret

        self.assertFalse(env_value_looks_secret("DEMO_MODE", "test"))
        self.assertTrue(env_value_looks_secret("DEMO_TOKEN", "abc"))


# -- 3. the registered adapters -------------------------------------------------------------------

#: Optional: adapter id -> function(env) that creates the user's own native config in the fake
#: home. Needed only when an adapter's fixture should start non-empty.
REGISTERED_SEEDS = {
    "opencode": seed_opencode_home,
    "claude": seed_claude_home,
    "codex": seed_codex_home,
    "agy": seed_antigravity_home,
}

#: Optional: adapter id -> function() returning extra members for its contract class (the hooks
#: `make_env` and `make_cli` of `AdapterContract`). Needed by an adapter that plans `RunCli` ops.
REGISTERED_EXTRAS = {"claude": claude_namespace, "agy": antigravity_namespace}


def contract_case_for(adapter_cls):
    """A test class for one registered adapter: the full contract suite."""
    name = f"Contract_{adapter_cls.__name__}"
    seed = REGISTERED_SEEDS.get(adapter_cls.id)
    namespace = {
        "make_adapter": lambda self: adapter_cls(),
        "seed_home": lambda self, env: seed(env) if seed else None,
    }
    extras = REGISTERED_EXTRAS.get(adapter_cls.id)
    if extras:
        namespace.update(extras())
    return type(name, (AdapterContract, unittest.TestCase), namespace)


for _cls in ADAPTERS:
    _case = contract_case_for(_cls)
    _case.__module__ = __name__
    globals()[_case.__name__] = _case
del _cls, _case


class RegistryCoverageTests(unittest.TestCase):
    def test_every_registered_adapter_has_a_contract_class(self):
        for cls in ADAPTERS:
            self.assertIn(f"Contract_{cls.__name__}", globals(), cls.id)

    def test_every_registered_adapter_gets_the_full_suite(self):
        for cls in ADAPTERS:
            self.assertTrue(issubclass(globals()[f"Contract_{cls.__name__}"], AdapterContract))
        self.assertTrue(issubclass(contract_case_for(ExampleAdapter), AdapterContract))

    def test_the_helpers_use_a_temporary_home_only(self):
        case = ExampleSpecificTests("test_normalize_mcp_drops_exactly_what_the_format_cannot_store")
        h = Harness(case, True)  # never run as a test, so its cleanup is done by hand below
        temp = Path(tempfile.gettempdir()).resolve()
        for directory in (h.env.home, h.env.config_dir, h.env.state_dir):
            self.assertTrue(is_under(directory, temp), f"{directory} is not in the temp dir")
        self.assertIsInstance(h.env, Env)
        shutil.rmtree(h.root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
