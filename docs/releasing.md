# Release Process

This document provides the release checklist for project maintainers. Releasing is automated through GitHub Actions using PyPI Trusted Publishing (OIDC) per [ADR 0020](decisions/0020-release-and-publishing.md).

## Prerequisites (One-time Setup)

1. **PyPI Trusted Publisher Configuration:**
   - Log in to [pypi.org](https://pypi.org).
   - Under account settings / publishing, add a **Pending Publisher** with:
     - **Owner:** `<owner>` (your GitHub username or organization)
     - **Repository:** `one-setup-all-agents`

     - **Workflow name:** `release.yml`
     - **Environment:** `pypi`
2. **GitHub Environment Protection:**
   - In GitHub repository settings (`Settings -> Environments`), create the `pypi` environment.
   - Configure **Required reviewers** to require your personal approval before deployments proceed.

---

## Release Checklist

### 1. Version Bump
- Update `__version__` in `src/one_setup_all_agents/__init__.py` to the target version (e.g. `0.1.0rc1` or `0.1.0`).
- Update `CHANGELOG.md` with the release notes, moving items from `[Unreleased]` to a new version header.

### 2. Pre-release Verification
Run the test suite and security guards locally:
```bash
python -m unittest discover -s tests
python scripts/check_personal_data.py
```
Ensure all tests pass and no unmasked personal data is present.

### 3. Commit and Tag
Create the release commit and signed/annotated Git tag:
```bash
git add src/one_setup_all_agents/__init__.py CHANGELOG.md
git commit -m "release: vX.Y.Z"
git tag -a vX.Y.Z -m "Release vX.Y.Z"
```
> **Important:** The tag name MUST start with `v` followed by the exact version in `__version__` (e.g., `v0.1.0`).

### 4. Push to GitHub
```bash
git push origin main
git push origin vX.Y.Z
```

### 5. Deployment Approval
- Navigate to GitHub repository **Actions** tab.
- The `Release` workflow will execute the `build` job, verify tag matches `__version__`, and validate distribution artifacts with `scripts/check_dist.py`.
- The `publish` job will pause for environment approval. Review and approve the deployment to `pypi`.

### 6. Post-release Verification
Test installing directly from PyPI in a clean shell:
```bash
uvx one-setup-all-agents --version
```
Confirm the output displays `all-agents X.Y.Z`.

---

## Rollback Policy
PyPI releases are immutable. Once a version string is published, artifacts cannot be overwritten or reused:
- If a critical bug is detected immediately after release, yank the release on PyPI via the web interface.
- Publish a patch release (e.g. `v0.1.1`) following the normal checklist above.
