# 0009: Adapter interface v1

Status: Accepted

This settles the open point of 0008 ("the exact shape of `desired` and `owned`") and the gaps found while writing the contract test (`tests/adapter_contract.py`). It changes no rule of 0008: adapters still never write, `plan_*` stays pure, `Env` stays explicit, and hooks, permissions and memory still default to `Unsupported`.

## Decision

**Item keys and `owned`.** Every managed item has a key within its kind: the server name for MCP, the skill name for skills, and the fixed key `instructions` (`INSTRUCTIONS_KEY`) for the instructions file, which is owned as one item. The ledger of 0005 already stores `owned: {tool: {kind: [item keys]}}`. An adapter now receives only its own keys for that kind, as `Owned = frozenset[str]`, in every `plan_*` call. `desired` is a `str` for instructions, `{name: McpServer}` for MCP and a sequence of `SkillRef` (the source folders) for skills.

**Ownership flows back through the ops.** `EditFile`, `RunCli` and `Link` each get two optional fields, `claims` and `releases`: tuples of `ItemRef(tool, kind, key)`. An op lists the items it creates or updates in `claims` and the items it removes in `releases`. The engine updates `owned` from the ops it applied and nothing else. An `ItemRef` carries the tool id, so a flat plan stays self-describing.

**Removal of a link.** `Link.target` may be `None`, meaning "remove this link". Without it a skill that left the source could not be withdrawn (the three executable ops had no removal for a directory link). It applies only to a link, junction or marked copy that the ledger records as owned.

**Conflicts.** A new non-executed op, `Conflict(tool, kind, key, reason, path)`, expresses "this item is in the way or cannot be written, and is left alone". It is used for:

- a destination that exists, is not owned and differs from what is wanted (0007, and the same rule for an MCP entry or the instructions file);
- desired content the tool cannot take, for example instructions over a size limit (0003).

`Unsupported` keeps its meaning ("the tool cannot express this kind"). `Plan.is_empty` is true only when there is nothing to run and nothing to report: `Unsupported` entries do not count (a permanent gap), a `Conflict` does (the desired state is not reached and the user must be told). `Plan` also gets `executable` and `conflicts`. So "a second plan after apply is empty" holds when there are no conflicts, and a conflict-only plan is not empty and has nothing to execute. An existing item that is not owned but already equals the wanted state produces neither an op nor a conflict.

**Normalization.** A tool may legitimately lose information, so "reading what the plan produced gives back `desired`" means "gives back `normalize(desired)`". `ToolAdapter` gets two overridable methods, `normalize_instructions(text)` and `normalize_mcp(servers)`, both identity by default. An adapter overrides one only for the fields its format cannot store; the contract test uses it as the expected value. Skills have no normalizer: they are compared by name and link target.

**`McpServer` fields.** The type is extended with what the neutral source (`_core/source.py`, `tests/fixtures/servers.jsonc`) needs and a tool can carry: `headers` (a tuple of `HeaderRef(name, env_var, scheme)`, the value always coming from an environment variable, for example `Authorization: Bearer ${TOKEN}`), `cwd`, `auth` (`None`, `oauth` or `none`), `enabled` and `startup_timeout_sec`; `transport` is derived (`http` when `url` is set). `env_vars` stays a tuple of variable NAMES that the tool passes on from its own environment. Non-secret literal values are carried by a new field `env: tuple[tuple[str, str], ...]`, sorted by name so that equality and hashing are stable (a plain mapping would be unhashable inside a frozen dataclass and order-dependent). A value is rejected with `ValueError` at construction when it looks like a secret, using the one check the legacy source also uses (`env_value_looks_secret` in `_core/mask.py`: masking would change the value, or the name sounds secret and the value is not just a reference such as `${VAR}`); a name may not appear in both `env` and `env_vars`, or twice in `env`. So no adapter can carry a secret value, and the rule that secrets are referenced by variable name only still holds: `env` is for values such as `MODE=test`. Header values stay variable-only (`HeaderRef`). The source-level selectors `tools`, `keep_live_env` and `description` are not part of the type: choosing which tools get a server is the engine's job, not an adapter's.

**Contract test.** `tests/adapter_contract.py` is a reusable mixin that runs the checks of 0008 against any adapter in a temporary home, and `tests/test_adapter_contract.py` applies it to a neutral example adapter, to deliberately broken adapters and to every registered adapter (a visible skip while the adapter is still a `StubAdapter`). Ops are applied by a test-only stand-in (`tests/contract_applier.py`, one function as the seam, `make_applier`).

## Reason

- The engine cannot keep the ledger or let `sync` remove entries unless ops say which items they create and remove; guessing it from paths would put tool knowledge into the engine.
- A frozenset of keys is the smallest type that is exact: an adapter cannot be handed the ledger of another tool or kind by mistake.
- 0007 and 0003 already require "report, do not write". Mapping that onto `Unsupported` would blur "cannot express" with "will not touch", and mapping it onto an executable op would break the rule that the plan shows exactly what runs.
- A conflict-only plan reading as empty would let `sync` say "nothing to do" while the user's tools differ from the source.
- Without `normalize_*`, the round-trip check either fails for every adapter that loses a field, or is weakened until it proves nothing.
- Headers and `cwd` appear in the real source and the fixture; a model that cannot hold them would force an adapter to read the raw source, which breaks the boundary.

## Open points

- The contract test's undo check runs against a stand-in, not `engine/`. The `undo` guarantee of 0005 is NOT proven by it. It becomes real only when `make_applier` returns the real engine and the whole suite passes. This is the next plan item.
- Ownership changes that need no op have no carrier: adopting an existing item that already equals the wanted state, and dropping an owned item that has already vanished. The assumption is that `init` (which imports existing config into the source) and the engine's ledger reconciliation record these directly. To be confirmed when the engine is built.
- Literal header values are not representable. The literal-env design does not cover them cleanly: the source judges headers with a different rule (the header name decides, `Authorization` and similar must be references) and they need their own field. Decide when an adapter needs one.
- The copy-plus-manifest fallback of 0007 (`Link.allow_copy_fallback`) is not exercised by the stand-in; links are always plain links there.
- `RunCli` sequences such as "remove, then add" are not atomic (0005 open point); the contract suite applies them step by step and does not simulate a failure between the two.
- `desired` for hooks, permissions and memory is still `object`; it is fixed when those kinds get an implementation.
- Project scope (v0.2) is not covered by the contract test; it runs `Scope.GLOBAL` only.
- A third-party adapter mechanism stays deferred, as in 0008.
