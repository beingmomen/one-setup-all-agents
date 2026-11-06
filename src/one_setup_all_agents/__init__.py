"""one-setup-all-agents: one setup for all your AI coding agents.

A shared agent config for Claude Code, Codex, OpenCode and Antigravity.
Write your config once, every agent gets the same setup, and any change can be
undone with one command.

Status: pre-alpha. Phase 0 (decisions and skeleton) only; nothing is usable yet.

Layout:
    cli.py       command-line entry point (`all-agents init|sync|status|undo`)
    model.py     shared data types: Env, Scope, Detection, Paths, Op kinds
    paths.py     where the user's source config and the engine state live, per OS
    engine/      the only code that writes: plan, apply, backup, journal, undo
    adapters/    one module per tool; adapters detect, read and plan, never write
"""

__version__ = "0.0.0.dev0"
