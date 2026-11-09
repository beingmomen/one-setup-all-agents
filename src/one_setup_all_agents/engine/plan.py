"""Build a reviewable plan from the adapters' ops, and render it as text.

`build_plan` is the engine's half of planning: it asks every adapter, for every kind, with the
`owned` keys the ledger records for that tool and kind, then merges the results (docs/decisions/
0008, 0009, 0010). It reads the ledger and the adapters' own reads; it writes nothing.

The source of truth (the config directory) is NOT read here. Whoever calls `build_plan` loads
the desired state and passes it in as `DesiredState` (one for all tools, or a mapping of tool id
to `DesiredState` when tools get different servers or skills). The loader is a later plan item.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .._core.mask import ENV_REF, MASK, SECRET_NAME, mask_args, mask_text
from ..adapters.base import ToolAdapter
from ..model import (
    Conflict,
    EditFile,
    Env,
    ItemRef,
    Kind,
    Link,
    McpServer,
    Op,
    Plan,
    RunCli,
    Scope,
    SkillRef,
    Unsupported,
    WantedLink,
)
from . import backup, fsops, journal
from .errors import BackupError


@dataclass(frozen=True)
class DesiredState:
    """The neutral desired state, per kind (docs/decisions/0009).

    `None` for a core kind (instructions, mcp, skills) means "not managed in this run": the kind
    is skipped. The optional kinds (hooks, permissions, memory) are always asked, so that a tool
    that cannot express them shows an `Unsupported` entry in the plan.
    """

    instructions: str | None = None
    mcp: Mapping[str, McpServer] | None = None
    skills: Sequence[SkillRef] | None = None
    hooks: object | None = None
    permissions: object | None = None
    memory: object | None = None


#: The order in which kinds are planned for each adapter.
KIND_ORDER = (
    Kind.INSTRUCTIONS,
    Kind.MCP,
    Kind.SKILLS,
    Kind.HOOKS,
    Kind.PERMISSIONS,
    Kind.MEMORY,
)
_CORE_KINDS = (Kind.INSTRUCTIONS, Kind.MCP, Kind.SKILLS)


def _plan_kind(
    adapter: ToolAdapter,
    env: Env,
    scope: Scope,
    kind: Kind,
    desired: DesiredState,
    owned: frozenset[str],
) -> list[Op]:
    if kind is Kind.INSTRUCTIONS:
        return adapter.plan_instructions(env, scope, desired.instructions, owned)
    if kind is Kind.MCP:
        return adapter.plan_mcp(env, scope, desired.mcp, owned)
    if kind is Kind.SKILLS:
        return adapter.plan_skills(env, scope, desired.skills, owned)
    method = {
        Kind.HOOKS: (adapter.plan_hooks, desired.hooks),
        Kind.PERMISSIONS: (adapter.plan_permissions, desired.permissions),
        Kind.MEMORY: (adapter.plan_memory, desired.memory),
    }[kind]
    return method[0](env, scope, method[1], owned)


def build_plan(
    env: Env,
    adapters: Iterable[ToolAdapter],
    desired: DesiredState | Mapping[str, DesiredState],
    *,
    scope: Scope = Scope.GLOBAL,
    kinds: Iterable[Kind] | None = None,
) -> Plan:
    """Ask each adapter to plan `desired` and merge the results into one `Plan`.

    `desired` is one `DesiredState` for every adapter, or a mapping of adapter id to its own
    (an adapter missing from the mapping gets nothing planned for the core kinds). `kinds`
    limits the kinds asked (default: all, in `KIND_ORDER`). Ops are ordered adapter by adapter,
    kind by kind. Identical links from several adapters are merged (`merge_links`).

    Pure: reads the ledger and the tools' current state, writes nothing. A second call right
    after a successful apply returns an empty plan when there are no conflicts (idempotence is
    part of the adapter contract).
    """
    wanted = tuple(kinds) if kinds is not None else KIND_ORDER
    ops: list[Op] = []
    wanted_links: dict[str, list[WantedLink]] = {}
    for adapter in adapters:
        if isinstance(desired, Mapping):
            state = desired.get(adapter.id, DesiredState())
        else:
            state = desired
        for kind in KIND_ORDER:
            if kind not in wanted:
                continue
            if kind in _CORE_KINDS and getattr(state, kind.value) is None:
                continue
            owned = journal.owned_for(env.state_dir, adapter.id, kind.value)
            planned_ops = _plan_kind(adapter, env, scope, kind, state, owned)
            for op in planned_ops:
                if isinstance(op, RunCli):
                    argv_list = list(op.argv)
                    inv_list = list(op.inverse_argv)
                    if mask_args(argv_list) != argv_list or mask_args(inv_list) != inv_list:
                        ref = next(iter(op.claims + op.releases), None)
                        ops.append(
                            Conflict(
                                tool=ref.tool if ref else adapter.id,
                                kind=ref.kind if ref else kind,
                                key=ref.key if ref else mask_text(op.description),
                                reason="command looks like a secret",
                            )
                        )
                        continue
                ops.append(op)
            if kind is Kind.SKILLS:
                for want in adapter.wanted_links(env, scope, state.skills):
                    wanted_links.setdefault(_norm(want.link), []).append(want)
    return Plan(merge_links(ops, wanted_links))


# -- merging identical links across adapters (docs/decisions/0007) ------------------------------


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _union(a: tuple[ItemRef, ...], b: tuple[ItemRef, ...]) -> tuple[ItemRef, ...]:
    return a + tuple(r for r in b if r not in a)


def _first_ref(op: Link) -> ItemRef | None:
    return next(iter(op.claims + op.releases), None)


def _in_wanted_state(link: Path, target: Path) -> bool:
    """Is `link` a link (or an unedited marked copy) that already gives what `target` wants?"""
    try:
        current = backup.describe(link)
    except (BackupError, OSError):
        return False
    if current["state"] == "dir_link":
        return fsops.same_target(current["target"], target)
    if current["state"] == "copy_tree":
        return current["tree_sha256"] == current["marker"]["tree_sha256"] and fsops.same_target(
            current["marker"]["source"], target
        )
    return False


def _keep_if_still_wanted(removal: Link, wanting: Sequence[WantedLink]) -> Link:
    """A removal of a link that another tool still wants, as it is, becomes a release-only op.

    The op is a `Link` to the target the other tool wants; the path is already in that state, so
    the engine changes nothing on disk (docs/decisions/0010, "already as wanted") but journals the
    op, which moves the ownership: the withdrawing tool's `releases` and the remaining tools'
    `claims` (docs/decisions/0012). Nothing changes when no other tool wants the path, or the path
    is not in the state it wants (that tool then plans its own relink, which `merge_links` keeps).
    """
    leaving = {ref.tool for ref in removal.releases}
    keepers = [
        w
        for w in wanting
        if w.ref.tool not in leaving
        and _norm(w.link) == _norm(removal.link)
        and _in_wanted_state(removal.link, w.target)
    ]
    if not keepers:
        return removal
    tools = ", ".join(sorted({w.ref.tool for w in keepers}))
    return Link(
        link=removal.link,
        target=keepers[0].target,
        description=f"keep the shared link, still wanted by {tools} ({removal.description})",
        allow_copy_fallback=all(w.allow_copy_fallback for w in keepers),
        claims=tuple(dict.fromkeys(w.ref for w in keepers)),
        releases=removal.releases,
    )


def merge_links(
    ops: list[Op], wanted_links: Mapping[str, Sequence[WantedLink]] | None = None
) -> list[Op]:
    """Collapse `Link` ops that name the same link path, so a shared folder is linked once.

    - Same target (or both removals): one op, claims and releases of all adapters joined, so the
      ledger records every tool as an owner. `allow_copy_fallback` stays on only if all allow it.
    - One removes, one links: the link is kept (another tool still wants it); the removal's
      `releases` are added to the kept op so the withdrawing tool stops owning the item.
    - Different targets: the first stays, the later one becomes a `Conflict` and is not run.
    - `wanted_links` (normalized link path -> what the adapters want there, op or no op): a removal
      that is left over after merging is dropped when another tool still wants that path and the
      path is already as that tool wants it. The removal's `releases` stay (the withdrawing tool
      stops owning the item) and the other tools claim it (`_keep_if_still_wanted`). This is the
      case the first rule cannot see: the other tool plans nothing because nothing is to be done.
    """
    out: list[Op] = []
    at: dict[str, int] = {}
    for op in ops:
        if not isinstance(op, Link):
            out.append(op)
            continue
        key = _norm(op.link)
        if key not in at:
            at[key] = len(out)
            out.append(op)
            continue
        first = out[at[key]]
        first_removes, this_removes = first.target is None, op.target is None
        if (first_removes and this_removes) or (
            not first_removes and not this_removes and fsops.same_target(first.target, op.target)
        ):
            out[at[key]] = replace(
                first,
                claims=_union(first.claims, op.claims),
                releases=_union(first.releases, op.releases),
                allow_copy_fallback=first.allow_copy_fallback and op.allow_copy_fallback,
            )
            continue
        if first_removes != this_removes:
            keep, removal = (op, first) if first_removes else (first, op)
            out[at[key]] = replace(keep, releases=_union(keep.releases, removal.releases))
            continue
        ref = _first_ref(op)
        out.append(
            Conflict(
                tool=ref.tool if ref else "engine",
                kind=ref.kind if ref else Kind.SKILLS,
                key=ref.key if ref else os.path.basename(str(op.link)),
                reason=(
                    "two tools want different targets for the same link path; "
                    "the first one is kept and this one is left alone"
                ),
                path=op.link,
            )
        )
    if wanted_links:
        out = [
            _keep_if_still_wanted(op, wanted_links.get(_norm(op.link), ()))
            if isinstance(op, Link) and op.target is None
            else op
            for op in out
        ]
    return out


# -- rendering -----------------------------------------------------------------------------------


def _mask_json(value, key: str | None = None):
    if isinstance(value, str):
        if key is not None and SECRET_NAME.search(key) and not ENV_REF.match(value.strip()):
            return MASK
        return mask_text(value)
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return mask_args(value)
        return [_mask_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _mask_json(v, k) for k, v in value.items()}
    return value


def _mask_arg(arg: str) -> str:
    """Mask one argv entry; a JSON document is masked field by field first."""
    text = arg.strip()
    if text[:1] in ("{", "["):
        try:
            return mask_text(json.dumps(_mask_json(json.loads(text)), sort_keys=True))
        except ValueError:
            pass
    return mask_text(arg)


def _argv(argv: Sequence[str]) -> str:
    return shlex.join(_mask_arg(a) for a in mask_args(list(argv)))


def _describe(op: Op, number: int) -> str:
    if isinstance(op, EditFile):
        how = "create" if op.expect_sha256 is None else "edit"
        tag = " [sensitive]" if op.sensitive else ""
        return f"{number}. {how} file {op.path}{tag}: {op.description}"
    if isinstance(op, Link):
        if op.target is None:
            return f"{number}. remove link {op.link}: {op.description}"
        fallback = " (copy if linking fails)" if op.allow_copy_fallback else ""
        return f"{number}. link {op.link} -> {op.target}{fallback}: {op.description}"
    if isinstance(op, RunCli):
        return (
            f"{number}. run {_argv(op.argv)}: {op.description}\n"
            f"     inverse: {_argv(op.inverse_argv)}"
        )
    raise TypeError(f"not an executable op: {type(op).__name__}")


def render_plan(plan: Plan) -> str:
    """Render a plan as human-readable text for the dry run. Secrets are masked.

    Shows what would run (in order), then conflicts (left alone) and unsupported kinds. File
    contents are never shown. Every line passes through the mask.
    """
    executable = plan.executable
    conflicts = [op for op in plan.ops if isinstance(op, Conflict)]
    unsupported = [op for op in plan.ops if isinstance(op, Unsupported)]
    lines = [
        f"Plan: {len(executable)} change(s), {len(conflicts)} conflict(s), "
        f"{len(unsupported)} unsupported"
    ]
    if executable:
        lines.append("To run:")
        lines.extend(f"  {_describe(op, i)}" for i, op in enumerate(executable, 1))
    if conflicts:
        lines.append("Conflicts (left alone, not changed):")
        for op in conflicts:
            where = f" at {op.path}" if op.path else ""
            lines.append(f"  - {op.tool} {op.kind.value} '{op.key}'{where}: {op.reason}")
    if unsupported:
        lines.append("Not supported by the tool (shown so the gap is visible):")
        lines.extend(f"  - {op.tool} {op.kind.value}: {op.reason}" for op in unsupported)
    if not (executable or conflicts):
        lines.append("Nothing to do.")
    text = "\n".join(lines)
    return "\n".join(mask_text(line) for line in text.split("\n")) + "\n"
