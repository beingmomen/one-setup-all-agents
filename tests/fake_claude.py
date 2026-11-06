"""Test stand-in for `claude mcp add-json/remove -s user|local`. Edits <USERPROFILE>/.claude.json like the real CLI does.

Used by the apply tests through --claude-bin (a .py path is started with the current interpreter). Fake data only."""

import json
import os
import sys
from pathlib import Path


def git_root(cwd):
    p = Path(cwd)
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return str(d)
    return str(p)


def main(argv):
    if len(argv) < 3 or argv[0] != "mcp":
        print("fake claude: unsupported", file=sys.stderr)
        return 2
    path = Path(os.environ["USERPROFILE"]) / ".claude.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    scope = argv[argv.index("-s") + 1] if "-s" in argv else "local"
    if scope == "local":
        key = git_root(os.getcwd()).replace(
            "\\", "/"
        )  # like the real CLI: the local scope is keyed by the GIT ROOT, not the cwd
        holder = data.setdefault("projects", {}).setdefault(key, {}).setdefault("mcpServers", {})
    else:
        holder = data.setdefault("mcpServers", {})
    verb, name = argv[1], argv[2]
    if verb == "add-json":
        if name in holder:
            print(f"MCP server {name} already exists in {scope} config", file=sys.stderr)
            return 1
        holder[name] = json.loads(argv[3])
    elif verb == "remove":
        if name not in holder:
            print(f"No MCP server found with name: {name}", file=sys.stderr)
            return 1
        del holder[name]
    else:
        return 2
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
