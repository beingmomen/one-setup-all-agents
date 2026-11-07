"""engine/plan.py: build_plan merges the adapters' ops; render_plan shows them with secrets masked."""

import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401  (adds src/ to sys.path)
from engine_support import EngineCase, can_make_dir_link
from example_adapter import ExampleAdapter, ExampleCliAdapter, FakeExampleCli
from one_setup_all_agents.engine import journal
from one_setup_all_agents.engine.apply import apply_plan
from one_setup_all_agents.engine.plan import (
    DesiredState,
    _norm,
    build_plan,
    merge_links,
    render_plan,
)
from one_setup_all_agents.engine.revert import revert_operation
from one_setup_all_agents.model import (
    INSTRUCTIONS_KEY,
    Conflict,
    EditFile,
    HeaderRef,
    ItemRef,
    Kind,
    Link,
    McpServer,
    Plan,
    RunCli,
    SkillRef,
    Unsupported,
    WantedLink,
)

SERVER = McpServer(
    "demo-server", command="demo-cmd", args=("--flag",), env=(("DEMO_MODE", "test"),)
)


class SharedSkillsAdapter(ExampleAdapter):
    """A second tool that reads the same skills folder as `example` (the shared agents folder)."""

    id = "shared"
    label = "Shared"

    def paths(self, env, scope):
        base = super().paths(env, scope)
        return replace(base, skills_dir=env.home / ".example" / "skills")


class PlanCase(EngineCase):
    def skill(self, name):
        folder = self.env.config_dir / "skills" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\n", "utf-8")
        return SkillRef(name, folder)

    def desired(self):
        return DesiredState(
            instructions="# Demo\n", mcp={"demo-server": SERVER}, skills=[self.skill("demo-skill")]
        )


class BuildPlanTests(PlanCase):
    def test_the_ops_of_every_kind_are_collected_in_a_fixed_order(self):
        plan = build_plan(self.env, [ExampleAdapter()], self.desired())
        kinds = [
            op.claims[0].kind if not isinstance(op, Unsupported) else op.kind
            for op in plan.ops
            if isinstance(op, (EditFile, Link, Unsupported))
        ]
        self.assertEqual(
            kinds,
            [Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS, Kind.HOOKS, Kind.PERMISSIONS, Kind.MEMORY],
        )
        self.assertEqual(len(plan.executable), 3)

    def test_unsupported_kinds_are_in_the_plan_and_do_not_make_it_non_empty(self):
        plan = build_plan(self.env, [ExampleAdapter()], DesiredState())
        self.assertEqual({op.kind for op in plan.ops}, {Kind.HOOKS, Kind.PERMISSIONS, Kind.MEMORY})
        self.assertTrue(plan.is_empty)

    def test_a_core_kind_that_is_none_is_not_managed_and_not_asked(self):
        plan = build_plan(self.env, [ExampleAdapter()], DesiredState(instructions="# Demo\n"))
        self.assertEqual(len(plan.executable), 1)

    def test_kinds_limit_what_is_asked(self):
        plan = build_plan(self.env, [ExampleAdapter()], self.desired(), kinds=[Kind.MCP])
        self.assertEqual([type(op) for op in plan.ops], [EditFile])

    def test_owned_comes_from_the_ledger_per_tool_and_kind(self):
        adapter = ExampleAdapter()
        apply_plan(self.env, build_plan(self.env, [adapter], self.desired()), label="first")
        self.assertEqual(journal.owned_for(self.state, "example", "mcp"), {"demo-server"})
        # the source no longer has the server: the ledger says it is ours, so it is removed
        gone = replace(self.desired(), mcp={})
        plan = build_plan(self.env, [adapter], gone)
        edit = next(op for op in plan.executable if op.releases)
        self.assertEqual(edit.releases, (ItemRef("example", Kind.MCP, "demo-server"),))
        # with the ledger emptied, the same server is the user's own: left alone, not removed
        journal.set_owned(self.state, "example", "mcp", [])
        self.assertEqual(
            [op for op in build_plan(self.env, [adapter], gone).executable if op.releases], []
        )
        # the instructions are owned under their fixed key
        self.assertEqual(
            journal.owned_for(self.state, "example", "instructions"), {INSTRUCTIONS_KEY}
        )

    def test_a_second_plan_after_the_apply_is_empty(self):
        adapter = ExampleAdapter()
        apply_plan(self.env, build_plan(self.env, [adapter], self.desired()), label="x")
        self.assertTrue(build_plan(self.env, [adapter], self.desired()).is_empty)

    def test_planning_writes_nothing(self):
        desired = self.desired()  # makes the skill source folder
        before = self.listing(self.root)
        build_plan(self.env, [ExampleAdapter(), ExampleCliAdapter()], desired)
        self.assertEqual(self.listing(self.root), before)
        self.assertFalse(self.state.exists())

    def test_a_mapping_gives_each_tool_its_own_desired_state(self):
        adapters = [ExampleAdapter(), ExampleCliAdapter()]
        plan = build_plan(
            self.env,
            adapters,
            {"example": self.desired(), "example-cli": DesiredState(instructions="# Only this\n")},
        )
        tools = [(op.claims[0].tool, op.claims[0].kind) for op in plan.executable]
        self.assertEqual(
            tools,
            [
                ("example", Kind.INSTRUCTIONS),
                ("example", Kind.MCP),
                ("example", Kind.SKILLS),
                ("example-cli", Kind.INSTRUCTIONS),
            ],
        )
        plan = build_plan(self.env, adapters, {"example": self.desired()})
        self.assertEqual({op.claims[0].tool for op in plan.executable}, {"example"})

    def test_run_cli_ops_of_a_cli_adapter_are_planned_and_applied_through_the_fake(self):
        adapter = ExampleCliAdapter()
        cli = FakeExampleCli(self.env.home)
        plan = build_plan(self.env, [adapter], DesiredState(mcp={"demo-server": SERVER}))
        self.assertTrue(any(isinstance(op, RunCli) for op in plan.ops))
        apply_plan(self.env, plan, label="x", run_cli=cli)
        self.assertEqual(len(cli.calls), 1)
        self.assertTrue(
            build_plan(self.env, [adapter], DesiredState(mcp={"demo-server": SERVER})).is_empty
        )


class SharedLinkTests(PlanCase):
    def setUp(self):
        super().setUp()
        self.skills = self.env.home / ".example" / "skills"

    def plan(self, desired):
        return build_plan(self.env, [ExampleAdapter(), SharedSkillsAdapter()], desired)

    def test_identical_links_from_two_adapters_become_one_op_with_both_owners(self):
        plan = self.plan(DesiredState(skills=[self.skill("demo-skill")]))
        links = [op for op in plan.executable if isinstance(op, Link)]
        self.assertEqual(len(links), 1)
        self.assertEqual(
            {(r.tool, r.key) for r in links[0].claims},
            {("example", "demo-skill"), ("shared", "demo-skill")},
        )

    def test_applying_the_merged_plan_records_both_tools_as_owners(self):
        if not can_make_dir_link(self.root):
            self.skipTest("this machine cannot create a directory link")
        apply_plan(self.env, self.plan(DesiredState(skills=[self.skill("demo-skill")])), label="x")
        self.assertEqual(journal.owned_for(self.state, "example", "skills"), {"demo-skill"})
        self.assertEqual(journal.owned_for(self.state, "shared", "skills"), {"demo-skill"})

    def test_removals_of_the_same_link_are_merged_too(self):
        ref_a, ref_b = (ItemRef(t, Kind.SKILLS, "s") for t in ("example", "shared"))
        dest = self.skills / "s"
        merged = merge_links(
            [Link(dest, None, "a", releases=(ref_a,)), Link(dest, None, "b", releases=(ref_b,))]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].releases), {ref_a, ref_b})

    def test_one_tool_withdrawing_while_another_still_wants_the_link_keeps_the_link(self):
        ref_a, ref_b = (ItemRef(t, Kind.SKILLS, "s") for t in ("example", "shared"))
        dest, src = self.skills / "s", self.home / "src"
        for first, second in (
            (Link(dest, None, "gone", releases=(ref_a,)), Link(dest, src, "keep", claims=(ref_b,))),
            (Link(dest, src, "keep", claims=(ref_b,)), Link(dest, None, "gone", releases=(ref_a,))),
        ):
            merged = merge_links([first, second])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0].target, src)
            self.assertEqual(merged[0].claims, (ref_b,))
            self.assertEqual(merged[0].releases, (ref_a,))

    def test_different_targets_for_one_path_keep_the_first_and_report_a_conflict(self):
        ref_a, ref_b = (ItemRef(t, Kind.SKILLS, "s") for t in ("example", "shared"))
        dest = self.skills / "s"
        one, two = self.home / "one", self.home / "two"
        merged = merge_links(
            [Link(dest, one, "a", claims=(ref_a,)), Link(dest, two, "b", claims=(ref_b,))]
        )
        self.assertEqual([type(op) for op in merged], [Link, Conflict])
        self.assertEqual(merged[0].target, one)
        self.assertEqual((merged[1].tool, merged[1].key, merged[1].path), ("shared", "s", dest))

    def test_the_fallback_flag_survives_only_if_every_adapter_allows_it(self):
        dest, src = self.skills / "s", self.home / "src"
        both = merge_links(
            [
                Link(dest, src, "a", allow_copy_fallback=True),
                Link(dest, src, "b", allow_copy_fallback=True),
            ]
        )
        one = merge_links([Link(dest, src, "a", allow_copy_fallback=True), Link(dest, src, "b")])
        self.assertTrue(both[0].allow_copy_fallback)
        self.assertFalse(one[0].allow_copy_fallback)

    def test_links_with_different_paths_are_not_merged(self):
        src = self.home / "src"
        merged = merge_links([Link(self.skills / "a", src, "a"), Link(self.skills / "b", src, "b")])
        self.assertEqual(len(merged), 2)


class WithdrawnSharedLinkTests(PlanCase):
    """Tool A withdraws a skill, tool B still wants it and plans nothing (the link is as wanted).

    The removal must not delete the shared link; A stops owning it and B owns it (ADR 0012).
    """

    def setUp(self):
        super().setUp()
        if not can_make_dir_link(self.root):
            self.skipTest("this machine cannot create a directory link")
        self.dest = self.home / ".example" / "skills" / "demo-skill"
        self.skill_ref = self.skill("demo-skill")

    def plan(self, example_skills, shared_skills):
        return build_plan(
            self.env,
            [ExampleAdapter(), SharedSkillsAdapter()],
            {
                "example": DesiredState(skills=example_skills),
                "shared": DesiredState(skills=shared_skills),
            },
        )

    def owners(self):
        return {
            tool: journal.owned_for(self.state, tool, "skills") for tool in ("example", "shared")
        }

    def sync_both(self):
        apply_plan(self.env, self.plan([self.skill_ref], [self.skill_ref]), label="both")
        self.assertEqual(self.owners(), {"example": {"demo-skill"}, "shared": {"demo-skill"}})

    def test_the_removal_becomes_a_release_only_op_and_the_link_survives(self):
        self.sync_both()
        plan = self.plan([], [self.skill_ref])
        (op,) = plan.executable
        self.assertIsInstance(op, Link)
        self.assertEqual(op.target, self.skill_ref.path)
        self.assertEqual(op.releases, (ItemRef("example", Kind.SKILLS, "demo-skill"),))
        self.assertEqual(op.claims, (ItemRef("shared", Kind.SKILLS, "demo-skill"),))
        apply_plan(self.env, plan, label="withdraw")
        self.assertTrue(self.dest.is_dir(), "the shared link was deleted under the other tool")
        self.assertEqual(self.owners(), {"example": set(), "shared": {"demo-skill"}})
        self.assertTrue(self.plan([], [self.skill_ref]).is_empty)

    def test_the_release_is_journaled_and_undone(self):
        self.sync_both()
        before = self.listing()
        op_id = apply_plan(self.env, self.plan([], [self.skill_ref]), label="withdraw")
        self.assertIsNotNone(op_id, "the ownership change must be journaled")
        self.assertEqual(self.owners(), {"example": set(), "shared": {"demo-skill"}})
        revert_operation(self.env, op_id)
        self.assertEqual(self.owners(), {"example": {"demo-skill"}, "shared": {"demo-skill"}})
        self.assertEqual(self.listing(), before)

    def test_a_tool_that_was_equal_but_unowned_gains_ownership_and_undo_takes_it_back(self):
        apply_plan(
            self.env, build_plan(self.env, [ExampleAdapter()], self.desired_with()), label="a"
        )
        self.assertEqual(self.owners(), {"example": {"demo-skill"}, "shared": set()})
        before = self.listing()
        op_id = apply_plan(self.env, self.plan([], [self.skill_ref]), label="withdraw")
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(self.owners(), {"example": set(), "shared": {"demo-skill"}})
        revert_operation(self.env, op_id)
        self.assertEqual(self.owners(), {"example": {"demo-skill"}, "shared": set()})
        self.assertEqual(self.listing(), before)

    def desired_with(self):
        return DesiredState(skills=[self.skill_ref])

    def test_when_every_owner_withdraws_the_link_is_removed(self):
        self.sync_both()
        plan = self.plan([], [])
        (op,) = plan.executable
        self.assertIsNone(op.target)
        self.assertEqual({r.tool for r in op.releases}, {"example", "shared"})
        apply_plan(self.env, plan, label="all gone")
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.owners(), {"example": set(), "shared": set()})

    def test_a_tool_that_is_not_in_the_build_does_not_keep_the_link(self):
        self.sync_both()
        plan = build_plan(
            self.env, [ExampleAdapter()], {"example": DesiredState(skills=[])}
        )  # `shared` is not asked: it cannot keep anything
        (op,) = plan.executable
        self.assertIsNone(op.target)

    def test_a_tool_that_does_not_manage_skills_in_this_run_does_not_keep_the_link(self):
        self.sync_both()
        plan = build_plan(
            self.env,
            [ExampleAdapter(), SharedSkillsAdapter()],
            {"example": DesiredState(skills=[]), "shared": DesiredState(mcp={})},
        )
        (op,) = [op for op in plan.executable if isinstance(op, Link)]
        self.assertIsNone(op.target)

    def test_a_link_that_does_not_point_where_the_other_tool_wants_is_not_kept_for_it(self):
        self.sync_both()
        other = self.skill("elsewhere").path
        wish = WantedLink(self.dest, other, ItemRef("shared", Kind.SKILLS, "demo-skill"))
        removal = Link(
            self.dest, None, "gone", releases=(ItemRef("example", Kind.SKILLS, "demo-skill"),)
        )
        merged = merge_links([removal], {_norm(self.dest): [wish]})
        self.assertIsNone(merged[0].target, "the link does not give what the other tool wants")

    def test_the_wanting_tool_must_be_another_one(self):
        self.sync_both()
        ref = ItemRef("example", Kind.SKILLS, "demo-skill")
        removal = Link(self.dest, None, "gone", releases=(ref,))
        own_wish = {_norm(self.dest): [WantedLink(self.dest, self.skill_ref.path, ref)]}
        self.assertIsNone(merge_links([removal], own_wish)[0].target)

    def test_a_removal_without_wanted_links_is_unchanged(self):
        ref = ItemRef("example", Kind.SKILLS, "demo-skill")
        removal = Link(self.dest, None, "gone", releases=(ref,))
        self.assertEqual(merge_links([removal]), [removal])
        self.assertEqual(merge_links([removal], {}), [removal])

    def test_the_kept_link_is_shown_in_the_dry_run(self):
        self.sync_both()
        text = render_plan(self.plan([], [self.skill_ref]))
        self.assertIn("keep the shared link", text)
        self.assertIn("shared", text)


class RealToolsSharedSkillsTests(PlanCase):
    """The real OpenCode and Codex adapters both link into one temporary ~/.agents/skills."""

    def setUp(self):
        super().setUp()
        if not can_make_dir_link(self.root):
            self.skipTest("this machine cannot create a directory link")
        from one_setup_all_agents.adapters.codex import CodexAdapter
        from one_setup_all_agents.adapters.opencode import OpenCodeAdapter

        self.adapters = [OpenCodeAdapter(), CodexAdapter()]
        self.skill_a, self.skill_b = self.skill("demo-skill-a"), self.skill("demo-skill-b")
        self.shared = self.home / ".agents" / "skills"

    def plan(self, opencode, codex):
        return build_plan(
            self.env,
            self.adapters,
            {"opencode": DesiredState(skills=opencode), "codex": DesiredState(skills=codex)},
        )

    def owners(self):
        return {t: journal.owned_for(self.state, t, "skills") for t in ("opencode", "codex")}

    def test_one_tool_withdrawing_a_skill_leaves_the_link_to_the_other_and_undo_restores_both(self):
        both = [self.skill_a, self.skill_b]
        apply_plan(self.env, self.plan(both, both), label="both")
        both_owned = self.owners()
        self.assertEqual(both_owned["opencode"], {"demo-skill-a", "demo-skill-b"})
        self.assertEqual(both_owned["codex"], {"demo-skill-a", "demo-skill-b"})
        before = self.listing()

        # OpenCode drops skill b; Codex keeps both: the shared link of b must survive.
        plan = self.plan([self.skill_a], both)
        (op,) = plan.executable
        self.assertEqual(op.link, self.shared / "demo-skill-b")
        self.assertIsNotNone(op.target)
        op_id = apply_plan(self.env, plan, label="opencode drops b")
        self.assertTrue((self.shared / "demo-skill-b" / "SKILL.md").is_file())
        self.assertEqual(self.owners()["opencode"], {"demo-skill-a"})
        self.assertEqual(self.owners()["codex"], {"demo-skill-a", "demo-skill-b"})
        self.assertTrue(self.plan([self.skill_a], both).is_empty)

        # now Codex drops b too: the last owner is gone, so the link goes
        last = apply_plan(
            self.env, self.plan([self.skill_a], [self.skill_a]), label="codex drops b"
        )
        self.assertFalse((self.shared / "demo-skill-b").exists())
        self.assertTrue((self.shared / "demo-skill-a").is_dir())

        revert_operation(self.env, last)  # newest first
        self.assertTrue((self.shared / "demo-skill-b").is_dir())
        revert_operation(self.env, op_id)
        self.assertEqual(self.owners(), both_owned)
        self.assertEqual(self.listing(), before)

    def test_a_tool_that_never_made_the_link_gains_it_when_the_owner_withdraws(self):
        apply_plan(self.env, self.plan([self.skill_a], None), label="opencode only")
        self.assertEqual(self.owners(), {"opencode": {"demo-skill-a"}, "codex": set()})
        plan = self.plan([], [self.skill_a])  # the same skill moves from one tool to the other
        apply_plan(self.env, plan, label="moves")
        self.assertTrue((self.shared / "demo-skill-a").is_dir())
        self.assertEqual(self.owners(), {"opencode": set(), "codex": {"demo-skill-a"}})
        self.assertTrue(self.plan([], [self.skill_a]).is_empty)


class RenderTests(PlanCase):
    SECRETS = ["FAKE-ENV-SECRET-aaa111", "FAKE-ARG-SECRET-bbb222", "FAKE-URL-SECRET-ccc333"]

    def test_an_empty_plan_says_there_is_nothing_to_do(self):
        text = render_plan(Plan())
        self.assertIn("0 change(s)", text)
        self.assertIn("Nothing to do.", text)
        self.assertIn(
            "Nothing to do.", render_plan(Plan([Unsupported("example", Kind.HOOKS, "r")]))
        )

    def test_each_kind_of_entry_is_shown(self):
        plan = Plan(
            [
                EditFile(
                    Path("/x/a.json"),
                    lambda old: b"",
                    "abc",
                    "update the MCP servers",
                    sensitive=True,
                ),
                EditFile(Path("/x/new.md"), lambda old: b"", None, "write the instructions"),
                Link(Path("/x/skills/s"), Path("/src/s"), "link skill s", allow_copy_fallback=True),
                Link(Path("/x/skills/t"), None, "withdraw skill t"),
                RunCli(
                    ["tool", "mcp", "add", "demo-server"],
                    ["tool", "mcp", "remove", "demo-server"],
                    "add",
                ),
                Conflict(
                    "example",
                    Kind.MCP,
                    "demo-server",
                    "exists and was not created here",
                    Path("/x/a.json"),
                ),
                Unsupported("example", Kind.HOOKS, "hooks are not supported by this adapter yet"),
            ]
        )
        text = render_plan(plan).replace("\\", "/")  # Windows paths use backslashes
        for expected in (
            "Plan: 5 change(s), 1 conflict(s), 1 unsupported",
            "edit file /x/a.json [sensitive]: update the MCP servers",
            "create file /x/new.md: write the instructions",
            "link /x/skills/s -> /src/s (copy if linking fails)",
            "remove link /x/skills/t",
            "run tool mcp add demo-server",
            "inverse: tool mcp remove demo-server",
            "Conflicts (left alone, not changed):",
            "example mcp 'demo-server' at /x/a.json: exists and was not created here",
            "example hooks: hooks are not supported by this adapter yet",
        ):
            self.assertIn(expected, text)

    def test_file_contents_are_never_shown(self):
        plan = Plan([EditFile(Path("/x/a"), lambda old: b"CONTENT-NEVER-SHOWN", None, "write a")])
        self.assertNotIn("CONTENT-NEVER-SHOWN", render_plan(plan))

    def test_secrets_in_commands_descriptions_and_conflicts_are_masked(self):
        env_secret, arg_secret, url_secret = self.SECRETS
        blob = (
            '{"command": "npx", "args": ["-y", "pkg", "--token", "%s"], "env": {"DEMO_API_KEY": "%s"}}'
            % (arg_secret, env_secret)
        )
        plan = Plan(
            [
                RunCli(
                    [
                        "tool",
                        "add",
                        "--token",
                        arg_secret,
                        "--url",
                        f"https://h.example.test/x?token={url_secret}",
                    ],
                    ["tool", "remove", "--api-key", env_secret],
                    f"add with token={arg_secret}",
                ),
                RunCli(
                    ["tool", "add-json", "demo-server", blob],
                    ["tool", "remove", "demo-server"],
                    "add json",
                ),
                EditFile(Path("/x/a"), lambda old: b"", None, f"set password={env_secret}"),
                Link(Path("/x/l"), Path("/src"), f"link with secret={url_secret}"),
                Conflict("example", Kind.MCP, "demo-server", f"differs; token={url_secret}"),
            ]
        )
        text = render_plan(plan)
        for secret in self.SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn("***", text)
        self.assertIn("tool add-json demo-server", text)  # still reviewable

    def test_a_server_with_a_header_reference_shows_the_variable_name_only(self):
        server = McpServer(
            "demo-remote",
            url="https://demo.example.test/mcp",
            headers=(HeaderRef("Authorization", "DEMO_TOKEN", "Bearer"),),
        )
        plan = build_plan(
            self.env, [ExampleCliAdapter()], DesiredState(mcp={"demo-remote": server})
        )
        text = render_plan(plan)
        self.assertIn("DEMO_TOKEN", text)
        self.assertIn("demo-remote", text)

    def test_a_real_plan_renders_without_touching_the_disk(self):
        desired = self.desired()
        before = self.listing(self.root)
        plan = build_plan(self.env, [ExampleAdapter()], desired)
        text = render_plan(plan)
        self.assertIn("Plan: 3 change(s), 0 conflict(s), 3 unsupported", text)
        self.assertEqual(self.listing(self.root), before)


if __name__ == "__main__":
    unittest.main()
