"""Tool detection: which tools are installed, where, and which version (docs/decisions/0014).

`detect_all` is what `init` and `status` use. For every adapter it:

1. calls `adapter.detect(env)`, which only looks (PATH of `env.environ`, known install folders, the
   tool's config root) and never starts a program (docs/decisions/0008);
2. when a binary was found, asks the adapter how to read its version (`adapter.version_probe`, a
   pure declaration) and runs that one command through an injectable runner;
3. returns one `Detection` per adapter, with `tool`, `label`, `config_found` and `version` filled.

This module is the ONLY place that starts the version command. The command is run with a list of
arguments (no shell), no stdin, captured output, a short timeout and the environment of `Env`
only. A binary that fails, hangs or has vanished gives `version=None` and a warning for that tool;
nothing here raises for the whole run.

It names no tool: everything tool specific lives in the adapters.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePath

from .adapters import all_adapters
from .adapters.base import ToolAdapter
from .model import (
    SOURCE_KNOWN_LOCATION,
    SOURCE_OVERRIDE,
    SOURCE_PATH,
    STATUS_ABSENT,
    STATUS_CONFIG_ONLY,
    Detection,
    Env,
)

#: Seconds a version command may run before it is killed (docs/decisions/0014).
DEFAULT_PROBE_TIMEOUT = 8.0

#: At most this many bytes of each output stream are read. A version line is a few dozen.
MAX_OUTPUT_BYTES = 4096


@dataclass(frozen=True)
class ProbeResult:
    """What a version command did: its exit status and (bounded, decoded) output."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


#: A runner: `(argv, environ, timeout seconds) -> ProbeResult`. It must raise `TimeoutError` when
#: the command did not finish in time, and may raise `OSError` (for example `FileNotFoundError`)
#: when it cannot be started. Tests pass an in-process fake, so no tool ever runs in a test.
ProbeRunner = Callable[[Sequence[str], Mapping[str, str], float], ProbeResult]


def _read_stream(stream) -> str:
    stream.seek(0)
    return stream.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")


def _kill(process: subprocess.Popen) -> None:
    """Kill a version command that did not finish (the whole process group on POSIX)."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def default_probe_runner(
    argv: Sequence[str], environ: Mapping[str, str], timeout: float
) -> ProbeResult:
    """Run `argv` for real: no shell, no stdin, `environ` as the whole environment.

    Output goes to temporary files, not pipes, so a grandchild that keeps a pipe open (a launcher
    shim that starts the real program) cannot make this wait past the timeout.
    """
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            list(argv),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=dict(environ),
            **kwargs,
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            raise TimeoutError(f"no answer within {timeout:g} seconds") from None
        return ProbeResult(process.returncode, _read_stream(out), _read_stream(err))


def _read_version(
    adapter: ToolAdapter, env: Env, binary: Path, runner: ProbeRunner, timeout: float
) -> tuple[str | None, str | None]:
    """(version, warning). At most one is set; neither is when the adapter declares no probe."""
    try:
        probe = adapter.version_probe(env, binary)
    except Exception as exc:
        return None, f"the version command could not be prepared ({type(exc).__name__})"
    if probe is None:
        return None, None
    try:
        result = runner(probe.argv, env.environ, timeout)
    except TimeoutError:
        return None, f"the version command did not answer within {timeout:g} seconds; killed"
    except FileNotFoundError:
        return None, "the binary was found but could not be started (it is gone or not runnable)"
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return None, f"the version command could not be started ({reason})"
    except Exception as exc:
        return None, f"the version command failed to run ({type(exc).__name__})"
    if result.returncode != 0:
        return None, f"the version command exited with status {result.returncode}"
    for text in (result.stdout, result.stderr):
        try:
            version = probe.parse(text)
        except Exception:
            version = None
        if version:
            return version, None
    return None, "no version number was found in the output of the version command"


def _exists(path: Path) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def detect_one(
    adapter: ToolAdapter,
    env: Env,
    *,
    probe: ProbeRunner | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    versions: bool = True,
) -> Detection:
    """`adapter.detect` plus the version, for one adapter. Never raises."""
    runner = probe or default_probe_runner
    try:
        found = adapter.detect(env)
    except Exception as exc:
        return Detection(
            installed=False,
            tool=adapter.id,
            label=adapter.label,
            warnings=(f"detection failed ({type(exc).__name__}: {exc})",),
        )
    warnings = list(found.warnings)
    version = found.version
    if versions and found.binary is not None and version is None:
        version, warning = _read_version(adapter, env, Path(found.binary), runner, timeout)
        if warning:
            warnings.append(warning)
    return replace(
        found,
        tool=adapter.id,
        label=adapter.label,
        version=version,
        warnings=tuple(warnings),
        config_found=tuple(root for root in found.config_roots if _exists(root)),
    )


def detect_all(
    env: Env,
    adapters: Sequence[ToolAdapter] | None = None,
    *,
    probe: ProbeRunner | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    versions: bool = True,
) -> list[Detection]:
    """One `Detection` per adapter, in registry order (all registered adapters by default).

    `probe` replaces the runner of the version command (tests pass a fake). `versions=False`
    skips the version commands altogether, for a caller that only needs presence.
    """
    chosen = list(all_adapters() if adapters is None else adapters)
    return [
        detect_one(adapter, env, probe=probe, timeout=timeout, versions=versions)
        for adapter in chosen
    ]


# -- plain-text report ---------------------------------------------------------------------------

_SOURCE_TEXT = {
    SOURCE_OVERRIDE: "set by the tool's override variable",
    SOURCE_PATH: "found on PATH",
    SOURCE_KNOWN_LOCATION: "found in a known install folder",
}
_STATUS_TEXT = {
    STATUS_CONFIG_ONLY: "config only (no binary found)",
    STATUS_ABSENT: "not installed",
}


def _home_pattern(home: Path, platform: str) -> re.Pattern | None:
    spellings = {str(home), PurePath(home).as_posix()}
    spellings.discard("")
    if not spellings or spellings <= {".", "/", "\\"}:
        return None  # nothing meaningful to mask
    flags = re.IGNORECASE if platform == "windows" else 0
    ordered = sorted(spellings, key=len, reverse=True)
    return re.compile("|".join(re.escape(s) for s in ordered), flags)


def mask_home(text: str, env: Env) -> str:
    """Replace the home folder, in either separator style, by `~` (so a report is safe to paste)."""
    pattern = _home_pattern(Path(env.home), env.platform)
    return pattern.sub("~", text) if pattern else text


def _show_path(path: Path, env: Env) -> str:
    """A path for the report: `~/rest` under the home, else as is."""
    home = Path(env.home)
    try:
        relative = PurePath(path).relative_to(home)
    except ValueError:
        return mask_home(str(path), env)
    return "~" if not relative.parts else "~/" + relative.as_posix()


def render_detections(detections: Sequence[Detection], env: Env) -> str:
    """A plain-text report of `detect_all`. Paths under the home are shown as `~`, and no
    environment value is printed (only what the detections hold)."""
    lines = ["Detected tools:"]
    for found in detections:
        name = f"{found.label} ({found.tool})" if found.label else found.tool or "unknown tool"
        lines.append(f"  {name}: {_STATUS_TEXT.get(found.status, found.status)}")
        if found.binary is not None:
            how = _SOURCE_TEXT.get(found.binary_source or "", "")
            suffix = f" ({how})" if how else ""
            lines.append(f"    binary:  {_show_path(found.binary, env)}{suffix}")
            lines.append(f"    version: {found.version or 'unknown'}")
        for root in found.config_roots:
            state = "found" if root in found.config_found else "not found"
            lines.append(f"    config:  {_show_path(root, env)} ({state})")
        for warning in found.warnings:
            lines.append(f"    warning: {mask_home(warning, env)}")
    return "\n".join(lines) + "\n"
