"""CLI command tests — init policy-derived profiles and per-stage dry-run."""

import argparse
import json

from conftest import install_bmad_config, write_sprint

from automator import cli
from automator import policy as policy_mod

DUAL_CLIENT_POLICY = """\
[adapter]
name = "claude"
model = "opus"
[adapter.review]
name = "codex"
model = "gpt-5-codex"
"""


def _write_policy(project, text=DUAL_CLIENT_POLICY) -> None:
    automator_dir = project / ".automator"
    automator_dir.mkdir(parents=True, exist_ok=True)
    (automator_dir / "policy.toml").write_text(text)


def test_init_registers_hooks_for_all_policy_profiles(tmp_path):
    _write_policy(tmp_path)
    assert cli.main(["init", "--project", str(tmp_path)]) == 0
    assert "Stop" in json.loads((tmp_path / ".claude" / "settings.json").read_text())["hooks"]
    assert "Stop" in json.loads((tmp_path / ".codex" / "hooks.json").read_text())["hooks"]


def test_init_without_policy_defaults_to_claude(tmp_path):
    assert cli.main(["init", "--project", str(tmp_path)]) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not (tmp_path / ".codex").exists()
    # init installs the bundled skills by default
    assert (tmp_path / ".claude" / "skills" / "bmad-auto-dev" / "SKILL.md").is_file()


def test_init_no_skills_flag(tmp_path):
    assert cli.main(["init", "--project", str(tmp_path), "--no-skills"]) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not (tmp_path / ".claude" / "skills").exists()


def test_init_force_skills_flag(tmp_path):
    skill_md = tmp_path / ".claude" / "skills" / "bmad-auto-dev" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("CUSTOM", encoding="utf-8")
    assert cli.main(["init", "--project", str(tmp_path), "--force-skills"]) == 0
    assert skill_md.read_text() != "CUSTOM"


def test_dry_run_renders_per_stage_commands(project, capsys):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    _write_policy(project.project)
    pol = policy_mod.load(project.project / ".automator" / "policy.toml")
    args = argparse.Namespace(epic=None, story=None, max_stories=None)

    assert cli._dry_run(project, pol, args) == 0
    out = capsys.readouterr().out
    dev_line = next(line for line in out.splitlines() if "dev:" in line)
    review_line = next(line for line in out.splitlines() if "review:" in line)
    assert "claude" in dev_line and "--model opus" in dev_line
    assert review_line.split("review:")[1].strip().startswith("codex ")
    assert "--model gpt-5-codex" in review_line


def test_sweep_dry_run_lists_open_entries(project, capsys):
    from conftest import write_ledger

    write_ledger(project, {"DW-1": "open", "DW-2": "done 2026-06-01"}, commit=False)
    assert cli._sweep_dry_run(project, policy_mod.load(None)) == 0
    out = capsys.readouterr().out
    assert "1 open" in out
    assert "DW-1" in out and "DW-2" not in out
    triage_line = next(line for line in out.splitlines() if "triage:" in line)
    assert "bmad-auto-sweep" in triage_line


def test_sweep_dry_run_reports_legacy_entries(project, capsys):
    from conftest import write_legacy_ledger

    write_legacy_ledger(
        project,
        "# Deferred Work\n\n## Deferred from: epic 1 review (2026-04-06)\n\n"
        "- ~~**Old fixed thing** — repaired~~ → fixed in 1.3\n"
        "- **Open legacy thing here** — still pending\n",
        commit=False,
    )
    assert cli._sweep_dry_run(project, policy_mod.load(None)) == 0
    out = capsys.readouterr().out
    assert "0 open" in out  # canonical view
    assert "2 legacy (pre-DW-format) entries, 1 open" in out
    assert "would first migrate them" in out
    assert "Open legacy thing here" in out and "Old fixed thing" not in out
    assert "triage:" in out  # a sweep still runs even with zero canonical opens


def test_sweep_dry_run_renders_triage_adapter_from_policy(project, capsys):
    from conftest import write_ledger

    write_ledger(project, {"DW-1": "open"}, commit=False)
    _write_policy(
        project.project,
        '[adapter]\nmodel = "opus"\n[adapter.triage]\nname = "gemini"\n',
    )
    pol = policy_mod.load(project.project / ".automator" / "policy.toml")
    assert cli._sweep_dry_run(project, pol) == 0
    out = capsys.readouterr().out
    triage_line = next(line for line in out.splitlines() if "triage:" in line)
    assert triage_line.split("triage:")[1].strip().startswith("gemini ")
    # client switch: base model is claude-specific, must not leak into gemini
    assert "--model" not in triage_line


def test_sweep_dry_run_no_ledger(project, capsys):
    assert cli._sweep_dry_run(project, policy_mod.load(None)) == 0
    assert "no deferred-work ledger" in capsys.readouterr().out


class _StubEngine:
    def __init__(self, **kwargs):
        pass

    def run(self):
        class Summary:
            paused = False

            def render(self):
                return "stub summary"

        return Summary()


def test_run_honors_preassigned_run_id_and_writes_pid(project, monkeypatch):
    import os

    from conftest import git

    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "setup")
    monkeypatch.setattr(cli, "Engine", _StubEngine)
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {r: None for r in cli.ROLES})

    run_id = "20990101-000000-beef"
    assert cli.main(["run", "--project", str(project.project), "--run-id", run_id]) == 0
    run_dir = project.project / ".automator" / "runs" / run_id
    assert json.loads((run_dir / "state.json").read_text())["run_id"] == run_id
    assert (run_dir / "engine.pid").read_text() == str(os.getpid())


def _make_run_with_state(project, run_id, **state_kwargs):
    from automator.journal import save_state
    from automator.model import RunState

    run_dir = project / ".automator" / "runs" / run_id
    save_state(
        run_dir,
        RunState(run_id=run_id, project=str(project), started_at="now", **state_kwargs),
    )
    return run_dir


def test_stop_no_such_run(tmp_path, capsys):
    assert cli.main(["stop", "--project", str(tmp_path), "missing"]) == 1
    assert "no such run" in capsys.readouterr().err


def test_stop_marks_stopped(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_run_with_state(tmp_path, "r1")  # no pid -> fallback marks stopped
    assert cli.main(["stop", "--project", str(tmp_path), "r1"]) == 0
    assert "r1 stopped" in capsys.readouterr().out
    from automator.journal import load_state

    assert load_state(run_dir).stopped is True


def test_stop_already_finished(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    _make_run_with_state(tmp_path, "r1", finished=True)
    assert cli.main(["stop", "--project", str(tmp_path), "r1"]) == 1
    assert "already finished" in capsys.readouterr().err


def test_delete_refuses_live_run_without_force(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_alive", lambda _rd: True)
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1"]) == 1
    assert "stop it first" in capsys.readouterr().err
    assert run_dir.exists()


def test_delete_force_stops_then_removes(tmp_path, monkeypatch, capsys):
    from automator import runs

    stopped = []
    monkeypatch.setattr(runs, "engine_alive", lambda _rd: True)
    monkeypatch.setattr(runs, "stop_run", lambda rd: stopped.append(rd) or True)
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["delete", "--project", str(tmp_path), "r1", "--force"]) == 0
    assert "r1 deleted" in capsys.readouterr().out
    assert stopped == [run_dir]
    assert not run_dir.exists()


def test_delete_dead_run(tmp_path, capsys):
    run_dir = _make_run_with_state(tmp_path, "r1")  # no pid -> not alive
    assert cli.main(["delete", "--project", str(tmp_path), "r1"]) == 0
    assert not run_dir.exists()


def test_archive_creates_tarball_and_removes_run(tmp_path, capsys):
    run_dir = _make_run_with_state(tmp_path, "20260611-100000-aaaa")
    assert cli.main(["archive", "--project", str(tmp_path), "20260611-100000-aaaa"]) == 0
    out = capsys.readouterr().out
    dest = tmp_path / ".automator" / "archive" / "20260611-100000-aaaa.tar.gz"
    assert "archived to" in out
    assert dest.is_file()
    assert not run_dir.exists()


def test_archive_refuses_live_run_without_force(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_alive", lambda _rd: True)
    run_dir = _make_run_with_state(tmp_path, "r1")
    assert cli.main(["archive", "--project", str(tmp_path), "r1"]) == 1
    assert "stop it first" in capsys.readouterr().err
    assert run_dir.exists()


def _escalated_run(project, run_id="r1", *, story="s1", spec_file=None):
    from automator.model import Phase, StoryTask

    task = StoryTask(story_key=story, epic=1, phase=Phase.ESCALATED, attempt=1, spec_file=spec_file)
    return _make_run_with_state(
        project,
        run_id,
        paused_reason="CRITICAL escalation",
        paused_stage="escalation",
        paused_story_key=story,
        tasks={story: task},
    )


def test_resolve_no_such_run(tmp_path, capsys):
    assert cli.main(["resolve", "--project", str(tmp_path), "missing"]) == 1
    assert "no such run" in capsys.readouterr().err


def test_resolve_rejects_non_escalation_stage(tmp_path, capsys):
    _make_run_with_state(tmp_path, "r1", paused_stage="spec-approval", paused_reason="x")
    assert cli.main(["resolve", "--project", str(tmp_path), "r1"]) == 1
    assert "not paused at an escalation" in capsys.readouterr().err


def test_resolve_refuses_live_run(tmp_path, monkeypatch, capsys):
    from automator import runs

    monkeypatch.setattr(runs, "engine_alive", lambda _rd: True)
    _escalated_run(tmp_path, "r1")
    assert cli.main(["resolve", "--project", str(tmp_path), "r1"]) == 1
    assert "still live" in capsys.readouterr().err


def test_resolve_no_escalated_story(tmp_path, capsys):
    _make_run_with_state(
        tmp_path, "r1", paused_stage="escalation", paused_reason="x", paused_story_key="ghost"
    )
    assert cli.main(["resolve", "--project", str(tmp_path), "r1"]) == 1
    assert "no escalated story" in capsys.readouterr().err


def test_resolve_no_interactive_rearms_and_resumes(tmp_path, monkeypatch, capsys):
    from automator.journal import load_state
    from automator.model import Phase

    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: in-review\n---\n", encoding="utf-8")
    run_dir = _escalated_run(tmp_path, "r1", spec_file=str(spec))

    resumed = []
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: resumed.append(rd) or 0)
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--no-interactive", "--resume"])
    assert rc == 0
    assert resumed == [run_dir]
    # re-armed: task flipped out of ESCALATED, spec status re-armed
    task = load_state(run_dir).tasks["s1"]
    assert task.phase == Phase.PENDING
    assert "ready-for-dev" in spec.read_text()


def test_resolve_interactive_runs_session_then_rearms(tmp_path, monkeypatch):
    from automator import resolve
    from automator.journal import load_state
    from automator.model import Phase

    _escalated_run(tmp_path, "r1")
    calls = {}
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {"dev": object()})
    monkeypatch.setattr(resolve, "build_context", lambda *a, **k: calls.setdefault("ctx", True))
    monkeypatch.setattr(
        resolve, "run_session", lambda *a, **k: calls.setdefault("session", True) or True
    )
    monkeypatch.setattr(cli, "_resume_paused_run", lambda proj, rd: 0)
    run_dir = tmp_path / ".automator" / "runs" / "r1"
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--resume"])
    assert rc == 0
    assert calls == {"ctx": True, "session": True}
    assert load_state(run_dir).tasks["s1"].phase == Phase.PENDING


def test_resolve_interactive_unsupported_adapter(tmp_path, monkeypatch, capsys):
    from automator import resolve

    _escalated_run(tmp_path, "r1")
    monkeypatch.setattr(cli, "_make_adapters", lambda *a, **k: {"dev": object()})
    monkeypatch.setattr(resolve, "build_context", lambda *a, **k: None)

    def boom(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(resolve, "run_session", boom)
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1"])
    assert rc == 1
    assert "no interactive session mode" in capsys.readouterr().err


def test_resolve_rearm_only_skips_resume(tmp_path, monkeypatch, capsys):
    _escalated_run(tmp_path, "r1")
    monkeypatch.setattr(
        cli, "_resume_paused_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("resumed"))
    )
    rc = cli.main(["resolve", "--project", str(tmp_path), "r1", "--no-interactive", "--no-resume"])
    assert rc == 0
    assert "resume when ready" in capsys.readouterr().out


def test_sweep_command_parses_flags():
    parser_args = [
        "sweep",
        "--project",
        ".",
        "--no-prompt",
        "--decisions-only",
        "--max-bundles",
        "3",
        "--repeat",
        "--max-cycles",
        "4",
        "--dry-run",
    ]
    # exercise argparse wiring only: dry-run path needs a valid project, so
    # just confirm parsing reaches cmd_sweep with the expected namespace
    import argparse as ap

    captured = {}

    def fake_cmd(args: ap.Namespace) -> int:
        captured.update(vars(args))
        return 0

    original = cli.cmd_sweep
    cli.cmd_sweep = fake_cmd
    try:
        # rebuild the parser so it binds the patched function
        assert cli.main(parser_args) == 0
    finally:
        cli.cmd_sweep = original
    assert captured["no_prompt"] is True
    assert captured["decisions_only"] is True
    assert captured["max_bundles"] == 3
    assert captured["repeat"] is True
    assert captured["max_cycles"] == 4
    assert captured["dry_run"] is True
