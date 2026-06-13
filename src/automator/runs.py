"""Run-directory discovery and helpers shared by the CLI and the TUI."""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path

from . import verify
from .journal import STATE_FILE, Journal, load_state, save_state
from .model import PAUSE_ESCALATION, Phase

RUNS_DIR = Path(".automator") / "runs"
ARCHIVE_DIR = Path(".automator") / "archive"
PID_FILE = "engine.pid"

# How long stop_run waits for a signalled engine to exit before falling back to
# marking the run stopped itself.
_STOP_WAIT_S = 10.0
_STOP_POLL_S = 0.1
_TMUX_TIMEOUT_S = 5.0


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def list_run_dirs(project: Path) -> list[Path]:
    """All run dirs containing a state.json, oldest first (run ids sort
    chronologically)."""
    runs = project / RUNS_DIR
    if not runs.is_dir():
        return []
    return sorted(d for d in runs.iterdir() if (d / "state.json").is_file())


def latest_run_dir(project: Path) -> Path | None:
    candidates = list_run_dirs(project)
    return candidates[-1] if candidates else None


def write_pid(run_dir: Path) -> None:
    """Record the engine process pid. Never deleted: a stale pid that fails
    os.kill(pid, 0) is the signal that a run was interrupted."""
    (run_dir / PID_FILE).write_text(str(os.getpid()), encoding="utf-8")


def session_name(run_id: str) -> str:
    return f"bmad-auto-{run_id}"


def attach_target_argv(target: str) -> list[str]:
    """tmux command to reach a target session/window. Inside tmux, nesting is
    refused, so switch this client instead (tmux switch-client -l comes back)."""
    if os.environ.get("TMUX"):
        return ["tmux", "switch-client", "-t", target]
    return ["tmux", "attach", "-t", target]


def attach_argv(run_id: str) -> list[str]:
    return attach_target_argv(f"={session_name(run_id)}")


# ---------------------------------------------------- run resolution / liveness


def run_dir_for(project: Path, run_id: str) -> Path:
    return project / RUNS_DIR / run_id


def is_run(run_dir: Path) -> bool:
    """A directory is a run iff it holds a state.json."""
    return (run_dir / STATE_FILE).is_file()


def read_pid(run_dir: Path) -> int | None:
    """The recorded engine pid, or None when missing/unparseable."""
    try:
        return int((run_dir / PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def engine_alive(run_dir: Path) -> bool:
    """True only when a local engine pid is provably alive (mirrors
    tui.data.liveness, minus the tmux fallback — callers here want a definite
    'is something running' answer, and 'unknown' must not block stop/delete)."""
    pid = read_pid(run_dir)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ----------------------------------------------------------- stop / delete / archive


def kill_session(run_id: str) -> None:
    """Kill a run's agent tmux session (bmad-auto-<id>); a no-op when it is
    already gone or tmux is unavailable."""
    if not shutil.which("tmux"):
        return
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={session_name(run_id)}"],
            capture_output=True,
            timeout=_TMUX_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def stop_run(run_dir: Path) -> bool:
    """Stop a live run. Returns False if it was already finished.

    Prefers the engine's own SIGTERM handler so the engine stays the single
    writer of `stopped` (it marks the run, kills its in-flight agent window, and
    exits). Falls back to an external kill + mark when there is no live engine
    pid, it is a legacy run, or it does not exit in time.
    """
    state = load_state(run_dir)
    if state.finished:
        return False

    pid = read_pid(run_dir)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pid = None  # already gone / not ours — go straight to fallback
    if pid is not None:
        deadline = time.monotonic() + _STOP_WAIT_S
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break  # exited
            time.sleep(_STOP_POLL_S)
        # the engine clears its agent window itself, but kill the session as a
        # backstop in case it died before tearing it down
        kill_session(run_dir.name)
        if load_state(run_dir).stopped:
            return True

    # Fallback: no live engine (or it never confirmed). Mark it stopped here.
    kill_session(run_dir.name)
    state = load_state(run_dir)
    state.stopped = True
    save_state(run_dir, state)
    Journal(run_dir).append("run-stop", pid=pid, fallback=True)
    return True


def delete_run(run_dir: Path) -> None:
    """Permanently remove a run directory. Callers enforce the live guard."""
    shutil.rmtree(run_dir)


def archive_run(project: Path, run_dir: Path) -> Path:
    """Compress a run dir into .automator/archive/<id>.tar.gz and remove the
    original. The tarball is written to a temp path then os.replace'd into place
    so a partial archive never appears. Callers enforce the live guard."""
    archive_dir = project / ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{run_dir.name}.tar.gz"
    tmp = dest.with_suffix(".tar.gz.tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(run_dir, arcname=run_dir.name)
    os.replace(tmp, dest)
    shutil.rmtree(run_dir)
    return dest


# ----------------------------------------------------------- escalation resolution


class RearmError(Exception):
    """The run/story is not in a re-armable escalation state."""


def rearm_escalation(run_dir: Path, story_key: str | None = None) -> str:
    """Re-arm an escalation-paused story so the next resume re-drives it.

    Flips the escalated task out of its terminal ESCALATED phase back to
    PENDING — which makes `_finish_inflight` reset the tree to the story's
    baseline and re-run it (clean rebuild) against the now-corrected frozen
    spec. Deterministically sets that spec's status to `ready-for-dev` so the
    dev session routes straight to implement. Does NOT clear the pause; the
    caller resumes the run separately.

    Returns the re-armed story key. Raises RearmError when the run is not
    paused at the escalation stage or the target story is not escalated.
    """
    state = load_state(run_dir)
    if state.paused_stage != PAUSE_ESCALATION:
        raise RearmError(
            f"run {run_dir.name} is not paused at an escalation "
            f"(stage: {state.paused_stage or 'none'})"
        )
    key = story_key or state.paused_story_key
    if key is None:
        raise RearmError(f"run {run_dir.name} has no escalated story to resolve")
    task = state.tasks.get(key)
    if task is None:
        raise RearmError(f"run {run_dir.name} has no task for story {key}")
    if task.phase != Phase.ESCALATED:
        raise RearmError(f"story {key} is not escalated (phase: {task.phase})")

    # deliberate reset, not a normal state-machine transition (mirrors
    # engine._finish_inflight): a clean re-attempt against the corrected spec.
    task.phase = Phase.PENDING
    task.attempt = 0
    task.review_cycle = 0
    task.defer_reason = None

    if task.spec_file:
        # route /bmad-auto-dev to re-implement (decision table: ready-for-dev
        # -> step-03); independent of the resolve agent having set it.
        verify.set_frontmatter_status(Path(task.spec_file), "ready-for-dev")

    save_state(run_dir, state)
    Journal(run_dir).append("story-escalation-resolved", story_key=key)
    return key
