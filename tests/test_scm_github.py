"""Phase 4: scm_github thin `gh`/`git` wrapper.

Subprocess is mocked so these assert the exact argv shapes the engine relies on
(and the rc -> raise / rc -> bool contracts) without needing a real GitHub remote.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from automator import scm_github
from automator.verify import GitError

REPO = Path("/repo")


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_run(monkeypatch, *, returncode=0, stdout="", stderr="", raises=None):
    """Patch scm_github.subprocess.run; return the list it records argv into."""
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        runner.kwargs = kwargs
        if raises is not None:
            raise raises
        return FakeProc(returncode, stdout, stderr)

    runner.kwargs = {}
    monkeypatch.setattr(scm_github.subprocess, "run", runner)
    return calls, runner


# ----------------------------------------------------------------- availability


def test_gh_available_true_when_on_path(monkeypatch):
    monkeypatch.setattr(scm_github.shutil, "which", lambda _: "/usr/bin/gh")
    assert scm_github.gh_available() is True


def test_gh_available_false_when_absent(monkeypatch):
    monkeypatch.setattr(scm_github.shutil, "which", lambda _: None)
    assert scm_github.gh_available() is False


def test_has_remote_true_when_configured(monkeypatch):
    calls, _ = fake_run(monkeypatch, returncode=0, stdout="origin\n")
    assert scm_github.has_remote(REPO) is True
    assert calls[0] == ["git", "-C", str(REPO), "remote"]


def test_has_remote_false_when_empty(monkeypatch):
    fake_run(monkeypatch, returncode=0, stdout="")
    assert scm_github.has_remote(REPO) is False


def test_has_remote_false_on_git_error(monkeypatch):
    fake_run(monkeypatch, returncode=128, stdout="not a repo")
    assert scm_github.has_remote(REPO) is False


# ----------------------------------------------------------------- push


def test_push_branch_argv_and_upstream(monkeypatch):
    calls, _ = fake_run(monkeypatch, returncode=0)
    scm_github.push_branch(REPO, "automator/run/1-1-a")
    assert calls[0] == [
        "git",
        "-C",
        str(REPO),
        "push",
        "-u",
        "origin",
        "automator/run/1-1-a",
    ]


def test_push_branch_raises_on_failure(monkeypatch):
    fake_run(monkeypatch, returncode=1, stderr="rejected")
    with pytest.raises(GitError, match="git push"):
        scm_github.push_branch(REPO, "b")


# ----------------------------------------------------------------- create_pr


def test_create_pr_argv_and_returns_url(monkeypatch):
    calls, runner = fake_run(
        monkeypatch,
        returncode=0,
        stdout="Creating pull request...\nhttps://github.com/o/r/pull/7\n",
    )
    url = scm_github.create_pr(REPO, base="main", head="automator/run/x", title="t", body="b")
    assert url == "https://github.com/o/r/pull/7"
    argv = calls[0]
    assert argv[:3] == ["gh", "pr", "create"]
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--head") + 1] == "automator/run/x"
    assert argv[argv.index("--title") + 1] == "t"
    assert argv[argv.index("--body") + 1] == "b"
    # gh has no -C flag; it must run with cwd=repo
    assert runner.kwargs["cwd"] == str(REPO)


def test_create_pr_raises_on_failure(monkeypatch):
    fake_run(monkeypatch, returncode=1, stderr="no commits between")
    with pytest.raises(GitError, match="gh pr create"):
        scm_github.create_pr(REPO, base="main", head="b", title="t", body="b")


# ----------------------------------------------------------------- watch_checks


def test_watch_checks_passed(monkeypatch):
    calls, _ = fake_run(monkeypatch, returncode=0)
    assert scm_github.watch_checks(REPO, "https://pr/7") is True
    argv = calls[0]
    assert argv[:3] == ["gh", "pr", "checks"]
    assert "https://pr/7" in argv and "--watch" in argv and "--fail-fast" in argv
    assert argv[argv.index("--interval") + 1] == str(scm_github.DEFAULT_WATCH_INTERVAL_S)
    assert "--required" not in argv


def test_watch_checks_failed_returns_false_not_raises(monkeypatch):
    fake_run(monkeypatch, returncode=1, stdout="1 failing")
    assert scm_github.watch_checks(REPO, "pr") is False


def test_watch_checks_required_flag(monkeypatch):
    calls, _ = fake_run(monkeypatch, returncode=0)
    scm_github.watch_checks(REPO, "pr", required=True, fail_fast=False)
    argv = calls[0]
    assert "--required" in argv and "--fail-fast" not in argv


def test_watch_checks_timeout_is_failure(monkeypatch):
    fake_run(monkeypatch, raises=subprocess.TimeoutExpired(cmd="gh", timeout=1))
    assert scm_github.watch_checks(REPO, "pr") is False


# ----------------------------------------------------------------- merge_pr


@pytest.mark.parametrize(
    "strategy,flag",
    [("merge", "--merge"), ("squash", "--squash"), ("ff", "--rebase")],
)
def test_merge_pr_strategy_flag(monkeypatch, strategy, flag):
    calls, _ = fake_run(monkeypatch, returncode=0)
    scm_github.merge_pr(REPO, "pr", strategy=strategy, delete_branch=False)
    argv = calls[0]
    assert argv[:3] == ["gh", "pr", "merge"]
    assert flag in argv and "--auto" not in argv and "--delete-branch" not in argv


def test_merge_pr_auto_and_delete_branch(monkeypatch):
    calls, _ = fake_run(monkeypatch, returncode=0)
    scm_github.merge_pr(REPO, "pr", strategy="squash", auto=True, delete_branch=True)
    argv = calls[0]
    assert "--auto" in argv and "--delete-branch" in argv and "--squash" in argv


def test_merge_pr_unknown_strategy_raises(monkeypatch):
    fake_run(monkeypatch, returncode=0)
    with pytest.raises(GitError, match="unknown merge strategy"):
        scm_github.merge_pr(REPO, "pr", strategy="bogus")


def test_merge_pr_raises_on_failure(monkeypatch):
    fake_run(monkeypatch, returncode=1, stderr="not mergeable")
    with pytest.raises(GitError, match="gh pr merge"):
        scm_github.merge_pr(REPO, "pr", strategy="merge")
