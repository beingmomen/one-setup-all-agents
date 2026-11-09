"""one-setup-all-agents: one setup for all your AI coding agents.

A shared agent config for every supported tool (the registry in `adapters/` lists them).
Write your config once, every agent gets the same setup, and any change can be
undone with one command.

Status: pre-alpha. The four public commands (`init`, `sync`, `status`, `undo`) are implemented.


Layout:
    cli.py       command-line entry point (`all-agents init|sync|status|undo`)
    model.py     shared data types: Env, Scope, Detection, Paths, Op kinds
    paths.py     where the user's source config and the engine state live, per OS
    _core/       internal helpers (JSONC/TOML formatting, secret masking, runner)
    engine/      the only code that writes: plan, apply, backup, journal, undo
    adapters/    one module per tool; adapters detect, read and plan, never write
"""

__version__ = "0.0.0.dev0"
