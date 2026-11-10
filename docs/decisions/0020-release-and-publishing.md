# 0020: Release and publishing

Status: Proposed

This ADR records the release and distribution process for `one-setup-all-agents` on PyPI, formalizing the trusted publishing design outlined in ADR 0001.

## Decision

1. **Trusted Publishing (OIDC):**
   - Publishing uses PyPI Trusted Publishing via OpenID Connect (OIDC).
   - No long-lived PyPI API tokens or passwords are ever stored or used in repository secrets or workflows.
   - The workflow requests token generation via `id-token: write` strictly within the `publish` job under the protected GitHub environment `pypi`.

2. **Trigger and Execution Boundary:**
   - Automatic publishing is triggered only on annotated Git tag pushes matching the format `v*` (e.g. `v0.1.0`, `v0.1.0rc1`).
   - Manual workflow runs (`workflow_dispatch`) execute the `build` job only (creating and verifying artifacts) and never trigger publishing.
   - The `publish` job requires manual maintainer approval via GitHub environment protection rules.

3. **Version Alignment:**
   - The Git tag name (without the leading `v`) must strictly match `one_setup_all_agents.__version__` defined in `src/one_setup_all_agents/__init__.py`.
   - The `build` job enforces this match and fails the run if a mismatch is detected before publishing can occur.

4. **Action Pinning Policy:**
   - All third-party GitHub Actions in the release workflow are pinned to full commit SHAs, accompanied by inline comments stating the release tag.
   - Action releases must be at least two weeks old to guard against compromised upstream action releases.

5. **Distribution Validation:**
   - Prior to uploading artifacts, `scripts/check_dist.py` verifies both the wheel (`.whl`) and source distribution (`.tar.gz`) for zero runtime dependencies, presence of console script entry points (`all-agents` and `one-setup-all-agents`), Python `>=3.11` requirement, and the complete absence of test files, development workflows, or `.personal-patterns`.

## Consequences

- Accidental releases are prevented at multiple levels: tag name verification, build-only `workflow_dispatch`, and GitHub environment approval.
- Supply chain security is hardened against compromised credentials or tampered dependencies.
- PyPI releases are immutable; corrections require bumping `__version__` and creating a new release tag.
