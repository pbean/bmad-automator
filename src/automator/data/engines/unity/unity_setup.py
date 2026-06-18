#!/usr/bin/env python3
"""per_worktree setup for the bmad-auto Unity engine plugin.

Runs once per unit, right after bmad-auto cuts the unit's git worktree and before
the readiness gate, to turn that fresh checkout into a usable Unity project with
its own managed Editor:

  1. Symlink ``<worktree>/Library`` to a dedicated per-worktree persistent cache
     (NOT the operator's live Library — sharing one Library across two Editors
     corrupts it). The cache survives across runs to amortize the first import;
     a Unity Accelerator further amortizes it across worktrees if available.
  2. Write the worktree's MCP client config (``.mcp.json``). The IvanMurzak CLI
     derives a deterministic MCP port from the *project path*, so a worktree at a
     different path automatically gets its own port — its Editor self-isolates
     from the operator's main Editor with no manual port wiring.
  3. Launch a Unity Editor on the worktree path (detached). The plugin's
     ``ready_cmd`` (the engine's readiness gate) then blocks until that Editor +
     MCP are up, so this script only needs to start it, not wait for it.

The MCP tool *skill* files are not written here — they are gitignored and copied
in from the main repo by the plugin's ``seed_globs`` (``.claude/skills/*``).

Verified against unity-mcp-cli v0.81.0 (`open` / `setup-mcp` / `close` key off the
project path with auto port detection). The exact flags move between releases —
override engine.worktree_setup_cmd in a project-local plugin if yours differ.

Only the IvanMurzak MCP is wired for a managed per-worktree launch. CoplayDev runs
one shared :8080 server multiplexing Editors by instance id, so its per-worktree
story differs — point engine.worktree_setup_cmd at your own script for it.

Env (injected by the engine, all optional except the worktree):
  BMAD_AUTO_WORKTREE         the unit's worktree (the Unity project to manage)
  BMAD_AUTO_REPO_ROOT        main repo root (parent of the Library cache)
  BMAD_AUTO_ENGINE_MCP       ivanmurzak | coplaydev            (default ivanmurzak)
  BMAD_AUTO_UNITY_PATH       explicit Editor binary            (skips Unity Hub discovery)
  BMAD_AUTO_ENGINE_AGENT     agent id for setup-mcp            (default claude-code)
  BMAD_AUTO_UNITY_LIBRARY_CACHE  override the Library cache root
  UNITY_MCP_CLI              IvanMurzak CLI binary             (default unity-mcp-cli)

Exit 0 = the worktree Editor is launching; non-zero = setup failed (the engine
defers the unit and notifies).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# how long to watch a freshly-launched Editor for an immediate crash before
# treating "still running" as a successful launch (the ready gate does the wait).
_LAUNCH_GRACE_SEC = 15


def _worktree() -> Path | None:
    wt = os.environ.get("BMAD_AUTO_WORKTREE")
    return Path(wt) if wt else None


def _cli() -> str:
    return os.environ.get("UNITY_MCP_CLI", "unity-mcp-cli")


def _library_cache(worktree: Path) -> Path:
    """A persistent, per-worktree Library cache dir (keyed by worktree name).

    Lives under the repo's gitignored .automator/cache/ (init adds the ignore);
    relocate with BMAD_AUTO_UNITY_LIBRARY_CACHE (e.g. onto a faster disk)."""
    override = os.environ.get("BMAD_AUTO_UNITY_LIBRARY_CACHE")
    if override:
        root = Path(override)
    else:
        repo = Path(os.environ.get("BMAD_AUTO_REPO_ROOT", worktree.parent))
        root = repo / ".automator" / "cache" / "unity" / "Library"
    return root / worktree.name


def _link_library(worktree: Path) -> None:
    """Point <worktree>/Library at the per-worktree cache, unless a real Library
    is already present (committed/tracked) — never clobber the operator's tree."""
    link = worktree / "Library"
    if link.is_symlink() or link.exists():
        return  # already linked, or a real Library is checked out — leave it
    cache = _library_cache(worktree)
    cache.mkdir(parents=True, exist_ok=True)
    link.symlink_to(cache, target_is_directory=True)
    print(f"unity_setup: Library -> {cache}", file=sys.stderr)


def _setup_ivanmurzak(worktree: Path) -> int:
    cli = _cli()
    if shutil.which(cli) is None:
        print(
            f"unity_setup: {cli!r} not found on PATH; install the Unity-MCP CLI, set "
            "UNITY_MCP_CLI, or override engine.worktree_setup_cmd",
            file=sys.stderr,
        )
        return 2

    # 1. worktree MCP client config (deterministic per-path port; no Editor needed)
    agent = os.environ.get("BMAD_AUTO_ENGINE_AGENT", "claude-code")
    cfg = subprocess.run(
        [cli, "setup-mcp", agent, str(worktree)],
        capture_output=True,
        text=True,
    )
    if cfg.returncode != 0:
        sys.stderr.write(cfg.stdout + cfg.stderr)
        print("unity_setup: setup-mcp failed", file=sys.stderr)
        return cfg.returncode

    # 2. launch the worktree's Editor, detached, and watch briefly for a crash.
    cmd = [cli, "open", str(worktree)]
    editor = os.environ.get("BMAD_AUTO_UNITY_PATH")
    if editor:
        cmd += ["--editor-path", editor]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach so it outlives this hook
        )
    except OSError as exc:
        print(f"unity_setup: could not launch Editor: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + _LAUNCH_GRACE_SEC
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            # `open` either daemonizes the Editor and exits 0 (fine), or failed.
            if rc != 0:
                print(f"unity_setup: 'open' exited {rc} during launch", file=sys.stderr)
            return rc
        time.sleep(1)
    # still running after the grace window: an attached launch we leave detached;
    # the readiness gate confirms the Editor + MCP actually came up.
    print("unity_setup: Editor launching (readiness gate will confirm)", file=sys.stderr)
    return 0


def main() -> int:
    worktree = _worktree()
    if worktree is None:
        print("unity_setup: BMAD_AUTO_WORKTREE is not set", file=sys.stderr)
        return 2
    mcp = (os.environ.get("BMAD_AUTO_ENGINE_MCP") or "ivanmurzak").strip().lower()
    _link_library(worktree)
    if mcp == "ivanmurzak":
        return _setup_ivanmurzak(worktree)
    if mcp == "coplaydev":
        print(
            "unity_setup: per_worktree managed-launch is not wired for the CoplayDev "
            "MCP (one shared :8080 server multiplexes Editors). Override "
            "engine.worktree_setup_cmd with a CoplayDev launcher, or use editor_mode "
            "= 'shared'.",
            file=sys.stderr,
        )
        return 2
    print(
        f"unity_setup: unknown BMAD_AUTO_ENGINE_MCP={mcp!r} (expected ivanmurzak|coplaydev)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
