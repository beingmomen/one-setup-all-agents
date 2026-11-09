"""Import tool configurations and generate initial source files."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ._core.mask import mask_text
from .adapters import ADAPTERS
from .adapters.base import ToolAdapter
from .config_source import (
    INSTRUCTIONS_NAME,
    MCP_NAME,
    SOURCE_CONFIG_NAME,
    ServerEntry,
    ToolSettings,
    render_mcp_source,
    render_source_config,
    server_secret_problems,
)
from .engine import fsops
from .engine.errors import BackupError
from .model import (
    INSTRUCTIONS_KEY,
    Conflict,
    Detection,
    EditFile,
    Env,
    ImportNote,
    ItemRef,
    Kind,
    McpServer,
    Scope,
    SkillRef,
)
from .reader import ToolReading


@dataclass
class InstructionsProposal:
    """Proposed shared instructions configuration and adoption state."""

    core: str
    adopt: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    from_tool: str | None = None


@dataclass
class McpProposal:
    """Proposed MCP servers, server notes, adoption items, and messages."""

    servers: dict[str, ServerEntry] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)
    adopt: list[ItemRef] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    import_notes: list[ImportNote] = field(default_factory=list)


@dataclass
class SkillImport:
    """One skill to be copied into the shared source skills directory."""

    name: str
    source_path: Path
    files: list[tuple[Path, bytes, int | None]] = field(default_factory=list)


@dataclass
class SkillsProposal:
    """Proposed shared skills to copy, adopt, and conflicts/messages."""

    copies: list[SkillImport] = field(default_factory=list)
    adopt: list[ItemRef] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.copies)


@dataclass
class ImportProposal:
    """Proposed source configuration, adoption items, and messages."""

    instructions: InstructionsProposal | None = None
    servers: dict[str, ServerEntry] = field(default_factory=dict)
    server_notes: dict[str, list[str]] = field(default_factory=dict)
    skills: SkillsProposal = field(default_factory=SkillsProposal)
    adopt: list[ItemRef] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    import_notes: list[ImportNote] = field(default_factory=list)


def _normalize_instructions_text(raw: str) -> str:
    text = raw.lstrip("\ufeff").replace("\r\n", "\n").rstrip("\n")
    return (text + "\n") if text else ""


def propose_instructions(
    readings: Sequence[ToolReading],
    *,
    from_tool: str | None = None,
    ask: Callable[[str], str] | None = None,
    env: Env | None = None,
    adapters: Mapping[str, ToolAdapter] | None = None,
) -> InstructionsProposal | None:
    """Propose shared instructions and items to adopt across tool readings."""
    candidates: list[tuple[str, str, str]] = []
    messages: list[str] = []
    for r in readings:
        if r.instructions is not None and getattr(r.instructions, "text", None) is not None:
            text = r.instructions.text
            if "\ufffd" in text or "\x00" in text:
                file_str = (
                    str(r.instructions.path) if hasattr(r.instructions, "path") else "instructions"
                )
                messages.append(
                    f"problem reading {r.tool} instructions: {file_str} "
                    "contains invalid non-UTF-8 characters; skipped"
                )
                continue
            candidates.append((r.tool, r.label, _normalize_instructions_text(text)))

    if not candidates:
        if messages:
            return InstructionsProposal(core="", adopt=[], skipped=[], messages=messages)
        return None

    distinct_texts = {c[2] for c in candidates}

    chosen_from: str | None = None
    if len(distinct_texts) == 1:
        core = candidates[0][2]
        adopt = [c[0] for c in candidates]
        skipped: list[str] = []
        chosen_from = candidates[0][0]
    else:
        if from_tool is not None:
            matched = [c for c in candidates if c[0] == from_tool]
            if not matched:
                raise ValueError(f"tool '{from_tool}' has no instructions to import")
            core = matched[0][2]
            chosen_from = from_tool
            should_replace = False
            if ask is not None:
                ans = ask("replace the other files with the shared text on the next sync? [y/N]: ")
                should_replace = str(ans).strip().lower() in ("y", "yes")

            if should_replace:
                adopt = [c[0] for c in candidates]
                skipped = []
            else:
                adopt = [c[0] for c in candidates if c[2] == core]
                skipped = [c[0] for c in candidates if c[2] != core]
        else:
            if ask is None:
                raise ValueError("instructions differ between tools; pass --from TOOL")

            options: list[str] = []
            for i, (_tool, label, text) in enumerate(candidates, 1):
                lines = len(text.splitlines())
                h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
                options.append(f"{i}. {label} ({lines} lines, sha256:{h})")

            prompt = "Choose tool for shared instructions:\n" + "\n".join(options) + "\nChoice: "
            choice_ans = ask(prompt)
            if not choice_ans:
                return None

            chosen = None
            try:
                idx = int(str(choice_ans).strip()) - 1
                if 0 <= idx < len(candidates):
                    chosen = candidates[idx]
            except (ValueError, TypeError):
                for c in candidates:
                    if str(choice_ans).strip().lower() in (c[0].lower(), c[1].lower()):
                        chosen = c
                        break

            if chosen is None:
                return None

            core = chosen[2]
            chosen_from = chosen[0]
            adopt = [c[0] for c in candidates if c[2] == core]
            skipped = [c[0] for c in candidates if c[2] != core]

    # Dry-run conflict check
    if env is not None and adapters is not None:
        for tool_id in adopt:
            adapter = adapters.get(tool_id)
            if adapter is not None:
                try:
                    ops = adapter.plan_instructions(
                        env, Scope.GLOBAL, core, frozenset({INSTRUCTIONS_KEY})
                    )
                    for op in ops:
                        if isinstance(op, Conflict):
                            messages.append(f"warning: {tool_id}: {op.reason}")
                except Exception as exc:
                    messages.append(f"warning: {tool_id}: {exc}")

    # Secret hint
    if mask_text(core) != core:
        for n, line in enumerate(core.splitlines(), 1):
            if mask_text(line) != line:
                messages.append(
                    f"the instructions text looks like it contains a secret (line {n}); move it out"
                )
                break

    return InstructionsProposal(
        core=core,
        adopt=adopt,
        skipped=skipped,
        messages=messages,
        from_tool=chosen_from,
    )


def propose_mcp(
    readings: Sequence[ToolReading],
    adapters: Mapping[str, ToolAdapter] | Sequence[ToolAdapter],
    env: Env,
    from_tool: str | None = None,
) -> McpProposal:
    """Propose shared MCP servers, adoption items, notes, and difference messages."""
    adapter_map = dict(adapters) if isinstance(adapters, Mapping) else {a.id: a for a in adapters}
    registry_order = [cls.id for cls in ADAPTERS]

    installed_tool_ids = [r.tool for r in readings]
    tool_labels = {r.tool: r.label for r in readings}

    servers_by_name: dict[str, list[tuple[str, McpServer]]] = {}
    for r in readings:
        for sname, srv in r.mcp.items():
            servers_by_name.setdefault(sname, []).append((r.tool, srv))

    proposal_servers: dict[str, ServerEntry] = {}
    proposal_notes: dict[str, list[str]] = {}
    proposal_adopt: list[ItemRef] = []
    messages: list[str] = []
    import_notes: list[ImportNote] = []

    for name in sorted(servers_by_name.keys()):
        entries = servers_by_name[name]

        # Group by distinct McpServer form
        forms: list[tuple[McpServer, list[str]]] = []
        for tool_id, srv in entries:
            matched = False
            for form, tool_list in forms:
                if form == srv:
                    tool_list.append(tool_id)
                    matched = True
                    break
            if not matched:
                forms.append((srv, [tool_id]))

        if len(forms) == 1:
            chosen = forms[0][0]
        else:
            max_count = max(len(tools) for _, tools in forms)
            tied = [pair for pair in forms if len(pair[1]) == max_count]
            if len(tied) == 1:
                chosen = tied[0][0]
                agreeing_tools = tied[0][1]
            else:
                chosen_pair = None
                if from_tool is not None:
                    for pair in tied:
                        if from_tool in pair[1]:
                            chosen_pair = pair
                            break
                if chosen_pair is None:

                    def rank(pair: tuple[McpServer, list[str]]) -> int:
                        ranks = [
                            registry_order.index(tid) if tid in registry_order else 999
                            for tid in pair[1]
                        ]
                        return min(ranks)

                    chosen_pair = min(tied, key=rank)

                chosen = chosen_pair[0]
                agreeing_tools = chosen_pair[1]

            agreeing_labels = [tool_labels.get(tid, tid) for tid in agreeing_tools]
            other_labels = []
            for form, tool_list in forms:
                if form != chosen:
                    other_labels.extend(tool_labels.get(tid, tid) for tid in tool_list)

            diff_msg = (
                f"differs between tools: {name} "
                f"({', '.join(agreeing_labels)} vs {', '.join(other_labels)})"
            )
            messages.append(diff_msg)

        if chosen.startup_timeout_sec is None:
            for _tid, srv in entries:
                if srv.startup_timeout_sec is not None:
                    chosen = dataclasses.replace(
                        chosen, startup_timeout_sec=srv.startup_timeout_sec
                    )
                    break

        if any(not srv.enabled for _, srv in entries):
            chosen = dataclasses.replace(chosen, enabled=False)

        # Check candidate server and entries for literal secret problems
        chosen_probs = server_secret_problems(chosen)
        entry_probs_by_tool: dict[str, list[str]] = {}
        for tool_id, srv in entries:
            p = server_secret_problems(srv)
            if p:
                entry_probs_by_tool[tool_id] = p

        if chosen_probs or entry_probs_by_tool:
            all_probs = list(chosen_probs)
            for p in entry_probs_by_tool.values():
                for item in p:
                    if item not in all_probs:
                        all_probs.append(item)

            for prob in all_probs:
                field_name = "arg" if prob in ("arg", "args") else prob
                for tool_id, _ in entries:
                    if tool_id in entry_probs_by_tool or chosen_probs:
                        adapter = adapter_map.get(tool_id)
                        tool_label = adapter.label if adapter else tool_labels.get(tool_id, tool_id)
                        msg = (
                            f"server {name} in {tool_label}: {field_name} "
                            "had a literal value that was not imported; "
                            "move the value to an environment variable and reference it as ${NAME}"
                        )
                        if msg not in messages:
                            messages.append(msg)
                        import_notes.append(
                            ImportNote(tool=tool_id, server=name, field=field_name, name=field_name)
                        )
            continue

        tools_present = [tool_id for tool_id, _ in entries]
        if len(set(tools_present)) < len(installed_tool_ids):
            tools_selector: tuple[str, ...] | None = tuple(sorted(set(tools_present)))
        else:
            tools_selector = None

        proposal_servers[name] = ServerEntry(server=chosen, tools=tools_selector)

        for tool_id, current_srv in entries:
            adapter = adapter_map.get(tool_id)
            if adapter is not None:
                expected_dict = adapter.normalize_mcp({name: chosen})
                expected_norm = expected_dict.get(name)
                current_dict = adapter.normalize_mcp({name: current_srv})
                current_norm = current_dict.get(name)
                if current_norm is not None and current_norm == expected_norm:
                    proposal_adopt.append(ItemRef(tool_id, Kind.MCP, name))

    for r in readings:
        adapter = adapter_map.get(r.tool)
        if adapter is not None:
            tool_notes = adapter.import_notes(env, Scope.GLOBAL)
            for note in tool_notes:
                import_notes.append(note)
                if note.field == "header":
                    msg = (
                        f"server {note.server} in {adapter.label}: header '{note.name}' "
                        "had a literal value that was not imported; "
                        "reference an environment variable as ${NAME}"
                    )
                    note_comment = (
                        f"{adapter.label}: header '{note.name}' had a literal value that was not "
                        "imported; reference an environment variable as ${NAME}"
                    )
                else:
                    msg = (
                        f"server {note.server} in {adapter.label}: {note.field} {note.name} "
                        "had a literal value that was not imported; "
                        f"set it as an environment variable named {note.name}"
                    )
                    note_comment = (
                        f"{adapter.label}: {note.field} {note.name} had a literal value "
                        "that was not imported; "
                        f"set it as an environment variable named {note.name}"
                    )
                messages.append(msg)
                proposal_notes.setdefault(note.server, []).append(note_comment)

    dropped_servers = {(n.tool, n.server) for n in import_notes}
    proposal_adopt = [
        ref
        for ref in proposal_adopt
        if not (ref.kind == Kind.MCP and (ref.tool, ref.key) in dropped_servers)
    ]

    return McpProposal(
        servers=proposal_servers,
        notes=proposal_notes,
        adopt=proposal_adopt,
        messages=messages,
        import_notes=import_notes,
    )


EXCLUDED_SKILL_NAMES = frozenset({".trash", "synced", ".system"})


@dataclass
class _SkillCandidate:
    tool: str
    label: str
    ref: SkillRef
    real_dir: Path
    tree_hash: str


def _collect_skill_files(real_dir: Path) -> list[tuple[Path, bytes, int | None]]:
    files: list[tuple[Path, bytes, int | None]] = []
    for root_str, dirs, file_names in os.walk(real_dir):
        dirs.sort()
        file_names.sort()
        root_path = Path(root_str)
        for fname in file_names:
            full = root_path / fname
            rel = full.relative_to(real_dir)
            content = full.read_bytes()
            st = full.stat()
            mode = stat.S_IMODE(st.st_mode) if (st.st_mode & 0o111) else None
            files.append((rel, content, mode))
    return files


def propose_skills(
    readings: Sequence[ToolReading],
    adapters: Mapping[str, ToolAdapter] | Sequence[ToolAdapter],
    env: Env,
    *,
    from_tool: str | None = None,
    ask: Callable[[str], str] | None = None,
) -> SkillsProposal:
    """Propose skills to copy into shared source and skills to adopt across tools."""
    proposal = SkillsProposal()
    adapters_by_id = (
        adapters if isinstance(adapters, Mapping) else {getattr(a, "id", ""): a for a in adapters}
    )
    installed_tool_ids = {r.tool for r in readings}
    installed_adapters = {
        tid: adapters_by_id[tid] for tid in installed_tool_ids if tid in adapters_by_id
    }

    # 1. Inventory & validation
    candidates_by_name: dict[str, list[_SkillCandidate]] = {}
    for r in readings:
        for ref in r.skills:
            if ref.name.startswith(".") or ref.name in EXCLUDED_SKILL_NAMES:
                continue

            real_dir = Path(os.path.realpath(ref.path))
            if not real_dir.is_dir() or not (real_dir / "SKILL.md").is_file():
                proposal.messages.append(f"skill {ref.name} has no SKILL.md; skipped")
                continue

            try:
                thash = fsops.tree_hash(real_dir)
            except BackupError as exc:
                msg = str(exc)
                if "is a link" in msg:
                    proposal.messages.append(f"skill {ref.name} contains an internal link; skipped")
                elif "larger than" in msg:
                    proposal.messages.append(f"skill {ref.name} exceeds size limit; skipped")
                elif "more than" in msg:
                    proposal.messages.append(f"skill {ref.name} exceeds file count limit; skipped")
                else:
                    proposal.messages.append(f"skill {ref.name} could not be read; skipped")
                continue
            except OSError:
                proposal.messages.append(f"skill {ref.name} could not be read; skipped")
                continue

            candidates_by_name.setdefault(ref.name, []).append(
                _SkillCandidate(r.tool, r.label, ref, real_dir, thash)
            )

    # 2. Group candidates by name & choose source
    for name in sorted(candidates_by_name.keys()):
        cands = candidates_by_name[name]
        if (env.config_dir / "skills" / name).exists():
            proposal.messages.append(f"skill {name} already exists in shared source; skipped")
            continue

        distinct_hashes = {c.tree_hash for c in cands}
        chosen: _SkillCandidate | None = None
        if len(distinct_hashes) == 1:
            chosen = cands[0]
        else:
            if from_tool is not None:
                for c in cands:
                    if c.tool == from_tool:
                        chosen = c
                        break
            if chosen is None and ask is not None:
                lines = [f"Skill '{name}' differs between tools:"]
                for i, c in enumerate(cands, 1):
                    lines.append(f"  {i}. {c.label}")
                lines.append(f"Choose a tool to import skill '{name}' from [1-{len(cands)}]: ")
                ans = ask("\n".join(lines)).strip()
                if ans.isdigit():
                    idx = int(ans) - 1
                    if 0 <= idx < len(cands):
                        chosen = cands[idx]
            if chosen is None:
                labels = sorted({c.label for c in cands})
                conflict = f"skill {name} differs between tools ({', '.join(labels)})"
                proposal.conflicts.append(conflict)
                proposal.messages.append(conflict)
                continue

        # Prepare copies
        files = _collect_skill_files(chosen.real_dir)
        proposal.copies.append(SkillImport(name=name, source_path=chosen.real_dir, files=files))

        # 3. Adoption
        for tool_id in sorted(installed_adapters.keys()):
            adapter = installed_adapters[tool_id]
            if Kind.SKILLS not in adapter.capabilities:
                continue

            is_manifest = False
            try:
                probe = [SkillRef(chosen.name, chosen.real_dir)]
                wanted = adapter.wanted_links(env, Scope.GLOBAL, probe)
                is_manifest = len(wanted) == 0
            except Exception:
                is_manifest = False

            if is_manifest:
                native_skills_dir = adapter.paths(env, Scope.GLOBAL).skills_dir
                try:
                    tool_skills = adapter.read_skills(env, Scope.GLOBAL)
                except Exception:
                    tool_skills = []
                for s in tool_skills:
                    if s.name == name:
                        in_native = False
                        if native_skills_dir is not None:
                            try:
                                in_native = s.path.is_relative_to(native_skills_dir)
                            except ValueError:
                                in_native = False
                        if not in_native:
                            try:
                                s_hash = fsops.tree_hash(Path(os.path.realpath(s.path)))
                            except Exception:
                                s_hash = None
                            if s_hash == chosen.tree_hash:
                                proposal.adopt.append(ItemRef(tool_id, Kind.SKILLS, name))
            else:
                skills_dir = adapter.paths(env, Scope.GLOBAL).skills_dir
                if skills_dir is not None:
                    target_entry = skills_dir / name
                    kind = fsops.link_kind(target_entry)
                    if kind in ("symlink", "junction"):
                        try:
                            real_target = Path(os.path.realpath(target_entry))
                            link_hash = fsops.tree_hash(real_target)
                        except Exception:
                            link_hash = None
                        if link_hash == chosen.tree_hash:
                            proposal.adopt.append(ItemRef(tool_id, Kind.SKILLS, name))
                    elif kind == "dir":
                        conflict = (
                            f"{adapter.label}: skill {name} exists as a real folder; "
                            "rename or remove it, then run sync"
                        )
                        if conflict not in proposal.conflicts:
                            proposal.conflicts.append(conflict)
                        if conflict not in proposal.messages:
                            proposal.messages.append(conflict)

    return proposal


def skill_copy_ops(config_dir: Path, proposal: SkillsProposal) -> list[EditFile]:
    """Emit EditFile ops to write skill files into the shared source skills directory."""
    ops: list[EditFile] = []
    for skill in sorted(proposal.copies, key=lambda s: s.name):
        for rel_path, content, mode in sorted(skill.files, key=lambda f: f[0].as_posix()):
            target = config_dir / "skills" / skill.name / rel_path
            ops.append(
                EditFile(
                    path=target,
                    transform=lambda _old, b=content: b,
                    expect_sha256=None,
                    description=f"create shared skill file {skill.name}/{rel_path.as_posix()}",
                    mode=mode,
                )
            )
    return ops


def source_ops(
    env: Env,
    proposal: ImportProposal,
    detected_tool_ids: Sequence[str] = (),
) -> list[EditFile]:
    """Generate EditFile ops to write initial shared source configuration files."""
    ops: list[EditFile] = []

    # 1. config.toml
    tools = {tid: ToolSettings(enabled=True) for tid in detected_tool_ids}
    config_text = render_source_config(tools)
    config_bytes = config_text.encode("utf-8")
    ops.append(
        EditFile(
            path=env.config_dir / SOURCE_CONFIG_NAME,
            transform=lambda _old, b=config_bytes: b,
            expect_sha256=None,
            description=f"create shared {SOURCE_CONFIG_NAME}",
        )
    )

    # 2. mcp.jsonc if proposal.servers
    if proposal.servers:
        mcp_text = render_mcp_source(proposal.servers, notes=proposal.server_notes)
        mcp_bytes = mcp_text.encode("utf-8")
        ops.append(
            EditFile(
                path=env.config_dir / MCP_NAME,
                transform=lambda _old, b=mcp_bytes: b,
                expect_sha256=None,
                description=f"create shared {MCP_NAME}",
            )
        )

    # 3. instructions.md if proposal.instructions
    if proposal.instructions is not None:
        inst = proposal.instructions
        if hasattr(inst, "core"):
            inst_text = inst.core
        elif hasattr(inst, "text"):
            inst_text = inst.text
        else:
            inst_text = str(inst)
        if inst_text is not None:
            inst_bytes = inst_text.encode("utf-8")
            ops.append(
                EditFile(
                    path=env.config_dir / INSTRUCTIONS_NAME,
                    transform=lambda _old, b=inst_bytes: b,
                    expect_sha256=None,
                    description=f"create shared {INSTRUCTIONS_NAME}",
                )
            )

    # 4. skills if proposal.skills
    if isinstance(proposal.skills, SkillsProposal):
        ops.extend(skill_copy_ops(env.config_dir, proposal.skills))
    elif isinstance(proposal.skills, Sequence):
        ops.extend(skill_copy_ops(env.config_dir, SkillsProposal(copies=list(proposal.skills))))

    return ops


def propose(
    readings: Sequence[ToolReading],
    detections: Sequence[Detection],
    *,
    from_tool: str | None = None,
    ask: Callable[[str], str] | None = None,
    env: Env | None = None,
    adapters: Mapping[str, ToolAdapter] | None = None,
) -> ImportProposal:
    """Build an import proposal from tool readings and detections."""
    proposal = ImportProposal()
    inst_proposal = propose_instructions(
        readings,
        from_tool=from_tool,
        ask=ask,
        env=env,
        adapters=adapters,
    )
    if inst_proposal is not None and inst_proposal.core:
        proposal.instructions = inst_proposal
        proposal.messages.extend(inst_proposal.messages)
        for tool_id in inst_proposal.adopt:
            proposal.adopt.append(ItemRef(tool_id, Kind.INSTRUCTIONS, INSTRUCTIONS_KEY))
    elif inst_proposal is not None:
        proposal.messages.extend(inst_proposal.messages)
        proposal.messages.append("no instructions found")
    else:
        proposal.messages.append("no instructions found")

    if env is not None and adapters is not None:
        mcp_proposal = propose_mcp(
            readings,
            adapters,
            env,
            from_tool=from_tool,
        )
        proposal.servers.update(mcp_proposal.servers)
        proposal.server_notes.update(mcp_proposal.notes)
        proposal.adopt.extend(mcp_proposal.adopt)
        proposal.messages.extend(mcp_proposal.messages)
        proposal.import_notes.extend(mcp_proposal.import_notes)

        skills_proposal = propose_skills(
            readings,
            adapters,
            env,
            from_tool=from_tool,
            ask=ask,
        )
        proposal.skills = skills_proposal
        proposal.adopt.extend(skills_proposal.adopt)
        proposal.messages.extend(skills_proposal.messages)

    return proposal
