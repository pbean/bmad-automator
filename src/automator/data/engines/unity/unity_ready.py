#!/usr/bin/env python3
"""Readiness gate for the bmad-auto Unity engine plugin.

Blocks until the Unity Editor + MCP bridge are ready to accept commands, so a
dev/sweep session never starts against a half-open Editor. The engine runs this
as the plugin's ``ready_cmd`` and injects its configuration via the environment.

Supported MCP servers (BMAD_AUTO_ENGINE_MCP):
  - ivanmurzak : shells out to the Unity-MCP CLI's ``wait-for-ready``, passing an
                 explicit ``--timeout`` (the CLI's own default is only 120s) and
                 retrying so a fast connection-refused against a not-yet-listening
                 Editor doesn't abort the gate early.
  - coplaydev  : connectivity check against the MCP HTTP server (best effort —
                 see note below; override engine.ready_cmd for a stricter probe).

Cold-launch grace: a per_worktree Editor is launched fresh per unit and needs
time to start (and import) before it can answer at all. The gate waits
BMAD_AUTO_ENGINE_READY_GRACE seconds before the first probe; ``-1`` (the default)
auto-picks 120s for per_worktree and 0s for shared (a warm, already-open Editor).
The grace counts against the overall readiness budget.

Exit 0 = ready; non-zero = not ready (the engine defers the unit and notifies).

Env knobs (all optional):
  BMAD_AUTO_ENGINE_MCP            ivanmurzak | coplaydev          (default ivanmurzak)
  BMAD_AUTO_ENGINE_EDITOR_MODE   shared | per_worktree           (selects the grace default)
  BMAD_AUTO_WORKTREE             project the Editor has open      (falls back to REPO_ROOT)
  BMAD_AUTO_REPO_ROOT            main repo root
  BMAD_AUTO_ENGINE_READY_TIMEOUT seconds to keep polling          (default 600)
  BMAD_AUTO_ENGINE_READY_GRACE   pre-probe delay seconds; -1=auto (default -1)
  UNITY_MCP_CLI                  IvanMurzak CLI binary            (default unity-mcp-cli)
  UNITY_MCP_URL                  CoplayDev MCP server URL         (default http://localhost:8080)

NOTE: the exact IvanMurzak CLI name/subcommand and CoplayDev readiness endpoint
move between releases — verify against the version installed in your project and
override ``engine.ready_cmd`` in a project-local plugin if they differ.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

# return this many seconds before the engine's outer timeout would fire, so the
# gate yields a clean "not ready" rc rather than being hard-killed mid-probe.
_SAFETY_MARGIN = 10.0
# default pre-probe grace per editor_mode when BMAD_AUTO_ENGINE_READY_GRACE = -1.
_AUTO_GRACE = {"per_worktree": 120.0, "shared": 0.0}


def _project() -> str:
    return os.environ.get("BMAD_AUTO_WORKTREE") or os.environ.get("BMAD_AUTO_REPO_ROOT") or "."


def _timeout() -> float:
    try:
        return float(os.environ.get("BMAD_AUTO_ENGINE_READY_TIMEOUT", "600"))
    except ValueError:
        return 600.0


def _grace() -> float:
    """Seconds to wait before the first probe. -1/unset → per-mode auto default."""
    raw = os.environ.get("BMAD_AUTO_ENGINE_READY_GRACE", "-1").strip()
    try:
        val = float(raw)
    except ValueError:
        val = -1.0
    if val >= 0:
        return val
    mode = (os.environ.get("BMAD_AUTO_ENGINE_EDITOR_MODE") or "shared").strip().lower()
    return _AUTO_GRACE.get(mode, 0.0)


def _ready_ivanmurzak(deadline: float) -> int:
    cli = os.environ.get("UNITY_MCP_CLI", "unity-mcp-cli")
    if shutil.which(cli) is None:
        print(
            f"unity_ready: {cli!r} not found on PATH; install the Unity-MCP CLI, set "
            "UNITY_MCP_CLI, or override engine.ready_cmd",
            file=sys.stderr,
        )
        return 2
    last = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # let the CLI poll for the rest of the budget in one call; if it instead
        # fast-fails (Editor not listening yet) we retry until the deadline.
        cli_timeout_ms = max(1000, int(remaining * 1000))
        try:
            proc = subprocess.run(
                [cli, "wait-for-ready", _project(), "--timeout", str(cli_timeout_ms)],
                timeout=remaining + 5,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            last = "wait-for-ready process timed out"
            break
        if proc.returncode == 0:
            return 0
        last = (proc.stdout + proc.stderr).strip()[-500:]
        if deadline - time.monotonic() <= 0:
            break
        time.sleep(3)  # brief pause before retrying a fast-fail
    print(f"unity_ready: Editor not ready within budget: {last}", file=sys.stderr)
    return 1


def _ready_coplaydev(deadline: float) -> int:
    url = os.environ.get("UNITY_MCP_URL", "http://localhost:8080")
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 8080)
    last = ""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                print(
                    f"unity_ready: connected to CoplayDev MCP at {host}:{port} "
                    "(connectivity check only — not a full Editor-ready probe)",
                    file=sys.stderr,
                )
                return 0
        except OSError as exc:  # server not up yet
            last = str(exc)
            time.sleep(2)
    print(
        f"unity_ready: could not reach CoplayDev MCP at {host}:{port}: {last}",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    mcp = (os.environ.get("BMAD_AUTO_ENGINE_MCP") or "ivanmurzak").strip().lower()
    # the whole gate must finish a hair before the engine's outer timeout fires.
    deadline = time.monotonic() + max(1.0, _timeout() - _SAFETY_MARGIN)
    grace = min(_grace(), max(0.0, deadline - time.monotonic()))
    if grace > 0:
        print(
            f"unity_ready: waiting {grace:.0f}s for the Editor to start before probing",
            file=sys.stderr,
        )
        time.sleep(grace)
    if mcp == "ivanmurzak":
        return _ready_ivanmurzak(deadline)
    if mcp == "coplaydev":
        return _ready_coplaydev(deadline)
    print(
        f"unity_ready: unknown BMAD_AUTO_ENGINE_MCP={mcp!r} (expected ivanmurzak|coplaydev)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
