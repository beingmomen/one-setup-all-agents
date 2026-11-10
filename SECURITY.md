# Security Policy

## Supported Versions

`one-setup-all-agents` is currently in pre-alpha / active development. While pre-1.0, security updates and bug fixes are provided for the latest released version only.

| Version | Supported |
|---|---|
| Latest release (`>=0.0.0`) | :white_check_mark: |
| Older releases | :x: |

## Reporting a Vulnerability

If you discover a security vulnerability or potential data leak in `one-setup-all-agents`, please report it privately through GitHub's Private Vulnerability Reporting:

**[Report a vulnerability via GitHub Advisory](https://github.com/beingmomen/one-setup-all-agents/security/advisories/new)**

Do **not** open a public issue or discussion for an unpatched vulnerability.

### Response Timeline
- **Acknowledgment:** We aim to acknowledge reports within 7 days.
- **Resolution or Explanation:** We aim to provide a fix or public explanation within 90 days.
- **Capacity:** Project maintenance is currently carried out on a best-effort basis by a single maintainer.

### What to Include
When submitting a report, please include:
- The exact version of `one-setup-all-agents` and Python version (`all-agents --version` and `python --version`).
- Operating system and version (Windows, Linux distro, macOS).
- Clear, minimal step-by-step reproduction instructions.
- A proof-of-concept configuration or demonstration where possible.
- **Never include real secrets, tokens, or credentials in your report.** Use dummy placeholders (e.g. `MOCK_SECRET_TOKEN`).

## In Scope and Out of Scope

### In Scope
- Any behavior causing `one-setup-all-agents` to read, print to logs/terminal, or persist cleartext secrets.
- Writes or modifications made outside the items explicitly owned or configured by `all-agents`.
- Loss or corruption of user configuration files upon running `all-agents undo`.
- Unintended or malicious command execution triggered during configuration processing.
- Path traversal vulnerabilities in journal logging, backups, snapshot extraction, or history pruning.
- Supply chain security vulnerabilities in packaging or distribution artifacts.

### Out of Scope
- Attacks requiring prior root, administrator, or privileged local machine compromise.
- Attacks relying on social engineering or physical access to the device.
- Unsupported operating systems or modified/forked engine versions.
- Bugs that do not breach confidentiality, file safety boundaries, or execution integrity.

## Security Model

`one-setup-all-agents` is built around safety and least privilege:

1. **Zero Secret Storage:**
   - Secrets are referenced strictly by environment-variable name (e.g. `${GITHUB_TOKEN}` or `Authorization: Bearer <NAME>`).
   - Secret-like values are detected and masked using pattern detectors implemented in `src/one_setup_all_agents/_core/mask.py` and filtered during plan rendering in `src/one_setup_all_agents/engine/plan.py`.
2. **Subprocess Isolation:**
   - When tool detection runs version probes or external adapter CLIs are executed, commands are invoked directly as argument vectors without a shell (`shell=False`), preventing shell injection attacks.
   - Enforced in `src/one_setup_all_agents/detect.py` and `src/one_setup_all_agents/engine/runcli.py`.
3. **Atomic Writes and File Permissions:**
   - All file modifications are staged through temporary files and applied atomically.
   - Files are written with restricted permissions (`0600` for sensitive files, `0700` for state directories on POSIX systems).
   - Enforced in `src/one_setup_all_agents/engine/atomic.py` and `src/one_setup_all_agents/engine/fsops.py`.
4. **Safety Backups and Reversible Journal:**
   - Before any file edit or link change is performed, a full byte backup is captured in the engine state directory.
   - Enforced in `src/one_setup_all_agents/engine/backup.py`.
   - Backups are never printed and cannot be read without local user permissions.
5. **Zero Runtime Dependencies:**
   - The runtime relies exclusively on the Python standard library, eliminating third-party dependency vulnerabilities.
6. **Data Guardrails:**
   - Repository code and release artifacts are validated against forbidden personal path patterns and email disclosures using `scripts/check_personal_data.py`.
