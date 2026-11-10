# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-10

### Added
- Core CLI commands: `init`, `sync`, `status`, and `undo`.
- Tool adapters for Claude Code, Codex, OpenCode, and Antigravity.
- Unified reversible engine with atomic writes, pre-write backups, and unified journal.
- Real tool verification reports in `docs/verification/` for Windows, Linux (WSL), and macOS CI.
- Automated documentation integrity tests (`test_docs_readme.py`, `test_docs_support_table.py`, `test_docs_security.py`, `test_docs_github_metadata.py`).
- Security policy and vulnerability disclosure process in `SECURITY.md`.
- Automated GitHub Actions release workflow with PyPI Trusted Publishing (OIDC).
