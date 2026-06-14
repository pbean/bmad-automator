"""Detached launching of bmad-auto commands for the TUI.

The TUI never runs engines in-process: run/sweep/resume are launched in new
windows of a dedicated tmux control session (bmad-auto-ctl) so they survive
TUI exit, and the dashboard observes them through run-dir artifacts exactly
like runs started from a plain shell. Fast read-only commands (validate,
--dry-run) are captured instead, for display in a modal.

No textual imports here — everything is subprocess-level and unit-testable.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .. import runs

CTL_SESSION = "bmad-auto-ctl"

# control-session windows are named <kind>-<run_id> (see start_detached)
_CTL_WINDOW_RE = re.compile(r"^(?:run|sweep|resume|resolve)-(.+)$")


class LaunchError(Exception):
    pass


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def session_exists(session: str) -> bool:
    return _tmux("has-session", "-t", f"={session}").returncode == 0


def ctl_window(run_id: str) -> str | None:
    """Name of the control-session window hosting this run's orchestrator
    process (start_detached names windows <kind>-<run_id>), or None when the
    run was not launched from the TUI or the session is gone."""
    if not tmux_available():
        return None
    proc = _tmux("list-windows", "-t", f"={CTL_SESSION}", "-F", "#{window_name}")
    if proc.returncode != 0:
        return None
    for name in proc.stdout.splitlines():
        if name.endswith(f"-{run_id}"):
            return name
    return None


def select_ctl_window(window: str) -> None:
    """Make `window` the control session's current window, so a plain attach
    to the session lands on it (attach-session itself takes no window)."""
    _tmux("select-window", "-t", f"={CTL_SESSION}:{window}")


def select_ctl_window_id(window_id: str) -> None:
    """Like select_ctl_window but by stable tmux window id (@N). Immune to the
    by-name first-match ambiguity in ctl_window and to tmux auto-rename."""
    _tmux("select-window", "-t", window_id)


def current_session() -> str | None:
    """Name of the tmux session this process is running inside, or None when
    not in tmux / tmux is unavailable."""
    proc = _tmux("display-message", "-p", "#{session_name}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def in_ctl_session() -> bool:
    """True when we are running inside a control-session window (i.e. launched
    detached by the TUI), as opposed to a user's own shell."""
    return bool(os.environ.get("TMUX")) and current_session() == CTL_SESSION


def detach_client() -> None:
    """Detach the tmux client viewing the current session, handing the terminal
    back to the user. Processes in the session keep running."""
    _tmux("detach-client")


def kill_ctl_window(run_id: str) -> None:
    """Kill the control-session window hosting this run's orchestrator process,
    if any. A no-op when the run was not launched from the TUI or tmux is gone."""
    window = ctl_window(run_id)
    if window is not None:
        _tmux("kill-window", "-t", f"={CTL_SESSION}:{window}")


def _current_window_id() -> str | None:
    """Stable tmux id (@N) of the window this process runs in, or None when not
    inside tmux / tmux is unavailable."""
    proc = _tmux("display-message", "-p", "#{window_id}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def _ctl_window_candidates(project: Path) -> list[tuple[str, str]]:
    """(window_id, window_name) for parked control-session run windows whose run
    is no longer live — the kill candidates for a prune.

    A `<kind>-<run_id>` window parks on a `read` prompt that never closes on its
    own; it is a candidate once its run has finished/stopped/crashed (or its run
    dir is gone). The current window is excluded so a prune triggered from inside
    the ctl session never targets itself; live runs and the session's own shell
    window are excluded too.
    """
    if not tmux_available() or not session_exists(CTL_SESSION):
        return []
    current = _current_window_id()
    proc = _tmux("list-windows", "-t", f"={CTL_SESSION}", "-F", "#{window_id}\t#{window_name}")
    if proc.returncode != 0:
        return []
    candidates: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        win_id, _, name = line.partition("\t")
        if not win_id or win_id == current:
            continue
        m = _CTL_WINDOW_RE.match(name)
        if m is None:
            continue  # not a run window (e.g. the session's initial shell)
        run_dir = runs.run_dir_for(project, m.group(1))
        if runs.is_run(run_dir) and runs.engine_alive(run_dir):
            continue
        candidates.append((win_id, name))
    return candidates


def prunable_ctl_windows(project: Path) -> list[str]:
    """Names of the control-session windows a prune would close (dry-run view)."""
    return [name for _, name in _ctl_window_candidates(project)]


def prune_ctl_windows(project: Path) -> list[str]:
    """Close parked control-session windows whose run is no longer live; returns
    the names of the windows that were closed (see _ctl_window_candidates)."""
    killed: list[str] = []
    for win_id, name in _ctl_window_candidates(project):
        _tmux("kill-window", "-t", win_id)
        killed.append(name)
    return killed


def _ensure_ctl_session(project: Path) -> None:
    if session_exists(CTL_SESSION):
        return
    proc = _tmux("new-session", "-d", "-s", CTL_SESSION, "-c", str(project))
    if proc.returncode != 0:
        raise LaunchError(f"tmux new-session failed: {proc.stderr.strip()}")


def cli_argv(*tail: str) -> list[str]:
    """`sys.executable -m automator.cli ...` — immune to PATH/venv drift
    inside tmux windows."""
    return [sys.executable, "-m", "automator.cli", *tail]


def start_detached(project: Path, argv_tail: list[str], run_id: str, kind: str) -> str | None:
    """Run a bmad-auto command in a new window of the control session.

    The window runs under explicit `sh -c` (the user's login shell may be
    fish); the trailing `read` keeps the exit status inspectable instead of
    tmux closing the window the moment the process exits.

    Returns the new window's stable tmux id (@N) so callers can target it
    unambiguously (window names collide when several kinds share a run_id).
    """
    if not tmux_available():
        raise LaunchError("tmux not found on PATH")
    _ensure_ctl_session(project)
    inner = shlex.join(cli_argv(*argv_tail))
    shell = f'{inner}; ec=$?; echo "[bmad-auto exited $ec — press enter]"; read -r'
    proc = _tmux(
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}",
        "-t",
        f"={CTL_SESSION}:",
        "-n",
        f"{kind}-{run_id}",
        "-c",
        str(project),
        "sh",
        "-c",
        shell,
    )
    if proc.returncode != 0:
        raise LaunchError(f"tmux new-window failed: {proc.stderr.strip()}")
    return proc.stdout.strip() or None


def start_run_detached(
    project: Path,
    run_id: str,
    *,
    epic: int | None = None,
    story: str | None = None,
    max_stories: int | None = None,
) -> None:
    tail = ["run", "--project", str(project), "--run-id", run_id]
    if epic is not None:
        tail += ["--epic", str(epic)]
    if story:
        tail += ["--story", story]
    if max_stories is not None:
        tail += ["--max-stories", str(max_stories)]
    start_detached(project, tail, run_id, "run")


def start_sweep_detached(
    project: Path,
    run_id: str,
    *,
    no_prompt: bool = False,
    decisions_only: bool = False,
    max_bundles: int | None = None,
) -> None:
    tail = ["sweep", "--project", str(project), "--run-id", run_id]
    if no_prompt:
        tail.append("--no-prompt")
    if decisions_only:
        tail.append("--decisions-only")
    if max_bundles is not None:
        tail += ["--max-bundles", str(max_bundles)]
    start_detached(project, tail, run_id, "sweep")


def resume_detached(project: Path, run_id: str) -> None:
    start_detached(project, ["resume", "--project", str(project), run_id], run_id, "resume")


def start_resolve_detached(project: Path, run_id: str) -> str | None:
    """Run `bmad-auto resolve <run_id>` in a ctl-session window. The caller
    attaches to it: the resolve agent is interactive, and the post-session
    confirm + resume happen in that same window. Returns the window id so the
    caller attaches to exactly this window, not a stale same-run_id window."""
    return start_detached(
        project, ["resolve", "--project", str(project), run_id], run_id, "resolve"
    )


def run_captured(argv_tail: list[str]) -> tuple[int, str]:
    """Run a fast read-only command (validate, --dry-run) and capture its
    combined output for display."""
    proc = subprocess.run(cli_argv(*argv_tail), capture_output=True, text=True)
    out = proc.stdout
    if proc.stderr:
        if out and not out.endswith("\n"):
            out += "\n"
        out += proc.stderr
    return proc.returncode, out
