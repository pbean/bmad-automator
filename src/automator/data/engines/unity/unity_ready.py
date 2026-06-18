#!/usr/bin/env python3
"""Readiness gate for the bmad-auto Unity engine plugin.

Blocks until the Unity Editor + MCP bridge are ready to accept commands, so a
dev/sweep session never starts against a half-open Editor. The engine runs this
as the plugin's ``ready_cmd`` and injects its configuration via the environment.

Supported MCP servers (BMAD_AUTO_ENGINE_MCP):
  - ivanmurzak : shells out to the Unity-MCP CLI's ``wait-for-ready`` (turnkey).
  - coplaydev  : connectivity check against the MCP HTTP server (best effort —
                 see note below; override engine.ready_cmd for a stricter probe).

Exit 0 = ready; non-zero = not ready (the engine defers the unit and notifies).

Env knobs (all optional):
  BMAD_AUTO_ENGINE_MCP            ivanmurzak | coplaydev          (default ivanmurzak)
  BMAD_AUTO_WORKTREE              project the Editor has open      (falls back to REPO_ROOT)
  BMAD_AUTO_REPO_ROOT            main repo root
  BMAD_AUTO_ENGINE_READY_TIMEOUT seconds to keep polling          (default 600)
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


def _project() -> str:
    return os.environ.get("BMAD_AUTO_WORKTREE") or os.environ.get("BMAD_AUTO_REPO_ROOT") or "."


def _timeout() -> float:
    try:
        return float(os.environ.get("BMAD_AUTO_ENGINE_READY_TIMEOUT", "600"))
    except ValueError:
        return 600.0


def _ready_ivanmurzak(deadline: float) -> int:
    cli = os.environ.get("UNITY_MCP_CLI", "unity-mcp-cli")
    if shutil.which(cli) is None:
        print(
            f"unity_ready: {cli!r} not found on PATH; install the Unity-MCP CLI, set "
            "UNITY_MCP_CLI, or override engine.ready_cmd",
            file=sys.stderr,
        )
        return 2
    remaining = max(1, int(deadline - time.monotonic()))
    try:
        proc = subprocess.run(
            [cli, "wait-for-ready", _project()],
            timeout=remaining,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print("unity_ready: wait-for-ready timed out", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
    return proc.returncode


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
    deadline = time.monotonic() + _timeout()
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
