"""The runner: start a command after expanding variable placeholders in its arguments.

    all-agents _run [--derive NAME=template]... -- <command> [args...]

Why it exists: Codex and Antigravity do not expand ${VAR} in their own configs, and a header that carries
a secret must not be written into a config file or an argument list. The generated entry for such a
server is a small stdio bridge:

    all-agents _run --derive MCP_HDR_1=Bearer{sp}{{TOKEN}} -- npx -y mcp-remote <url> --header "Authorization:{{$MCP_HDR_1}}"

  * --derive NAME=template sets one more variable in the CHILD'S ENVIRONMENT only, computed from variables of
    the environment (`{{VAR}}` or `${VAR}`; `{sp}` is one space, so the argument never needs a space). A variable
    that is nowhere is a refusal that names it. On Windows the User environment is read too, for a variable the
    process does not have.
  * `{{VAR}}` / `${VAR}` in the child's arguments are replaced from that environment. `{{$VAR}}` becomes the
    literal `${VAR}`, left for the child to expand itself, so the value never appears on a command line.
  * stdin/stdout/stderr pass straight through to the child (MCP stdio). On Windows the child runs in a Job
    Object with KILL_ON_JOB_CLOSE, so it dies with the runner. On other systems there is no equivalent and the
    child may outlive the runner (unverified, see docs/decisions/0007).

Exit codes: 0..N the child's exit code, 2 refusal or bad arguments, 126 could not start, 127 command not found.
Never prints a value: only variable NAMES.
"""

import os
import re
import shutil
import subprocess
import sys

ENV_NAME = re.compile(
    r"^[A-Z][A-Z0-9_]{0,63}$"
)  # a pasted token (lowercase, long) cannot pass as a "name"
VAR_REF = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}|\{\{\$([A-Za-z_][A-Za-z0-9_]*)\}\}"
)


class RunnerError(Exception):
    """A refusal or a bad argument. The message never contains a secret value."""

    exit_code = 2


class CommandNotFound(RunnerError):
    exit_code = 127

    def __init__(self, name):
        super().__init__(f"command not found on PATH: {name}")


def read_user_env(name):
    """Windows User environment (HKCU\\Environment), for a variable the process env lacks. None elsewhere or if absent."""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value, _ = winreg.QueryValueEx(k, name)
        return value if isinstance(value, str) and value else None
    except OSError:
        return None


def get_value(name, env):
    v = env.get(name)
    return v if v else read_user_env(name)


def expand_args(args, env):
    """Replace ${NAME} and {{NAME}} using env. A missing or empty name is a refusal (naming only the variable).
    {{$NAME}} becomes the literal ${NAME}, left for the child to expand from its own environment."""
    out = []
    for a in args:

        def sub(m):
            if m.group(3):
                return "${" + m.group(3) + "}"
            name = m.group(1) or m.group(2)
            v = get_value(name, env)
            if not v:
                raise RunnerError(f"argument references {name} which is not set")
            return v

        out.append(VAR_REF.sub(sub, a))
    return out


def parse_derive(specs):
    """['NAME=template', ...] -> [(NAME, template)]. {{$VAR}} is not allowed in a template: a derived value is
    computed now, not passed through."""
    out, seen = [], set()
    for spec in specs or []:
        name, eq, tmpl = spec.partition("=")
        if not eq or not ENV_NAME.match(name):
            raise RunnerError("--derive needs NAME=template with an UPPER_CASE variable name")
        if name in seen:
            raise RunnerError(f"--derive {name} given twice")
        if not tmpl:
            raise RunnerError(f"--derive {name} has an empty template")
        if any(m.group(3) for m in VAR_REF.finditer(tmpl)):
            raise RunnerError(
                f"--derive {name}: use {{{{VAR}}}} or ${{VAR}} (not {{{{$VAR}}}}) inside a derive template"
            )
        seen.add(name)
        out.append((name, tmpl))
    return out


def apply_derive(env, derived):
    """Set each derived variable in `env` (the child's environment, never its arguments). Returns the names."""
    lookup = dict(env)
    names = []
    for name, tmpl in derived:
        value = expand_args([tmpl.replace("{sp}", " ")], lookup)[
            0
        ]  # {sp} first: a value is never rewritten
        for k in [k for k in env if k.upper() == name and k != name]:
            del env[k]
        env[name] = value
        names.append(name)
    return names


# ---------------------------------------------------------------- Windows Job Object
_JOB_KEEPALIVE = []  # the handle must live until the runner exits


def create_kill_on_close_job():
    """A Job Object with KILL_ON_JOB_CLOSE. Returns (kernel32, handle) or None (not Windows / API failure)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.GetCurrentProcess.restype = wintypes.HANDLE

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

        class EXT(ctypes.Structure):
            _fields_ = [
                ("Basic", BASIC),
                ("Io", IOC),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXT()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):  # ExtendedLimitInformation
            return None
        _JOB_KEEPALIVE.append(job)
        return k32, job
    except Exception:  # noqa: BLE001 - the job is a convenience; a launch without it is still correct
        return None


def run_child(argv, env, cwd=None):
    """Start argv with inherited stdio inside the kill-on-close job (Windows). Returns the Popen."""
    exe = shutil.which(argv[0], path=env.get("PATH") or env.get("Path"))
    if not exe:
        raise CommandNotFound(argv[0])
    job = create_kill_on_close_job()
    if job:  # put the runner itself in the job: the child inherits membership with no race
        k32, handle = job
        k32.AssignProcessToJobObject(handle, k32.GetCurrentProcess())
    return subprocess.Popen([exe, *argv[1:]], env=env, cwd=cwd)


def parse(argv):
    if "--" not in argv:
        raise RunnerError(
            "usage: _run [--cwd DIR] [--derive NAME=template]... -- <command> [args...]"
        )
    i = argv.index("--")
    head, cmd = argv[:i], argv[i + 1 :]
    if not cmd:
        raise RunnerError("no command after --")
    import argparse

    ap = argparse.ArgumentParser(prog="_run", add_help=False)
    ap.add_argument("--cwd")
    ap.add_argument("--derive", action="append", default=[])
    ns, extra = ap.parse_known_args(head)
    if extra:
        raise RunnerError(f"unknown option(s) before --: {' '.join(extra)}")
    ns.derived = parse_derive(ns.derive)
    return ns, cmd


def main(argv):
    try:
        ns, cmd = parse(list(argv))
        env = dict(os.environ)
        apply_derive(env, ns.derived)
        child_argv = [cmd[0], *expand_args(cmd[1:], env)]
        return run_child(child_argv, env, cwd=ns.cwd).wait()
    except RunnerError as e:
        print(f"all-agents _run: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        print(f"all-agents _run: could not start the command: {type(e).__name__}", file=sys.stderr)
        return 126
