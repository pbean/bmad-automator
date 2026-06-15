"""Thin `gh` / `git` wrapper for the PR-creation + CI-watch merge pipeline (Phase 4).

Mirrors `verify._git`: every call shells out and raises `GitError` on failure
(except `watch_checks`, where a failing CI run is an expected return value, not an
error). Kept in its own module so the worktree pipeline can *degrade gracefully*:
the engine calls `gh_available()` and `has_remote()` first, and when either is
False it falls back to a local merge with a journal warning instead of a PR.

`gh` has no `-C <dir>` flag (unlike git), so every gh call runs with `cwd=repo`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .verify import GIT_TIMEOUT_S, GitError

# Default refresh interval (s) for `gh pr checks --watch`. gh's own default is 10.
DEFAULT_WATCH_INTERVAL_S = 30
# CI can run for a long time; bound the watch so a hung run eventually returns
# control to the engine rather than blocking the process forever (~6h).
WATCH_TIMEOUT_S = 6 * 60 * 60

# scm.merge_strategy -> the matching `gh pr merge` flag. gh has no fast-forward
# -only mode, so "ff" maps to --rebase (the closest linear-history outcome).
_MERGE_FLAG = {"merge": "--merge", "squash": "--squash", "ff": "--rebase"}


def _gh(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_S) -> tuple[int, str]:
    proc = subprocess.run(
        ["gh", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_S) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def gh_available() -> bool:
    """True when the `gh` CLI is on PATH. Authentication is the operator's
    responsibility; a `gh` present but unauthenticated surfaces as a GitError
    from the first real call, which the engine turns into a local-merge fallback."""
    return shutil.which("gh") is not None


def has_remote(repo: Path) -> bool:
    """True when `repo` has at least one git remote configured (i.e. there is
    somewhere to push a branch and open a PR)."""
    rc, out = _git(repo, "remote")
    return rc == 0 and out != ""


def push_branch(repo: Path, branch: str, remote: str = "origin") -> None:
    """Push `branch` to `remote`, setting upstream so `gh pr create` can find it."""
    rc, out = _git(repo, "push", "-u", remote, branch, timeout=GIT_TIMEOUT_S)
    if rc != 0:
        raise GitError(f"git push {remote} {branch} failed in {repo}: {out}")


def create_pr(repo: Path, *, base: str, head: str, title: str, body: str) -> str:
    """Open a PR merging `head` into `base`; return the PR URL gh prints."""
    rc, out = _gh(
        repo,
        "pr",
        "create",
        "--base",
        base,
        "--head",
        head,
        "--title",
        title,
        "--body",
        body,
    )
    if rc != 0:
        raise GitError(f"gh pr create ({head} -> {base}) failed in {repo}: {out}")
    # gh prints the PR URL on its own line; take the last non-empty line to skip
    # any informational chatter it may emit first.
    url = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
    return url


def watch_checks(
    repo: Path,
    pr: str,
    *,
    required: bool = False,
    fail_fast: bool = True,
    interval: int = DEFAULT_WATCH_INTERVAL_S,
) -> bool:
    """Block on `gh pr checks <pr> --watch` until the PR's checks finish.

    Returns True when every (required, if `required`) check passed, False when
    any failed. A failed run is a normal control-flow outcome here, not a
    GitError. A timeout or a gh invocation error (no checks configured, bad pr)
    is treated as a watch failure (False) so the engine leaves the PR for a human
    rather than crashing the run.
    """
    args = ["pr", "checks", pr, "--watch", "--interval", str(interval)]
    if fail_fast:
        args.append("--fail-fast")
    if required:
        args.append("--required")
    try:
        rc, _ = _gh(repo, *args, timeout=WATCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False
    return rc == 0


def merge_pr(
    repo: Path,
    pr: str,
    *,
    strategy: str = "merge",
    auto: bool = False,
    delete_branch: bool = True,
) -> None:
    """Merge `pr` via `gh pr merge`.

    `auto=True` enables GitHub auto-merge (merges once required checks pass,
    returning immediately); `auto=False` merges now (the checks must already be
    green). `strategy` selects --merge/--squash/--rebase; `delete_branch` adds
    --delete-branch to remove the head branch (local + remote) after the merge.
    """
    flag = _MERGE_FLAG.get(strategy)
    if flag is None:
        raise GitError(f"unknown merge strategy for PR merge: {strategy!r}")
    args = ["pr", "merge", pr, flag]
    if auto:
        args.append("--auto")
    if delete_branch:
        args.append("--delete-branch")
    rc, out = _gh(repo, *args)
    if rc != 0:
        raise GitError(f"gh pr merge {pr} ({strategy}, auto={auto}) failed in {repo}: {out}")
