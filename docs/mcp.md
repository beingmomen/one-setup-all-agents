# MCP Servers Guide

`one-setup-all-agents` keeps a single tool-neutral list of your MCP servers in `mcp.jsonc` and translates it into the native format of each detected tool on `all-agents sync`. In v0.1, MCP management covers the **global (user) scope** only.

## Where `mcp.jsonc` Lives

`mcp.jsonc` sits in your shared source config directory alongside `config.toml` (see [Config Locations](../README.md#config-locations) and [ADR 0002](decisions/0002-config-and-state-locations.md)):

- **Windows:** `%APPDATA%\one-setup-all-agents\mcp.jsonc` (fallback `~\AppData\Roaming\one-setup-all-agents\mcp.jsonc`)
- **Linux:** `$XDG_CONFIG_HOME/one-setup-all-agents/mcp.jsonc` (default `~/.config/one-setup-all-agents/mcp.jsonc`)
- **macOS:** `~/.config/one-setup-all-agents/mcp.jsonc` (same as Linux; uses `$XDG_CONFIG_HOME` when set to an absolute path)

You can override the source directory on any OS with `--config-dir DIR` or the `ALL_AGENTS_CONFIG_DIR` environment variable (`--config-dir` takes precedence over the environment variable).

The file uses UTF-8 JSONC: `//` line comments, `/* */` block comments, and trailing commas are allowed. A UTF-8 BOM is accepted when reading; newly generated source files use LF (`\n`) line endings.

## `mcp.jsonc` Schema

### Top-level keys

| Key | Required | Type | Meaning |
|---|---|---|---|
| `version` | Yes | integer (`1`) | Source format version. Must be the integer `1` (booleans are rejected). |
| `servers` | No | object | Map of server name to server definition (defaults to `{}`). |

Any other top-level key is refused with a `SourceError`.

### Server fields

Every entry under `servers` may use only the following fields:

| Field | Type | Transport | Meaning |
|---|---|---|---|
| `command` | non-empty string | `stdio` only (required) | Executable name or path for a local MCP server. |
| `args` | list of strings | `stdio` only | Command-line arguments passed to `command` (default `[]`). |
| `cwd` | string | `stdio` only | Working directory for the server process. |
| `url` | non-empty string | `http` only (required) | Endpoint URL for a remote HTTP MCP server. |
| `env` | object (`string -> string`) | `stdio` only | Non-secret literal environment variables to set for the server. |
| `env_vars` | list of strings | `stdio` only | Names of environment variables to pass through from the host environment under the same name. Each entry must match `^[A-Za-z_][A-Za-z0-9_]*$`. |
| `headers` | object (`string -> string`) | `http` only | HTTP headers whose values reference an environment variable: `${VAR_NAME}` or `<Scheme> ${VAR_NAME}` (for example `Bearer ${EXAMPLE_TOKEN}`). |
| `startup_timeout_sec` | positive finite number | both | Server startup timeout in seconds (booleans are rejected). Prefer whole integers: the neutral model and Codex reads keep whole seconds. |
| `auth` | `"oauth"`, `"none"`, or `null` | both | Documentation-only marker. Not written to any tool. |
| `enabled` | boolean | both | Whether the server is enabled (default `true`). |
| `tools` | list of strings | both | Target adapter IDs (`"opencode"`, `"claude"`, `"codex"`, `"agy"`). When omitted, the server targets every enabled tool. |
| `description` | optional note | both | Accepted by the loader as a human-readable note, but not stored on the internal server model, not preserved by `init` rendering, and never written to tools. String values are still checked against legacy `{{VAR}}` syntax. |

### Transport rules (`stdio` vs `http`)

Transport is inferred from whether `command` or `url` is present (there is no `type` field in `mcp.jsonc`):

- Every server must set **either** `command` (`stdio`) **or** `url` (`http`), never both and never neither.
- A **`http` server** (`url` set) cannot set `command`, `args`, `cwd`, `env`, or `env_vars`.
- A **`stdio` server** (`command` set) cannot set `headers`.

### Complete schema example

<!-- example: mcp.jsonc -->
```jsonc
// Names only: no secrets. Reference environment variables with ${VAR_NAME}.
{
  "version": 1,
  "servers": {
    "demo-local": {
      "description": "Local stdio server example",
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "cwd": "/path/to/workspace",
      "env": {
        "LOG_LEVEL": "info"
      },
      "env_vars": ["EXAMPLE_API_KEY"],
      "startup_timeout_sec": 30,
      "enabled": true
    },
    "demo-remote": {
      "url": "https://example.test/mcp",
      "headers": {
        "Authorization": "Bearer ${EXAMPLE_TOKEN}"
      },
      "auth": "oauth",
      "tools": ["opencode", "claude", "codex"]
    }
  }
}
```

### What the loader refuses

`load_source` / `load_source_texts` (in `config_source.py`) raises `SourceError` and stops before any planning or writing if `mcp.jsonc` contains any of the following:

- Missing `version`, or a `version` other than the integer `1`.
- Any unknown top-level key or unknown server field.
- Legacy keys from earlier prototypes: `retired`, `unmanaged`, `keep_live_env`, or `vars`.
- Legacy `{{VAR}}` syntax anywhere inside a string value (use `${VAR}` instead).
- Both `url` and `command` on the same server, neither of them, an empty/whitespace-only `command` or `url`, or fields belonging to the other transport.
- Invalid field types (for example non-list `args`, non-boolean `enabled`, non-positive/non-finite or boolean `startup_timeout_sec`, invalid `auth`, or an unknown tool ID in `tools`).
- Invalid variable names in `env_vars` or `headers` (names must start with an ASCII letter or `_`, followed by ASCII letters, digits, or `_`).
- A variable name listed in both `env` and `env_vars` on the same server.
- A literal value in `headers` (every header value must match `${VAR}` or `<Scheme> ${VAR}`).
- A value that looks like a secret in `command`, `url`, `cwd`, `args`, or `env`. The detector (`_core/mask.py`) flags known token prefixes (such as `ghp_`, `github_pat_`, `sk-`, `xox...`, `AIza...`, JWTs), `Bearer`/`Basic` followed by a literal, URL userinfo passwords, secret `key=value` assignments, values after secret-looking flags (such as `--token`), opaque 32+ character strings with both letters and digits, and any non-empty `env` literal whose key name suggests a secret (`token`, `secret`, `password`/`passwd`, `api_key`/`apikey`, `credential`, `private_key`, `access_key`).

## Secrets and Variable References

`all-agents` never reads, prints, stores, or writes secret values. Every credential in `mcp.jsonc` is referenced by environment-variable name only:

- **HTTP headers (`headers`):** Write `"${VAR_NAME}"` for a raw header value or `"<Scheme> ${VAR_NAME}"` for a prefixed value such as `"Authorization": "Bearer ${EXAMPLE_TOKEN}"`.
- **Secret environment variables (`env_vars`):** List variable names in `env_vars` (for example `["EXAMPLE_API_KEY"]`). Each tool passes that variable from its environment to the MCP server under the **same name**.
- **Non-secret literals (`env`):** Use `env` only for plain configuration such as `"LOG_LEVEL": "info"` or `"MODE": "test"`. Anything that looks like a secret is refused by the loader.

> **Important:** Mapping an environment variable to a different name (for example setting `API_KEY` from `${MY_TOKEN}`) is **not supported today**. `env_vars` always passes the variable under its own name, and putting `${MY_TOKEN}` inside `env` is not portable across tools (Codex does not expand variable syntax inside `env`, Claude Code refuses `${` in literal `env` entries, and OpenCode does not expand `${...}`).

### Shell vs GUI environments

Because credentials come from environment variables at runtime, the tool process must actually see those variables. On macOS (and some desktop launchers), GUI apps do not inherit variables defined only in a shell profile (see [ADR 0002](decisions/0002-config-and-state-locations.md)).

### What `all-agents init` does with existing literal secrets

When `all-agents init` reads your existing tool configs:

- Adapter readers never copy literal secret `env` values or literal HTTP headers into the neutral model. Instead, `init` prints a warning naming the field (never the value), adds a `// Note:` comment above that server in `mcp.jsonc`, and leaves that server **unowned** for that tool so the next `all-agents sync` reports a `Conflict` rather than stripping your live credentials.
- If a secret-looking literal appears inside `command`, `url`, `cwd`, or `args`, `init` skips importing that server altogether and prints a warning naming the field.

## Per-Tool Translation

Given these two servers in `mcp.jsonc`:

<!-- example: mcp.jsonc -->
```jsonc
{
  "version": 1,
  "servers": {
    "demo-local": {
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "cwd": "/path/to/workspace",
      "env": {
        "LOG_LEVEL": "info"
      },
      "env_vars": ["EXAMPLE_API_KEY"],
      "startup_timeout_sec": 30
    },
    "demo-remote": {
      "url": "https://example.test/mcp",
      "headers": {
        "Authorization": "Bearer ${EXAMPLE_TOKEN}"
      }
    }
  }
}
```

### Summary table

| Feature | Claude Code (`claude`) | OpenCode (`opencode`) | Codex (`codex`) | Antigravity (`agy`) |
|---|---|---|---|---|
| Where it is written | `~/.claude.json` via `claude mcp` CLI (`-s user`) | `~/.config/opencode/opencode.jsonc` (`mcp.servers`) | `~/.codex/config.toml` (managed region) | `~/.gemini/config/mcp_config.json` (`mcpServers`) |
| Path override | `$CLAUDE_CONFIG_DIR` (absolute) | `$XDG_CONFIG_HOME` (absolute) | `$CODEX_HOME` (absolute) | None |
| `env_vars` | `"env": {"VAR": "${VAR}"}` | `"environment": {"VAR": "{env:VAR}"}` | `env_vars = ["VAR"]` | Not written (`Unsupported`) |
| `env` literals | `"env": {"K": "v"}` | `"environment": {"K": "v"}` | `[mcp_servers.<name>.env]` | `"env": {"K": "v"}` |
| `headers` (`Authorization: Bearer ${VAR}`) | `"headers": {"Authorization": "Bearer ${VAR}"}` | `"headers": {"Authorization": "Bearer {env:VAR}"}` | `bearer_token_env_var = "VAR"` | Server not written (`Unsupported`) |
| `headers` (no scheme, e.g. `X-API-Key: ${VAR}`) | `"headers": {"X-API-Key": "${VAR}"}` | `"headers": {"X-API-Key": "{env:VAR}"}` | `[mcp_servers.<name>.env_http_headers]` | Server not written (`Unsupported`) |
| `cwd` | Not stored (`Unsupported`) | `"cwd": "..."` | `cwd = "..."` | `"cwd": "..."` |
| `startup_timeout_sec` | Not stored (`Unsupported`) | Not stored (`Unsupported`) | `startup_timeout_sec = 30` | Not stored (`Unsupported`) |
| `enabled: false` | Not written; removes owned entry (`Unsupported`) | `"disabled": true` (v2) / `"enabled": false` (v1) | `enabled = false` | `"disabled": true` |
| `auth` | Documentation only (not written) | Documentation only (not written) | Documentation only (not written) | Documentation only (not written) |
| Windows `npx`/`npm`/`pnpm`/`yarn` | Plain name (`npx`) | `.cmd` suffix (`npx.cmd`) | `.cmd` suffix (`npx.cmd`) | `.cmd` suffix (`npx.cmd`) |

### Claude Code (`claude`)

Claude Code rewrites `~/.claude.json` from running sessions, so `all-agents` never edits that file directly. Instead, it runs Claude Code's own CLI with `-s user` (leaving project/local servers untouched):

- Add: `claude mcp add-json <name> '<json>' -s user` (inverse: `claude mcp remove <name> -s user`)
- Update an owned server: `remove` followed by `add-json`
- Remove: `claude mcp remove <name> -s user` (inverse: `claude mcp add-json <name> '<old json>' -s user`)

The JSON payload passed for the two example servers (resulting entries under `"mcpServers"` in `~/.claude.json`, or `$CLAUDE_CONFIG_DIR/.claude.json` when `CLAUDE_CONFIG_DIR` is set to an absolute path):

```json
{
  "demo-local": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@example/mcp-server"],
    "env": {
      "LOG_LEVEL": "info",
      "EXAMPLE_API_KEY": "${EXAMPLE_API_KEY}"
    }
  },
  "demo-remote": {
    "type": "http",
    "url": "https://example.test/mcp",
    "headers": {
      "Authorization": "Bearer ${EXAMPLE_TOKEN}"
    }
  }
}
```

- `type` is always written (`"stdio"` or `"http"`).
- `cwd` and `startup_timeout_sec` are omitted and reported as `Unsupported` (`demo-local: startup_timeout_sec, cwd not stored`).
- `npx`, `npm`, `pnpm`, and `yarn` are written as plain names on every OS (no `.cmd` suffix on Windows).

### OpenCode (`opencode`)

Edited as text inside `~/.config/opencode/opencode.jsonc` (or `$XDG_CONFIG_HOME/opencode/opencode.jsonc`), preserving comments and unrelated keys. If `opencode.json` exists and `opencode.jsonc` does not, `opencode.json` is edited instead. New entries use the v2 layout under `"mcp": {"servers": {...}}` (an existing v1 file with `"mcp": {"<name>": {...}}` is edited in place, using `"enabled": false` instead of `"disabled": true`).

```jsonc
{
  "mcp": {
    "servers": {
      "demo-local": {
        "type": "local",
        "command": ["npx", "-y", "@example/mcp-server"],
        "environment": {
          "LOG_LEVEL": "info",
          "EXAMPLE_API_KEY": "{env:EXAMPLE_API_KEY}"
        },
        "cwd": "/path/to/workspace"
      },
      "demo-remote": {
        "type": "remote",
        "url": "https://example.test/mcp",
        "headers": {
          "Authorization": "Bearer {env:EXAMPLE_TOKEN}"
        }
      }
    }
  }
}
```

- On Windows, `npx`, `npm`, `pnpm`, and `yarn` are written with a `.cmd` suffix (`"npx.cmd"`).
- `startup_timeout_sec` is omitted and reported as `Unsupported` (`demo-local: startup_timeout_sec not stored`).

### Codex (`codex`)

Edited as text inside `~/.codex/config.toml` (or `$CODEX_HOME/config.toml` when `CODEX_HOME` is an absolute path). Added or changed `[mcp_servers.<name>]` tables are written inside a managed region at the end of the file (`# >>> one-setup-all-agents >>>` ... `# <<< one-setup-all-agents <<<`); everything outside the managed region stays intact.

```toml
# >>> one-setup-all-agents >>>
[mcp_servers.demo-local]
command = "npx"
args = ["-y", "@example/mcp-server"]
startup_timeout_sec = 30
env_vars = ["EXAMPLE_API_KEY"]
cwd = "/path/to/workspace"

[mcp_servers.demo-local.env]
LOG_LEVEL = "info"

[mcp_servers.demo-remote]
url = "https://example.test/mcp"
bearer_token_env_var = "EXAMPLE_TOKEN"
# <<< one-setup-all-agents <<<
```

- On Windows, `npx`, `npm`, `pnpm`, and `yarn` are written with a `.cmd` suffix (`command = "npx.cmd"`).
- If `startup_timeout_sec` is omitted in `mcp.jsonc`, any timeout you added by hand to an owned Codex server is kept.
- **How Codex handles headers:**
  - `Authorization: Bearer ${VAR}` (case-insensitive header name and scheme) becomes `bearer_token_env_var = "VAR"` (Codex prepends `Bearer ` itself).
  - Any header with **no scheme** (for example `"X-API-Key": "${EXAMPLE_API_KEY}"`, or `"Authorization": "${EXAMPLE_RAW_AUTH}"`) becomes an entry in `[mcp_servers.<name>.env_http_headers]`:
    ```toml
    [mcp_servers.demo-custom]
    url = "https://example.test/mcp"

    [mcp_servers.demo-custom.env_http_headers]
    X-API-Key = "EXAMPLE_API_KEY"
    ```
  - Any header with a **scheme other than `Bearer`** (such as `Basic ${VAR}` or `Token ${VAR}`), or **any scheme on a header other than `Authorization`**, cannot be represented in Codex: the entire server is **not written** and is reported as `Unsupported`.

### Antigravity (`agy`)

Edited as text inside `~/.gemini/config/mcp_config.json` under `"mcpServers"`, preserving comments and unrelated entries.

```json
{
  "mcpServers": {
    "demo-local": {
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "env": {
        "LOG_LEVEL": "info"
      },
      "cwd": "/path/to/workspace"
    }
  }
}
```

- On Windows, `npx`, `npm`, `pnpm`, and `yarn` are written with a `.cmd` suffix (`"npx.cmd"`).
- `env_vars` is **not written** and is reported as `Unsupported` on every plan (`env_vars not stored (relies on agy passing its own environment to the server; unverified)`). The server itself is still written.
- `startup_timeout_sec` is omitted and reported as `Unsupported`.
- `demo-remote` is **not written** and is reported as `Unsupported`, because Antigravity has no variable-reference syntax for HTTP headers and `all-agents` never writes literal credentials.
- An HTTP server **without** `headers` is written using `"serverUrl"` (not `"url"`):
  ```json
  {
    "mcpServers": {
      "demo-public": {
        "serverUrl": "https://example.test/mcp"
      }
    }
  }
  ```

### Verification status across platforms

See the individual reports in [`docs/verification/`](verification/) and the [Supported tools](../README.md#supported-tools) table:

- **Windows:** Verified live against installed tool binaries on 2026-11-10 (Claude Code 2.1.319, Codex 0.167.0, OpenCode 2.2.6, Antigravity 1.5.1), including Claude Code `mcp add-json` / `remove` and `CLAUDE_CONFIG_DIR`, and Codex and Antigravity reading generated stdio MCP configs. Two behaviors remain marked **unverified** against live tools in the adapters: Windows `.cmd` suffixing for `npx`/`npm`/`pnpm`/`yarn` launchers, and Antigravity `env_vars` process-environment inheritance.
- **Linux and macOS:** Verified with unit and adapter-contract fixtures only (WSL2 and GitHub Actions CI), not against real tool installations.

## Per-Tool Limits and Reporting

### Summary of per-tool limits

- **Claude Code (`claude`):**
  - `cwd` and `startup_timeout_sec` are not stored (`Unsupported`).
  - `enabled: false` is not stored (`Unsupported`) because user scope has no per-server switch: an owned disabled server is removed, an unowned one is left alone, and a new disabled server is not added.
  - Server names must match `^[A-Za-z0-9_][A-Za-z0-9_-]*$` (letters, digits, hyphens, underscores; cannot start with a hyphen); other names are not written (`Unsupported`).
  - A literal `env` value containing `${` is not written (`Unsupported`), because Claude Code would expand it.
  - Requires the `claude` CLI on `PATH`, in a known install folder (such as `~/.claude/local`), or via `ALL_AGENTS_CLAUDE_BIN`; if missing, any MCP change becomes a `Conflict`.
  - On Windows, if the CLI is a `.cmd` or `.bat` shim and any argument contains `% & | < > ^ !`, MCP changes are refused as a `Conflict` (use `claude.exe` or set `ALL_AGENTS_CLAUDE_BIN`).
  - If an owned entry in `.claude.json` holds a secret-looking literal, replacing or removing it is refused as a `Conflict` so the value never reaches a command line or journal.
- **OpenCode (`opencode`):**
  - `startup_timeout_sec` is not stored (`Unsupported`).
- **Codex (`codex`):**
  - Headers can only be `Authorization: Bearer ${VAR}` or a raw `${VAR}` with no scheme; any other scheme causes the server to be skipped (`Unsupported`), as do duplicate header names differing only in case.
  - Server names must match `^[A-Za-z0-9_:@/.-]+$`; other names are not written (`Unsupported`).
  - Comments inside a modified `[mcp_servers.<name>]` table are lost when the table moves into the managed region.
  - A bare `[mcp_servers]` table, inline server tables, or nested tables deeper than one level under an owned server cannot be edited safely and are reported as `Conflict` (file left alone).
- **Antigravity (`agy`):**
  - Any HTTP server with `headers` is not written (`Unsupported`).
  - `env_vars` is not stored (`Unsupported`; relies on unverified process-environment inheritance).
  - `startup_timeout_sec` is not stored (`Unsupported`).
- **All tools:**
  - `auth` is documentation-only and is not written to any tool.
  - Header schemes must start with a letter when written to Claude Code or OpenCode (`^[A-Za-z][A-Za-z0-9._-]*$`); a scheme starting with a digit passes the source loader but is reported as `Unsupported` by adapters.

### How `Unsupported` and `Conflict` are reported

`all-agents` never drops a field or server silently:

- **`Unsupported` (permanent tool limit):**
  - In `all-agents sync --dry-run` and `all-agents sync`, gaps are counted in the summary line (`Plan: ... N unsupported`) and listed under `Not supported by the tool (shown so the gap is visible):`. `Unsupported` entries do **not** cause `sync` to fail (exit code stays `0` when there are no conflicts or errors).
  - In `all-agents status`, once a tool's `mcp` kind has no reader errors, conflicts, or pending changes, it shows `mcp: unsupported (reasons: ...)`. The tool's overall status still reports `in sync` and `status` exits `0`, because an `Unsupported` entry is a permanent capability gap rather than drift.
- **`Conflict` (something in the way):**
  - Reported when a server with the same name already exists and was not created or adopted by `all-agents`, when a tool config file cannot be parsed, or when the `claude` CLI is missing for a pending Claude Code MCP change.
  - Conflicting items are left untouched. They appear under `Conflicts (left alone, not changed):` in `sync` (exit code `3`) and as `mcp: conflict (...)` in `status` (exit code `2`).

## Walkthrough: From OpenCode to All Tools

This walkthrough starts from an existing OpenCode setup with two MCP servers, imports them into `mcp.jsonc`, previews the plan, and syncs them to Claude Code, Codex, and Antigravity.

### 1. Starting OpenCode configuration

Suppose `~/.config/opencode/opencode.jsonc` already defines one local server (with a non-secret literal and an environment-variable reference) and one remote server (with a bearer token header):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "demo-local": {
        "type": "local",
        "command": ["npx", "-y", "@example/mcp-server"],
        "environment": {
          "LOG_LEVEL": "info",
          "EXAMPLE_API_KEY": "{env:EXAMPLE_API_KEY}"
        }
      },
      "demo-remote": {
        "type": "remote",
        "url": "https://example.test/mcp",
        "headers": {
          "Authorization": "Bearer {env:EXAMPLE_TOKEN}"
        }
      }
    }
  }
}
```

### 2. Import into the shared source with `all-agents init`

Run:

```bash
all-agents init
```

`init` detects all installed tools, prints an import proposal listing `demo-local` and `demo-remote` (and adopting both entries for OpenCode), asks for confirmation (`Proceed with initialisation? [y/N]:`), takes a permanent safety snapshot of every detected tool's existing config files, and creates `config.toml` and `mcp.jsonc` in your config directory.

When other tools (Claude Code, Codex, Antigravity) are also installed but do not have those servers yet, `init` scopes each imported server to `"tools": ["opencode"]` so nothing is pushed to the other tools until you choose to share it:

<!-- example: mcp.jsonc -->
```jsonc
// Names only: no secrets. Reference environment variables with ${VAR_NAME}.
{
  "version": 1,
  "servers": {
    "demo-local": {
      "command": "npx",
      "args": [
        "-y",
        "@example/mcp-server"
      ],
      "env": {
        "LOG_LEVEL": "info"
      },
      "env_vars": [
        "EXAMPLE_API_KEY"
      ],
      "tools": [
        "opencode"
      ]
    },
    "demo-remote": {
      "url": "https://example.test/mcp",
      "headers": {
        "Authorization": "Bearer ${EXAMPLE_TOKEN}"
      },
      "tools": [
        "opencode"
      ]
    }
  }
}
```

To share both servers across all enabled tools, remove the `"tools": ["opencode"]` key from both entries in `mcp.jsonc`.

### 3. Preview with `all-agents sync --only mcp --dry-run`

Run a dry run focused on MCP servers (you can also run `all-agents sync --dry-run`, which additionally plans instructions and skills and lists `Unsupported` entries for future kinds `hooks`, `permissions`, and `memory`):

```bash
all-agents sync --only mcp --dry-run
```

With OpenCode already in sync and Claude Code, Codex, and Antigravity detected, the plan looks like this (your home directory is shortened to `~` here, and `claude` stands for the full path of the detected Claude Code binary; path separators match your OS):

```text
Plan: 4 change(s), 0 conflict(s), 1 unsupported
To run:
  1. create file ~/.gemini/config/mcp_config.json [sensitive]: update the MCP servers
  2. run claude mcp add-json demo-local '{"args": ["-y", "@example/mcp-server"], "command": "npx", "env": {"EXAMPLE_API_KEY": "${EXAMPLE_API_KEY}", "LOG_LEVEL": "info"}, "type": "stdio"}' -s user: add MCP server demo-local to Claude Code
     inverse: claude mcp remove demo-local -s user
  3. run claude mcp add-json demo-remote '{"headers": {"Authorization": "Bearer ${EXAMPLE_TOKEN}"}, "type": "http", "url": "https://example.test/mcp"}' -s user: add MCP server demo-remote to Claude Code
     inverse: claude mcp remove demo-remote -s user
  4. create file ~/.codex/config.toml [sensitive]: update the MCP servers
Not supported by the tool (shown so the gap is visible):
  - agy mcp: demo-local: env_vars not stored (relies on agy passing its own environment to the server; unverified); demo-remote: not written, agy has no way to reference an environment variable in a header, and a header value must never be written as a literal
Dry run: nothing was written.
```

Notice that:

- OpenCode needs no changes because `init` adopted its existing matching entries.
- Claude Code plans two `claude mcp add-json ... -s user` commands, each with its exact `remove` inverse.
- Codex and Antigravity each plan one atomic file edit marked `[sensitive]`.
- Antigravity's limits (`env_vars` on `demo-local` and `headers` on `demo-remote`) are shown upfront under `Not supported by the tool`.

### 4. Apply with `all-agents sync --only mcp`

When the plan looks right, apply it:

```bash
all-agents sync --only mcp
```

`all-agents` prints the same plan, asks `Apply 4 change(s)? [y/N]:`, applies the changes atomically with pre-write backups and journal entries, prints `applied operation <op-id>`, and re-plans to verify no pending changes remain.

Afterward, running `all-agents status` exits `0`: OpenCode, Claude Code, and Codex report `mcp: in sync`, while Antigravity reports overall `in sync` with `mcp: unsupported` repeating the two documented gaps. If you want to roll everything back (including removing the Claude Code servers via `claude mcp remove`), run `all-agents undo`.
