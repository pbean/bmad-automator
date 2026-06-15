"""Phase 4: create_pr — land a DONE unit via a GitHub PR instead of a local
merge, with ci_merge controlling the merge (off / watch / auto) and a graceful
fall back to a local merge when GitHub is unavailable.

`scm_github` is faked (no real remote / gh), so these assert the engine's
branching + call shapes, plus that the worktree/branch lifecycle is correct.
The worktree machinery itself is real (open/close against the conftest sandbox).
"""

from __future__ import annotations

import pytest
from conftest import git  # noqa: F401  (re-exported for parity with worktree tests)
from test_engine_worktree import (
    QUIET,
    commit_sprint,
    journal_kinds,
    make_engine,
    wt_dev_effect,
    wt_review_effect,
)

from automator import scm_github
from automator.model import Phase
from automator.policy import GatesPolicy, Policy, ScmPolicy
from automator.verify import GitError, branch_exists, worktree_list

GH_FUNCS = ("gh_available", "has_remote", "push_branch", "create_pr", "watch_checks", "merge_pr")


class FakeGH:
    """Records every scm_github call the engine makes and lets each test pick the
    branch (gh present? remote? CI green? push/merge error?)."""

    def __init__(self, *, gh=True, remote=True, watch=True, push_error=False, merge_error=False):
        self.calls: list[tuple] = []
        self._gh = gh
        self._remote = remote
        self._watch = watch
        self._push_error = push_error
        self._merge_error = merge_error

    def gh_available(self):
        self.calls.append(("gh_available",))
        return self._gh

    def has_remote(self, repo):
        self.calls.append(("has_remote",))
        return self._remote

    def push_branch(self, repo, branch):
        self.calls.append(("push", branch))
        if self._push_error:
            raise GitError("push rejected")

    def create_pr(self, repo, *, base, head, title, body):
        self.calls.append(("create_pr", base, head))
        return "https://pr/1"

    def watch_checks(self, repo, pr, **kw):
        self.calls.append(("watch", pr))
        return self._watch

    def merge_pr(self, repo, pr, *, strategy, auto, delete_branch):
        self.calls.append(("merge", pr, strategy, auto, delete_branch))
        if self._merge_error:
            raise GitError("not mergeable")

    def names(self):
        return [c[0] for c in self.calls]


def install(monkeypatch, fake):
    for name in GH_FUNCS:
        monkeypatch.setattr(scm_github, name, getattr(fake, name))


def pr_policy(ci_merge="off", **scm):
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree", create_pr=True, ci_merge=ci_merge, **scm),
    )


def happy_script(project, key="1-1-a"):
    return [wt_dev_effect(project, key), wt_review_effect(project, key, clean=True)]


# ----------------------------------------------------------------- ci_merge = off


def test_pr_off_opens_pr_and_leaves_it(project, monkeypatch):
    fake = FakeGH()
    install(monkeypatch, fake)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, happy_script(project), policy=pr_policy("off"))
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE and task.pr_url == "https://pr/1"
    # PR opened against the target branch from the unit branch; never merged.
    assert ("create_pr", "main", "automator/test-run/1-1-a") in fake.calls
    assert "merge" not in fake.names() and "watch" not in fake.names()
    # the work landed on the PR branch, NOT on the local target branch
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()
    # local worktree released, branch kept (it backs the open PR)
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert branch_exists(project.project, "automator/test-run/1-1-a")
    kinds = journal_kinds(engine)
    assert "pr-created" in kinds and "pr-left-open" in kinds


# ----------------------------------------------------------------- ci_merge = auto


def test_pr_auto_enables_automerge(project, monkeypatch):
    fake = FakeGH()
    install(monkeypatch, fake)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, happy_script(project), policy=pr_policy("auto"))
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    # auto-merge enabled with --auto; default merge strategy + delete_branch
    assert ("merge", "https://pr/1", "merge", True, True) in fake.calls
    assert "watch" not in fake.names()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert "pr-auto-merge-enabled" in journal_kinds(engine)


def test_pr_auto_merge_enable_failure_notifies_but_succeeds(project, monkeypatch):
    fake = FakeGH(merge_error=True)
    install(monkeypatch, fake)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, happy_script(project), policy=pr_policy("auto"))
    summary = engine.run()

    # enabling auto-merge failed, but the unit is DONE (PR is open for a human)
    assert summary.done == 1 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DONE
    assert "pr-merge-failed" in journal_kinds(engine)


# ----------------------------------------------------------------- ci_merge = watch


def test_pr_watch_green_merges(project, monkeypatch):
    fake = FakeGH(watch=True)
    install(monkeypatch, fake)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, happy_script(project), policy=pr_policy("watch"))
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    assert ("watch", "https://pr/1") in fake.calls
    # merged now (auto=False) after green CI
    assert ("merge", "https://pr/1", "merge", False, True) in fake.calls
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    kinds = journal_kinds(engine)
    assert "ci-watch-start" in kinds and "ci-watch-finish" in kinds and "pr-merged" in kinds


def test_pr_watch_red_escalates_and_keeps_pr(project, monkeypatch):
    fake = FakeGH(watch=False)
    install(monkeypatch, fake)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, happy_script(project), policy=pr_policy("watch"))
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.ESCALATED
    # CI red → never merged; PR + branch left for a human
    assert "merge" not in fake.names()
    assert branch_exists(project.project, "automator/test-run/1-1-a")
    # keep_failed default → worktree stays mounted for inspection
    assert len(worktree_list(project.project)) == 2
    kinds = journal_kinds(engine)
    assert "ci-watch-finish" in kinds and "story-escalated" in kinds


# ----------------------------------------------------------------- graceful degrade


@pytest.mark.parametrize("missing", ["gh", "remote"])
def test_pr_degrades_to_local_merge_when_github_unavailable(project, monkeypatch, missing):
    fake = FakeGH(gh=(missing != "gh"), remote=(missing != "remote"))
    install(monkeypatch, fake)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, happy_script(project), policy=pr_policy("off"))
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    # no PR was opened; the work was merged locally onto the target branch
    assert "create_pr" not in fake.names() and "push" not in fake.names()
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert "pr-degraded" in journal_kinds(engine)


def test_pr_push_failure_falls_back_to_local_merge(project, monkeypatch):
    fake = FakeGH(push_error=True)
    install(monkeypatch, fake)
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, happy_script(project), policy=pr_policy("off"))
    summary = engine.run()

    assert summary.done == 1 and not summary.paused
    # push failed before a PR could be opened → local merge instead
    assert "create_pr" not in fake.names()
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert "pr-failed-local-fallback" in journal_kinds(engine)
