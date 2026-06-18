#!/usr/bin/env python3
"""per_worktree teardown for the bmad-auto Unity engine plugin.

Runs once per unit when the worktree is finished — on success, on a deferral, and
on a pause/escalation — so the unit's managed Unity Editor never outlives its
worktree. Best effort: the engine logs a non-zero exit but the unit's outcome
stands (it does not re-defer a done/paused unit just because Editor-quit failed).

  1. Gracefully quit the worktree's Editor (`unity-mcp-cli close`, then --force).
  2. Drop the ``<worktree>/Library`` symlink this unit's setup created (leaving the
     persistent cache it pointed at intact for the next run). A real (non-symlink)
     Library is left untouched.

Verified against unity-mcp-cli v0.81.0 (`close <path>` keys off the project path).
Only the IvanMurzak MCP launches a managed per-worktree Editor, so only it is quit
here; for CoplayDev (shared :8080 server) override engine.worktree_teardown_cmd.

Env (injected by the engine):
  BMAD_AUTO_WORKTREE     the worktree whose Editor to quit
  BMAD_AUTO_ENGINE_MCP   ivanmurzak | coplaydev               (default ivanmurzak)
  UNITY_MCP_CLI          IvanMurzak CLI binary                (default unity-mcp-cli)
  BMAD_AUTO_UNITY_CLOSE_TIMEOUT  polite-quit seconds before --force (default 30)

Exit 0 = Editor quit + symlink dropped; non-zero = something failed (logged).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _worktree() -> Path | None:
    wt = os.environ.get("BMAD_AUTO_WORKTREE")
    return Path(wt) if wt else None


def _cli() -> str:
    return os.environ.get("UNITY_MCP_CLI", "unity-mcp-cli")


def _drop_library_symlink(worktree: Path) -> None:
    link = worktree / "Library"
    if link.is_symlink():
        try:
            link.unlink()
            print("unity_teardown: dropped Library symlink", file=sys.stderr)
        except OSError as exc:  # best effort
            print(f"unity_teardown: could not drop Library symlink: {exc}", file=sys.stderr)


def _close_ivanmurzak(worktree: Path) -> int:
    cli = _cli()
    if shutil.which(cli) is None:
        # nothing to quit against; not fatal — just drop the symlink below.
        print(f"unity_teardown: {cli!r} not on PATH; skipping Editor quit", file=sys.stderr)
        return 0
    timeout = os.environ.get("BMAD_AUTO_UNITY_CLOSE_TIMEOUT", "30")
    proc = subprocess.run(
        [cli, "close", str(worktree), "--timeout", timeout, "--force"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        print(f"unity_teardown: 'close' exited {proc.returncode}", file=sys.stderr)
    return proc.returncode


def main() -> int:
    worktree = _worktree()
    if worktree is None:
        print("unity_teardown: BMAD_AUTO_WORKTREE is not set", file=sys.stderr)
        return 2
    mcp = (os.environ.get("BMAD_AUTO_ENGINE_MCP") or "ivanmurzak").strip().lower()
    rc = 0
    if mcp == "ivanmurzak":
        rc = _close_ivanmurzak(worktree)
    elif mcp == "coplaydev":
        print(
            "unity_teardown: CoplayDev per_worktree teardown is not wired; override "
            "engine.worktree_teardown_cmd if you launched a managed Editor for it.",
            file=sys.stderr,
        )
    else:
        print(
            f"unity_teardown: unknown BMAD_AUTO_ENGINE_MCP={mcp!r} "
            "(expected ivanmurzak|coplaydev)",
            file=sys.stderr,
        )
        rc = 2
    _drop_library_symlink(worktree)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
