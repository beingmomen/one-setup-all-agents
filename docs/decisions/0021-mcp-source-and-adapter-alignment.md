# 0021: MCP source and adapter alignment

Status: Proposed

Supersedes in part: [0015](0015-source-layout-v1.md) (point 2: `mcp.jsonc` transport rules, secret checks, header schemes, and `description`), [0017](0017-init-import-adoption-snapshot.md) (point 4: MCP literal-secret import behavior), and [0019](0019-verification-results-v0.1.md) (OpenCode startup timeout note; scope of the `CLAUDE_CONFIG_DIR` verification).

While documenting `docs/mcp.md`, several discrepancies were found between earlier ADRs, the source loader, and the four tool adapters. This ADR records the real rules and aligns the loader and adapters.

## Decision

### 1. `mcp.jsonc` transport rules and secret checks (clarifies ADR 0015)

- **Transport exclusivity:** An `http` server (`url` set) forbids `command`, `args`, `cwd`, `env`, and `env_vars`. A `stdio` server (`command` set) forbids `url` and `headers`.
- **Secret checks (`server_secret_problems`):**
  - `command`, `url`, and `cwd` are checked with `mask_text(v) == v`.
  - `args` is checked with `mask_args(args) == args`, which also catches literal values following secret-looking flags such as `--token` or `--api-key`.
  - `env` literals are checked with `env_value_looks_secret(key, value)`.
  - `headers` values must reference an environment variable (`${VAR}` or `<Scheme> ${VAR}`).

### 2. Literal secrets on `init` import (supersedes part of ADR 0017)

- During `init`, adapter readers (`decode_server`) drop secret-looking `env` literals and literal HTTP header values while reading tool configs, so those values never enter the neutral `McpServer` model.
- The server is still imported into `mcp.jsonc` without those dropped fields, `import_notes` records the field names (never values) as warnings and `// Note:` comments, and the server is excluded from adoption (left unowned) for that tool.
- A server is skipped whole on import only when a literal secret is found in `command`, `url`, `cwd`, or `args` (`server_secret_problems`), or when its NAME is itself credential-shaped (`looks_like_credential`): such a server is never copied into the source and its name is never shown (`init` warns that a tool holds one, without the name).

### 3. Shared HTTP header scheme rule

- One shared rule (`HEADER_SCHEME_PATTERN = r"[A-Za-z][A-Za-z0-9._-]*"`, `HEADER_SCHEME_RE` in `model.py`) is used by the source loader (`config_source.py`) and the adapters (`claude_code.py`, `opencode.py`, and `antigravity.py`).
- A scheme must start with an ASCII letter and continue with ASCII letters, digits, `.`, `_`, or `-`. Invalid schemes (for example starting with a digit, `_`, or `-`) are refused at load time with `SourceError` (without echoing the value).

### 4. `description` preserved on `ServerEntry`

- `description` (optional `str | None`) is validated by the source loader, stored on the source-side `ServerEntry` (NOT on the tool-neutral `McpServer`), and written back by `render_mcp_source`.
- Because adapters receive `McpServer` only, `description` is never written to any tool config.

### 5. OpenCode `headers` ownership

- `"headers"` is always in OpenCode's `_OWNED_KEYS` (treating an empty `{}` object as equivalent to absent, matching Claude Code).
- Removing the last header of an owned server from `mcp.jsonc` removes `"headers"` from `opencode.jsonc`, and a second plan is empty.

### 6. Unowned servers with literal headers conflict across all four adapters

- When a desired server already exists in a tool config, is **unowned**, and carries literal HTTP headers that the source does not carry (for example right after `init` drops a literal header and leaves the server unowned), every adapter plans a `Conflict` and leaves the entry untouched, never placing the literal header value in the conflict reason, plan text, or journal:
  - Every adapter checks this first, before the disabled and "not written" (`Unsupported`) branches, with its own notion of a variable reference (Claude Code `${VAR}`, OpenCode `{env:VAR}`, each optionally after a scheme); a header value that is not such a reference is literal (a non-string value included); `import_notes` uses the same predicate, so `init` never adopts an entry that `sync` would protect.
  - Codex and Antigravity keep `http_headers` and `headers` **out** of `_OWNED_KEYS` so updates to owned servers continue to preserve user-added literal headers (ADR 0012).
  - In Antigravity, which has no variable references, a header value of the form `${VAR}` or `<scheme> ${VAR}` holds no secret: `import_notes` and `plan_mcp` use the same rule and treat it as not literal.
  - Because the check comes first, the `Conflict` is reported even when the source server is disabled or cannot be written to that tool (for example a non-Bearer header for Codex).

### 7. OpenCode `startup_timeout_sec`

- Confirmed from OpenCode's published JSON schema (`https://opencode.ai/config.json`, definitions `Mcp.LocalConfigEncoded` and `Mcp.RemoteConfigEncoded`) and official MCP docs (`https://opencode.ai/docs/mcp-servers`): in the v2 layout (`mcp.servers.<name>`), both local and remote servers accept a per-server `"timeout": {"startup": <int>}` object whose values are positive integer **milliseconds** (sibling keys: `catalog`, `execution`).
- In the v2 layout, `OpenCodeAdapter` maps `startup_timeout_sec` to `timeout.startup` in positive integer milliseconds (`max(1, round(sec * 1000))`) and decodes whole-second multiples (`startup % 1000 == 0`) back to `int` seconds (matching Codex's whole-second read-back rule).
- Updating `timeout.startup` edits only that key inside `timeout`, so comments and the sibling `catalog` and `execution` keys survive; a `startup` that is not an integer (for example `true` or `1500.5`) is corrected; a whole-number float such as `30000.0` is the same integer to OpenCode's schema (`Number.isSafeInteger(30000.0)` is true), so it is read as `30` seconds and kept as written. A value above 2^53 - 1 milliseconds (the largest integer JavaScript holds exactly) is not written and is reported as `Unsupported`.
- `normalize_mcp` takes an optional `env`: with it, OpenCode knows the layout of the existing file, so in the v1 layout the timeout is dropped and in the v2 layout it becomes the value read back (`29.9999` -> `30`, `1.5` -> `None`). `init` passes `env`, so an entry is adopted when it differs only where the tool cannot store a value. When the source omits `startup_timeout_sec` (`None`), any existing `timeout` on the server is left untouched ("no opinion", matching Codex).
- In the deprecated v1 layout (`mcp.<name>`), `timeout` is a single request-timeout integer rather than a startup timeout object, so `startup_timeout_sec` is not written and is reported as `Unsupported`.

### 8. Scope of the `CLAUDE_CONFIG_DIR` verification

- Verification V02 (`docs/verification/windows.md`) showed on Windows that an absolute `CLAUDE_CONFIG_DIR` relocates `.claude.json`. It did not test `CLAUDE.md` or skills, so ADR 0019's wording ("`.claude.json` and `CLAUDE.md`") is narrowed: the Claude Code adapter's docstring and notes call only the `.claude.json` relocation verified.

## Reason

- Keeping ADRs aligned with real code behavior prevents future drift and conflicting assumptions.
- Sharing one header-scheme regex removes a class of inputs accepted by the loader and rejected during planning (or vice versa).
- Preserving `description` on `ServerEntry` keeps user notes intact across source round trips without polluting the tool-neutral `McpServer` or tool configs.
- Owning `headers` in OpenCode fixes silent drift when the last header is removed.
- Reporting `Conflict` on unowned entries with literal headers makes all four adapters agree after `init` drops a literal header, matching the existing behavior for dropped secret `env` values, without regressing ADR 0012's preservation of user headers on owned Codex/Antigravity entries.
- Mapping `startup_timeout_sec` in OpenCode v2 removes an unnecessary `Unsupported` gap now that the official schema and docs confirm the per-server key and millisecond unit.

## Open points

1. **Fractional `startup_timeout_sec` round trip:** Both Codex and OpenCode read back whole seconds only so `McpServer.startup_timeout_sec` stays `int | None`; deciding in v0.2 whether fractional timeouts should become part of the neutral model.
2. **OpenCode v1 layout deprecation:** If OpenCode removes v1 (`mcp.<name>`) support, decide whether to drop v1 write-in-place support or offer a migration command.
