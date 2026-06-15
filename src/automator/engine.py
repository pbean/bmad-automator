"""The deterministic control loop.

Per story: dev session -> artifact verification -> bounded review loop
-> deterministic verify commands -> orchestrator commit. The engine never
edits sprint-status.yaml or spec files; it re-reads them to decide and
verify. All creative work happens inside disposable adapter sessions.
"""

from __future__ import annotations

import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import gates, scm_github, verify
from .adapters.base import CodingCLIAdapter, SessionResult, SessionSpec
from .bmadconfig import ProjectPaths
from .escalation import (
    Action,
    critical_escalations,
    decide_dev,
    decide_review_session,
    preference_escalations,
)
from .journal import Journal, save_state
from .model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_SPEC_APPROVAL,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
)
from .policy import Policy
from .runs import kill_session
from .sprintstatus import load as load_sprint_status
from .sprintstatus import next_actionable
from .statemachine import advance
from .workspace import (
    UnitWorkspace,
    Workspace,
    close_unit_workspace,
    discard_worktree,
    open_unit_workspace,
)


class RunPaused(Exception):
    def __init__(self, reason: str, stage: str, story_key: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.story_key = story_key


class RunStopped(Exception):
    """Raised from the SIGTERM/SIGINT handler to unwind the loop cleanly so the
    engine can mark the run `stopped` (a deliberate stop, distinct from a
    crash) and tear down its in-flight agent session."""


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    done: int
    deferred: int
    escalated: int
    paused: bool
    paused_reason: str
    total_tokens: int

    def render(self) -> str:
        lines = [
            f"run {self.run_id}: {self.done} done, {self.deferred} deferred, "
            f"{self.escalated} escalated, {self.total_tokens:,} tokens"
        ]
        if self.paused:
            lines.append(f"PAUSED: {self.paused_reason}")
        return "\n".join(lines)


class Engine:
    # The engine that installed the process-wide stop handlers; nested
    # auto-sweep runs (same process) see it set and let RunStopped propagate up.
    _stop_signals_owner: "Engine | None" = None

    def __init__(
        self,
        paths: ProjectPaths,
        policy: Policy,
        adapter: CodingCLIAdapter,
        run_dir: Path,
        journal: Journal,
        state: RunState,
        max_stories: int | None = None,
        epic_filter: int | None = None,
        story_filter: str | None = None,
        review_adapter: CodingCLIAdapter | None = None,
        sweep_factory: Callable[[str], None] | None = None,
    ):
        self.paths = paths
        # where code+git work + artifact reads happen. isolation="none" (today's
        # only mode) → the repo root in place; Phase 3 swaps in per-unit worktrees.
        self.workspace = Workspace.default(paths)
        self.policy = policy
        self.adapters = {
            "dev": adapter,
            "review": review_adapter if review_adapter is not None else adapter,
        }
        self.run_dir = run_dir
        self.journal = journal
        self.state = state
        self.max_stories = max_stories
        self.epic_filter = epic_filter
        self.story_filter = story_filter
        # spawns a child deferred-work sweep run (injected by the CLI to
        # avoid an engine -> sweep import cycle); see _maybe_auto_sweep
        self.sweep_factory = sweep_factory
        # stop-signal bookkeeping (see run())
        self._owns_signals = False
        self._stopping = False
        self._prev_handlers: dict[int, object] = {}

    # ------------------------------------------------------------- top level

    def run(self) -> RunSummary:
        self._install_stop_signals()
        self._ensure_target_branch()
        try:
            try:
                self._loop()
                self.state.finished = True
                self.journal.append("run-complete")
                # tear down the run's agent session now that it finished. Only
                # the outermost engine owns this (nested auto-sweep never sets
                # _owns_signals); stop already kills it, and pause/interrupt
                # leave it for resume to reuse.
                if self._owns_signals and self.policy.adapter.cleanup_session_on_finish:
                    kill_session(self.state.run_id)
            except RunPaused as pause:
                self.state.paused_reason = pause.reason
                self.state.paused_stage = pause.stage
                self.state.paused_story_key = pause.story_key
                self.journal.append(
                    "run-paused",
                    reason=pause.reason,
                    stage=pause.stage,
                    story_key=pause.story_key,
                )
            except RunStopped:
                # the loop was interrupted inside adapter.run(), so the agent
                # window is still live — tear the whole run session down.
                kill_session(self.state.run_id)
                if not self._owns_signals:
                    raise  # nested auto-sweep: let the owner record the stop
                self.state.stopped = True
                self.journal.append("run-stop")
            finally:
                self._save()
        finally:
            self._restore_stop_signals()
        summary = self.summary()
        gates.notify(self.policy, self.run_dir, "bmad-auto run finished", summary.render())
        return summary

    # ---------------------------------------------------------- stop signals

    def _install_stop_signals(self) -> None:
        """Make SIGTERM/SIGINT unwind the loop as a RunStopped. Only the
        outermost engine in the process owns the handlers (nested auto-sweep
        runs let the exception propagate up to it); install is best-effort and
        silently skipped off the main thread (signal.signal raises there)."""
        if Engine._stop_signals_owner is not None:
            return

        def handler(signum, frame):  # noqa: ANN001 - stdlib signal signature
            if self._stopping:
                return  # already unwinding; don't re-raise during teardown
            self._stopping = True
            raise RunStopped()

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                self._prev_handlers[sig] = signal.signal(sig, handler)
        except ValueError:
            # not on the main thread — cannot install; degrade to no handler
            self._restore_stop_signals()
            return
        self._owns_signals = True
        Engine._stop_signals_owner = self

    def _restore_stop_signals(self) -> None:
        for sig, prev in self._prev_handlers.items():
            try:
                signal.signal(sig, prev)
            except (ValueError, TypeError):
                pass
        self._prev_handlers.clear()
        if Engine._stop_signals_owner is self:
            Engine._stop_signals_owner = None
        self._owns_signals = False

    # ----------------------------------------------------- worktree isolation

    @property
    def _isolated(self) -> bool:
        return self.policy.scm.isolation == "worktree"

    def _ensure_target_branch(self) -> None:
        """Resolve (once, at run start) the branch every unit merges back into.

        No-op unless isolation=worktree. Default target is the branch checked out
        now; a configured target is created if missing and checked out in the
        main repo (merges land on whatever the main repo has checked out, and a
        unit worktree must never check out the target itself). Pinned in state so
        resume keeps targeting the same branch."""
        if not self._isolated or self.state.target_branch:
            return
        repo = self.paths.repo_root
        configured = self.policy.scm.target_branch.strip()
        if configured:
            if not verify.branch_exists(repo, configured):
                verify.create_branch(repo, configured, "HEAD")
                self.journal.append("target-branch-created", branch=configured)
            if verify.current_branch(repo) != configured:
                verify.checkout_branch(repo, configured)
                self.journal.append("target-branch-checkout", branch=configured)
            self.state.target_branch = configured
        else:
            self.state.target_branch = verify.current_branch(repo)
        self.journal.append("target-branch", branch=self.state.target_branch)
        self._save()

    def _run_isolated(self, task: StoryTask, drive: Callable[[StoryTask], None]) -> None:
        """Run one unit's `drive` body in a fresh per-unit worktree, then merge
        it back into the target branch. `drive` either returns (DONE/DEFERRED →
        integrate) or raises RunPaused (spec-approval gate / escalation → leave
        the worktree mounted for resume/inspection, integration skipped)."""
        try:
            unit = open_unit_workspace(
                self.paths.repo_root,
                self.paths,
                self.state.run_id,
                task.story_key,
                self.state.target_branch,
                self.policy.scm.branch_per,
            )
        except verify.GitError as e:
            # could not mount a worktree (e.g. branch_per=run with a kept-failed
            # unit still holding the shared branch). Defer this unit rather than
            # crash the whole run; the operator can free the branch and re-run.
            task.defer_reason = f"could not open worktree: {e}"
            task.phase = Phase.DEFERRED  # deliberate: no legal move from PENDING
            self.journal.append("worktree-open-failed", story_key=task.story_key, error=str(e))
            gates.notify(
                self.policy, self.run_dir, f"worktree open failed: {task.story_key}", str(e)
            )
            self._save()
            return
        task.worktree_path = str(unit.path)
        task.branch = unit.branch
        self.journal.append(
            "worktree-opened", story_key=task.story_key, branch=unit.branch, path=str(unit.path)
        )
        self._save()
        prev = self.workspace
        self.workspace = unit.workspace
        try:
            drive(task)
        finally:
            self.workspace = prev
        # reached only on a normal return (DONE or DEFERRED); a RunPaused from the
        # spec gate or an escalation propagates past here, leaving the worktree up.
        self._integrate_unit(task, unit)

    def _integrate_unit(self, task: StoryTask, unit: UnitWorkspace) -> None:
        scm = self.policy.scm
        if task.phase == Phase.DONE:
            # create_pr lands the work via a GitHub PR; without it (or when GitHub
            # is unavailable) we merge the unit branch into target locally.
            if scm.create_pr and self._github_ready():
                self._integrate_via_pr(task, unit)
            else:
                if scm.create_pr:
                    self.journal.append(
                        "pr-degraded",
                        story_key=task.story_key,
                        reason="no git remote or gh CLI; merging locally",
                    )
                self._merge_local(task, unit)
        else:  # DEFERRED — capture the diff, keep or drop per keep_failed
            patch = close_unit_workspace(
                unit,
                success=False,
                keep_failed=scm.keep_failed,
                run_dir=self.run_dir,
                unit_key=task.story_key,
                delete_branch=scm.delete_branch,
            )
            self.journal.append(
                "unit-closed",
                story_key=task.story_key,
                branch=unit.branch,
                kept=scm.keep_failed,
                patch=str(patch) if patch else None,
            )

    def _merge_local(self, task: StoryTask, unit: UnitWorkspace) -> None:
        """Merge a DONE unit's branch into the target branch from the main repo."""
        scm = self.policy.scm
        try:
            verify.merge_branch(
                self.paths.repo_root,
                unit.branch,
                strategy=scm.merge_strategy,
                message=self._merge_message(task),
            )
        except verify.GitError as e:
            # conflict against the target: keep the branch for manual merge.
            # The unit committed cleanly (phase is already DONE, which has no
            # legal transition), so set ESCALATED directly rather than via
            # advance(), then pause for the operator.
            close_unit_workspace(
                unit,
                success=False,
                keep_failed=True,
                run_dir=self.run_dir,
                unit_key=task.story_key,
                delete_branch=False,
            )
            reason = f"merge of {unit.branch} into {self.state.target_branch} failed: {e}"
            self._escalate_unit(task, reason)
        self.journal.append(
            "unit-merged",
            story_key=task.story_key,
            branch=unit.branch,
            target=self.state.target_branch,
        )
        close_unit_workspace(
            unit,
            success=True,
            keep_failed=scm.keep_failed,
            run_dir=self.run_dir,
            unit_key=task.story_key,
            delete_branch=scm.delete_branch,
        )

    def _github_ready(self) -> bool:
        """True when a PR can actually be opened — `gh` on PATH and a remote
        configured. Either missing degrades the PR path to a local merge."""
        return scm_github.gh_available() and scm_github.has_remote(self.paths.repo_root)

    def _integrate_via_pr(self, task: StoryTask, unit: UnitWorkspace) -> None:
        """Push the unit branch and open a PR into the target branch, then honor
        ci_merge (off → leave for a human, watch → block on CI then merge, auto →
        enable GitHub auto-merge). Any push/create failure degrades to a local
        merge so the work still lands rather than stranding a branch."""
        scm = self.policy.scm
        repo = self.paths.repo_root
        try:
            scm_github.push_branch(repo, unit.branch)
            pr = scm_github.create_pr(
                repo,
                base=self.state.target_branch,
                head=unit.branch,
                title=self._pr_title(task),
                body=self._pr_body(task),
            )
        except verify.GitError as e:
            self.journal.append("pr-failed-local-fallback", story_key=task.story_key, error=str(e))
            self._merge_local(task, unit)
            return
        task.pr_url = pr
        self.journal.append(
            "pr-created",
            story_key=task.story_key,
            branch=unit.branch,
            target=self.state.target_branch,
            pr=pr,
        )
        self._save()

        if scm.ci_merge == "auto":
            self._release_pr_worktree(unit)
            try:
                scm_github.merge_pr(
                    repo,
                    pr,
                    strategy=scm.merge_strategy,
                    auto=True,
                    delete_branch=scm.delete_branch,
                )
                self.journal.append("pr-auto-merge-enabled", story_key=task.story_key, pr=pr)
            except verify.GitError as e:
                # auto-merge couldn't be enabled (e.g. no required checks): leave
                # the PR open for a human rather than failing the unit.
                self.journal.append(
                    "pr-merge-failed", story_key=task.story_key, pr=pr, error=str(e)
                )
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"PR auto-merge not enabled: {task.story_key}",
                    f"{pr} — enable manually: {e}",
                )
            return

        if scm.ci_merge == "watch":
            self.journal.append("ci-watch-start", story_key=task.story_key, pr=pr)
            passed = scm_github.watch_checks(repo, pr, fail_fast=True)
            self.journal.append("ci-watch-finish", story_key=task.story_key, pr=pr, passed=passed)
            if not passed:
                self._escalate_pr(
                    task,
                    unit,
                    f"CI checks failed for {task.story_key} on {pr}; PR left open",
                    worktree_mounted=True,
                )
                return
            self._release_pr_worktree(unit)
            try:
                scm_github.merge_pr(
                    repo,
                    pr,
                    strategy=scm.merge_strategy,
                    auto=False,
                    delete_branch=scm.delete_branch,
                )
                self.journal.append("pr-merged", story_key=task.story_key, pr=pr)
            except verify.GitError as e:
                self._escalate_pr(
                    task,
                    unit,
                    f"PR merge after green CI failed for {task.story_key}: {e}",
                    worktree_mounted=False,
                )
            return

        # ci_merge == "off": leave the PR for a human to review and merge.
        self._release_pr_worktree(unit)
        self.journal.append("pr-left-open", story_key=task.story_key, pr=pr)

    def _release_pr_worktree(self, unit: UnitWorkspace) -> None:
        """Remove the local worktree once its branch is pushed — the work now
        lives on the remote branch / PR. The local branch ref is left in place so
        `gh pr merge --delete-branch` can remove it at merge time (gh refuses to
        delete a branch still checked out in a worktree, so the worktree goes
        first)."""
        try:
            verify.worktree_remove(unit.repo_root, unit.path, force=False)
        except verify.GitError:
            verify.worktree_remove(unit.repo_root, unit.path, force=True)

    def _escalate_pr(
        self, task: StoryTask, unit: UnitWorkspace, reason: str, *, worktree_mounted: bool
    ) -> None:
        """Escalate a DONE unit whose PR couldn't be merged (CI red, or a merge
        failure after green CI). The branch + PR stay on the remote for the
        operator; a still-mounted worktree is kept/dropped per keep_failed."""
        if worktree_mounted:
            close_unit_workspace(
                unit,
                success=False,
                keep_failed=self.policy.scm.keep_failed,
                run_dir=self.run_dir,
                unit_key=task.story_key,
                delete_branch=False,
            )
        self._escalate_unit(task, reason)

    def _escalate_unit(self, task: StoryTask, reason: str) -> None:
        """Mark a DONE unit ESCALATED, notify, and pause the run. DONE has no
        legal transition, so the phase is set directly rather than via advance()."""
        task.phase = Phase.ESCALATED
        self.journal.append("story-escalated", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"CRITICAL escalation: {task.story_key}",
            f"{reason} — resolve, then `bmad-auto resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(reason, PAUSE_ESCALATION, task.story_key)

    def _merge_message(self, task: StoryTask) -> str:
        return f"Merge {task.branch} into {self.state.target_branch} (bmad-auto)"

    def _pr_title(self, task: StoryTask) -> str:
        return f"bmad-auto: {task.story_key}"

    def _pr_body(self, task: StoryTask) -> str:
        return (
            f"Automated change for `{task.story_key}` (run `{self.state.run_id}`).\n\n"
            f"Branch `{task.branch}` → `{self.state.target_branch}`."
        )

    def _reopen_unit(self, task: StoryTask) -> UnitWorkspace:
        """Reconstruct the UnitWorkspace for an in-flight unit on resume, from
        the worktree path + branch persisted on the task."""
        wt = Path(task.worktree_path)
        return UnitWorkspace(
            workspace=Workspace(root=wt, paths=self.paths.rebased(wt)),
            repo_root=self.paths.repo_root,
            branch=task.branch,
            path=wt,
            baseline=task.baseline_commit or "",
        )

    def summary(self) -> RunSummary:
        tasks = self.state.tasks.values()
        return RunSummary(
            run_id=self.state.run_id,
            done=sum(1 for t in tasks if t.phase == Phase.DONE),
            deferred=sum(1 for t in tasks if t.phase == Phase.DEFERRED),
            escalated=sum(1 for t in tasks if t.phase == Phase.ESCALATED),
            paused=self.state.paused,
            paused_reason=self.state.paused_reason or "",
            total_tokens=sum(t.tokens.total for t in tasks),
        )

    def _loop(self) -> None:
        self._finish_inflight()
        started = 0
        while True:
            if self.max_stories is not None and started >= self.max_stories:
                self.journal.append("max-stories-reached", count=started)
                return
            story = self._pick_next()
            if story is None:
                self._maybe_auto_sweep("run-end", "run-end")
                return
            if self.state.current_epic is not None and story.epic != self.state.current_epic:
                self._epic_boundary(self.state.current_epic, story.epic)
            self.state.current_epic = story.epic
            task = StoryTask(story_key=story.key, epic=story.epic)
            self.state.tasks[story.key] = task
            self.journal.append("story-start", story_key=story.key)
            self._save()
            started += 1
            self._run_story(task)

    def _pick_next(self):
        ss = load_sprint_status(self.paths.sprint_status)
        if ss.unknown_keys:
            self.journal.append("sprint-status-unknown-keys", keys=list(ss.unknown_keys))
        skip = set(self.state.tasks)  # anything this run already touched
        while True:
            story = next_actionable(ss, skip)
            if story is None:
                return None
            if self.epic_filter is not None and story.epic != self.epic_filter:
                skip.add(story.key)
                continue
            if self.story_filter is not None and story.key != self.story_filter:
                skip.add(story.key)
                continue
            return story

    def _reset_to(self, baseline: str) -> None:
        """Roll back code changes, preserving run state and BMAD artifacts
        (sprint-status etc. may be untracked in young projects — `git clean`
        must never eat them)."""
        keep = [".automator"]
        for artifact_dir in (
            self.workspace.paths.implementation_artifacts,
            self.workspace.paths.planning_artifacts,
        ):
            try:
                keep.append(str(artifact_dir.relative_to(self.workspace.root)))
            except ValueError:
                pass  # artifacts configured outside the repo; nothing to protect
        verify.reset_hard(self.workspace.root, baseline, keep=tuple(keep))

    def _finish_inflight(self) -> None:
        """Complete or roll back tasks interrupted by a pause or crash."""
        for task in list(self.state.tasks.values()):
            if task.terminal:
                continue
            isolated = self._isolated and task.worktree_path
            if task.phase == Phase.DEV_VERIFY and task.spec_file:
                # paused at the spec-approval gate: dev verified, review pending
                self.journal.append("resume-review", story_key=task.story_key)
                if isolated:
                    unit = self._reopen_unit(task)
                    prev = self.workspace
                    self.workspace = unit.workspace
                    try:
                        self._review_and_commit(task)
                    finally:
                        self.workspace = prev
                    self._integrate_unit(task, unit)
                else:
                    self._review_and_commit(task)
            else:
                self.journal.append(
                    "resume-restart", story_key=task.story_key, phase=str(task.phase)
                )
                if isolated:
                    # drop the half-built worktree; _run_story mounts a fresh one
                    discard_worktree(self.paths.repo_root, task.worktree_path, task.branch)
                    task.worktree_path = ""
                    task.branch = ""
                elif task.baseline_commit:
                    self._reset_to(task.baseline_commit)
                task.phase = Phase.PENDING  # deliberate reset, not a normal transition
                self._save()
                self._run_story(task)

    # ------------------------------------------------------------- per story

    def _run_story(self, task: StoryTask) -> None:
        if self._isolated:
            self._run_isolated(task, self._drive_story)
        else:
            self._drive_story(task)

    def _drive_story(self, task: StoryTask) -> None:
        if not self._dev_phase(task):
            return
        if gates.pause_after_spec(self.policy):
            gates.notify(
                self.policy,
                self.run_dir,
                f"spec ready for approval: {task.story_key}",
                f"review {task.spec_file}, then `bmad-auto resume {self.state.run_id}`",
            )
            raise RunPaused(
                f"awaiting spec approval for {task.story_key}",
                PAUSE_SPEC_APPROVAL,
                task.story_key,
            )
        self._review_and_commit(task)

    def _dev_phase(self, task: StoryTask) -> bool:
        task.baseline_commit = verify.rev_parse_head(self.workspace.root)
        feedback: Path | None = None
        while True:
            task.attempt += 1
            advance(task, Phase.DEV_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="dev",
                prompt=self._dev_prompt(task, feedback),
                seq=task.attempt,
            )
            advance(task, Phase.DEV_VERIFY)
            outcome = None
            if result.status == "completed":
                outcome = self._verify_dev_artifacts(task, result.result_json)
                if outcome.ok:
                    # deterministic gates run here too: a broken build must not
                    # reach the (far more expensive) review loop
                    outcome = verify.verify_commands_outcome(self.policy, self.workspace.root)
            decision = decide_dev(task, result, outcome, self.policy)
            self.journal.append(
                "dev-decision",
                story_key=task.story_key,
                attempt=task.attempt,
                session_status=result.status,
                action=str(decision.action),
                reason=decision.reason,
            )
            self._save()
            if decision.action == Action.PROCEED:
                return True
            if decision.action == Action.RETRY:
                if outcome is not None and outcome.fixable:
                    # work exists and the failure is concrete: keep the tree,
                    # hand the failing output to a repair session
                    feedback = self._write_feedback(task, decision.reason)
                else:
                    feedback = None
                    self._reset_to(task.baseline_commit)
                continue
            if decision.action == Action.DEFER:
                self._defer(task, decision.reason)
                return False
            self._escalate(task, decision.reason)

    def _review_and_commit(self, task: StoryTask) -> None:
        if not self.policy.review.enabled:
            self._skip_review_and_commit(task)
            return
        clean = False
        while task.review_cycle < self.policy.limits.max_review_cycles:
            task.review_cycle += 1
            advance(task, Phase.REVIEW_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="review",
                prompt=self._review_prompt(task),
                seq=task.review_cycle,
            )
            advance(task, Phase.REVIEW_VERIFY)
            self._save()
            decision = decide_review_session(task, result, self.policy)
            if decision.action == Action.PAUSE:
                self._escalate(task, decision.reason)
            if decision.action == Action.DEFER:
                self._defer(task, decision.reason)
                return
            if decision.action == Action.RETRY:
                self.journal.append(
                    "review-retry", story_key=task.story_key, reason=decision.reason
                )
                continue

            rj = result.result_json or {}
            for pref in preference_escalations(rj):
                self.journal.append("preference-escalation", story_key=task.story_key, **pref)
            self.journal.append(
                "review-result",
                story_key=task.story_key,
                cycle=task.review_cycle,
                clean=bool(rj.get("clean")),
                patched=rj.get("patched", 0),
                deferred=rj.get("deferred", 0),
                dismissed=rj.get("dismissed", 0),
            )
            if rj.get("clean"):
                outcome = self._verify_review(task)
                if outcome.ok:
                    clean = True
                    break
                self.journal.append(
                    "review-verify-failed",
                    story_key=task.story_key,
                    reason=outcome.reason,
                )
                if outcome.fixable and task.review_cycle < self.policy.limits.max_review_cycles:
                    # failing verify commands are dev work, not review work: a
                    # re-review of the same tree cannot make them pass. Repair
                    # with the failing output as feedback, then re-review.
                    if not self._fix_phase(task, outcome.reason):
                        self._defer(task, "verify commands kept failing after clean review")
                        return
                continue
            # not clean: patches were applied; loop runs a fresh review of the new tree

        if not clean:
            self._defer(task, "review did not converge to clean within budget")
            return

        self._commit(task)

    def _skip_review_and_commit(self, task: StoryTask) -> None:
        """review.enabled = false: the dev session ran quick-dev's own internal
        triple-review and finalized the story to done. No separate review
        session runs — validate the deterministic gates (verify commands,
        spec/sprint = done) and commit, repairing once if verify is fixable."""
        self.journal.append("review-skipped", story_key=task.story_key)
        outcome = self._verify_review(task)
        if not outcome.ok and outcome.fixable and self._fix_phase(task, outcome.reason):
            outcome = self._verify_review(task)
        if not outcome.ok:
            self._defer(task, f"verify failed with review disabled: {outcome.reason}")
            return
        self._commit(task)

    def _commit(self, task: StoryTask) -> None:
        advance(task, Phase.COMMITTING)
        self._save()
        try:
            task.commit_sha = verify.commit_story(self.workspace.root, self._commit_message(task))
        except verify.GitError as e:
            self._escalate(task, f"commit failed: {e}")
        advance(task, Phase.DONE)
        self.journal.append("story-done", story_key=task.story_key, commit=task.commit_sha)
        self._save()
        weighted = task.tokens.weighted_total(self.policy.limits.cache_read_weight)
        if weighted > self.policy.limits.max_tokens_per_story:
            self.journal.append(
                "token-budget-exceeded",
                story_key=task.story_key,
                weighted=weighted,
                total=task.tokens.total,
            )

    # ----------------------------------------------------- override seams
    # SweepEngine reuses the dev/review pipeline for deferred-work bundles by
    # overriding these (bundles have no sprint-status entry).

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        return verify.verify_dev(
            task, self.workspace.paths, result_json, review_enabled=self.policy.review.enabled
        )

    def _verify_review(self, task: StoryTask):
        return verify.verify_review(task, self.workspace.paths, self.policy)

    def _review_prompt(self, task: StoryTask) -> str:
        return f"/bmad-auto-review {task.spec_file}"

    def _commit_message(self, task: StoryTask) -> str:
        if self.policy.review.enabled:
            return f"story {task.story_key}: implemented and reviewed via bmad-auto"
        return f"story {task.story_key}: implemented via bmad-auto"

    # ------------------------------------------------------------- helpers

    def _run_session(self, task: StoryTask, role: str, prompt: str, seq: int) -> SessionResult:
        task_id = f"{task.story_key}-{role}-{seq}"
        adapter = self.adapters[role]
        cfg = self.policy.adapter.resolved(role)
        env = {
            "BMAD_AUTO_MODE": "1",
            "BMAD_AUTO_RUN_DIR": str(self.run_dir),
            "BMAD_AUTO_TASK_ID": task_id,
            "BMAD_AUTO_STORY_KEY": task.story_key,
        }
        if role == "dev" and not self.policy.review.enabled:
            # tells the dev skill to run its own internal triple-review and
            # finalize straight to done (the orchestrator runs no review session)
            env["BMAD_AUTO_SKIP_REVIEW"] = "1"
        spec = SessionSpec(
            task_id=task_id,
            role=role,
            prompt=prompt,
            cwd=self.workspace.root,
            env=env,
            model=cfg.model,
            timeout_s=self.policy.limits.session_timeout_min * 60,
        )
        self.journal.set_active_log(task_id)
        self.journal.append("session-start", task_id=task_id, role=role, prompt=prompt)
        result = adapter.run(spec)
        usage = adapter.read_usage(result)
        task.record_session(
            SessionRecord(
                task_id=task_id,
                role=role,
                status=result.status,
                session_id=result.session_id,
                transcript_path=result.transcript_path,
                usage=usage,
            )
        )
        self.journal.append(
            "session-end",
            task_id=task_id,
            status=result.status,
            tokens=usage.total if usage else None,
        )
        return result

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        prompt = f"/bmad-auto-dev {task.story_key}"
        if feedback is not None:
            prompt += f" --feedback {feedback}"
        return prompt

    def _write_feedback(self, task: StoryTask, reason: str) -> Path:
        """Persist a verification failure where the next session can read it —
        deterministic evidence must reach the LLM, not just the journal."""
        path = self.run_dir / "feedback" / f"{task.story_key}-{len(task.sessions)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Verification feedback: {task.story_key}\n\n"
            "The previous session's work failed deterministic verification.\n"
            "Repair the working tree so verification passes, without violating\n"
            "the spec's frozen intent.\n\n"
            f"```\n{reason}\n```\n",
            encoding="utf-8",
        )
        return path

    def _fix_phase(self, task: StoryTask, reason: str) -> bool:
        """Feedback-driven repair after a clean review whose verify commands
        failed. Consumes the story's dev-attempt budget; returns True once the
        commands pass so the review loop can re-review the repaired tree."""
        while task.attempt < self.policy.limits.max_dev_attempts:
            task.attempt += 1
            feedback = self._write_feedback(task, reason)
            advance(task, Phase.DEV_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="dev",
                prompt=self._dev_prompt(task, feedback),
                seq=task.attempt,
            )
            advance(task, Phase.DEV_VERIFY)
            crits = critical_escalations(result.result_json)
            if crits:
                details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
                self._escalate(task, f"CRITICAL escalation from fix session: {details}")
            outcome = None
            if result.status == "completed":
                outcome = verify.verify_commands_outcome(self.policy, self.workspace.root)
                if not outcome.ok:
                    reason = outcome.reason
            ok = outcome is not None and outcome.ok
            self.journal.append(
                "fix-decision",
                story_key=task.story_key,
                attempt=task.attempt,
                session_status=result.status,
                ok=ok,
            )
            self._save()
            if ok:
                return True
        return False

    def _defer(self, task: StoryTask, reason: str) -> None:
        task.defer_reason = reason
        advance(task, Phase.DEFERRED)
        if self._isolated:
            # the failed work lives in the unit's worktree; the diff is captured
            # and the worktree kept/dropped by _integrate_unit. Don't touch the
            # tree here (no reset into the main repo — there's nothing to undo).
            self.journal.append("story-deferred", story_key=task.story_key, reason=reason)
            gates.notify(self.policy, self.run_dir, f"story deferred: {task.story_key}", reason)
            self._save()
            return
        if task.baseline_commit:
            self._stash_deferred_artifacts(task)
            deferred_work = self.workspace.paths.deferred_work
            snapshot = (
                deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
            )
            self._reset_to(task.baseline_commit)
            # reset reverts tracked deferred-work.md edits; restore review-found
            # defer entries — they are real knowledge worth keeping
            if snapshot is not None:
                current = (
                    deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
                )
                if current != snapshot:
                    deferred_work.parent.mkdir(parents=True, exist_ok=True)
                    deferred_work.write_text(snapshot, encoding="utf-8")
        self.journal.append("story-deferred", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"story deferred: {task.story_key}",
            reason,
        )
        self._save()

    def _stash_deferred_artifacts(self, task: StoryTask) -> None:
        """Move the deferred story's spec out of the artifacts dir into the run
        dir: a leftover in-review spec would confuse the next attempt, but the
        work in it is worth keeping for the human."""
        if not task.spec_file:
            return
        spec_path = Path(task.spec_file)
        if not spec_path.is_file():
            return
        dest = self.run_dir / "deferred" / task.story_key
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(spec_path), str(dest / spec_path.name))
        self.journal.append(
            "deferred-artifacts-stashed",
            story_key=task.story_key,
            stashed_to=str(dest / spec_path.name),
        )

    def _escalate(self, task: StoryTask, reason: str) -> None:
        advance(task, Phase.ESCALATED)
        self.journal.append("story-escalated", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"CRITICAL escalation: {task.story_key}",
            f"{reason} — resolve, then `bmad-auto resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(reason, PAUSE_ESCALATION, task.story_key)

    def _maybe_auto_sweep(self, kind: str, trigger: str) -> None:
        """Run a child deferred-work sweep when policy [sweep].auto matches.
        The child is its own resumable run; a paused or failed child is
        journaled + notified but never interrupts this run."""
        if self.policy.sweep.auto != kind or self.sweep_factory is None:
            return
        if trigger in self.state.sweeps_triggered:
            return  # already fired before a pause/resume of this run
        self.state.sweeps_triggered.append(trigger)
        self._save()
        try:
            clean = verify.worktree_clean(self.workspace.root)
        except verify.GitError:
            clean = False
        if not clean:
            # should not happen at these call sites (everything committed or
            # reset); refuse rather than sweep on top of stray changes
            self.journal.append("sweep-auto-skipped-dirty", trigger=trigger)
            return
        self.journal.append("sweep-auto-trigger", trigger=trigger)
        try:
            self.sweep_factory(trigger)
            self.journal.append("sweep-auto-finished", trigger=trigger)
        except Exception as e:  # noqa: BLE001 — child must never break the parent
            self.journal.append("sweep-auto-failed", trigger=trigger, error=str(e))
            gates.notify(self.policy, self.run_dir, "auto sweep failed", f"{trigger}: {e}")

    def _epic_boundary(self, finished_epic: int, next_epic: int) -> None:
        self.journal.append("epic-boundary", finished=finished_epic, next=next_epic)
        self._maybe_auto_sweep("per-epic", f"epic-{finished_epic}")
        if self.policy.gates.retrospective != "never":
            gates.notify(
                self.policy,
                self.run_dir,
                f"epic {finished_epic} stories complete",
                "retrospective suggested: run /bmad-retrospective when convenient",
            )
        if gates.pause_at_epic_boundary(self.policy):
            self.state.current_epic = next_epic  # don't re-trigger this gate on resume
            self._save()
            raise RunPaused(
                f"epic {finished_epic} boundary — `bmad-auto resume {self.state.run_id}` "
                f"to continue with epic {next_epic}",
                PAUSE_EPIC_BOUNDARY,
            )

    def _save(self) -> None:
        save_state(self.run_dir, self.state)
