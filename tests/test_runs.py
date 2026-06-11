"""Run-directory helper tests."""

import os
import re

from automator import runs


def _make_run(project, run_id, with_state=True):
    run_dir = project / ".automator" / "runs" / run_id
    run_dir.mkdir(parents=True)
    if with_state:
        (run_dir / "state.json").write_text("{}")
    return run_dir


def test_list_run_dirs_sorted_and_filtered(tmp_path):
    _make_run(tmp_path, "20260611-120000-bbbb")
    _make_run(tmp_path, "20260610-090000-aaaa")
    _make_run(tmp_path, "20260612-080000-cccc", with_state=False)  # no state.json
    listed = runs.list_run_dirs(tmp_path)
    assert [d.name for d in listed] == ["20260610-090000-aaaa", "20260611-120000-bbbb"]


def test_list_run_dirs_missing(tmp_path):
    assert runs.list_run_dirs(tmp_path) == []
    assert runs.latest_run_dir(tmp_path) is None


def test_latest_run_dir(tmp_path):
    _make_run(tmp_path, "20260610-090000-aaaa")
    newest = _make_run(tmp_path, "20260611-120000-bbbb")
    assert runs.latest_run_dir(tmp_path) == newest


def test_new_run_id_format():
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", runs.new_run_id())


def test_write_pid(tmp_path):
    runs.write_pid(tmp_path)
    assert (tmp_path / "engine.pid").read_text() == str(os.getpid())


def test_attach_argv_outside_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert runs.attach_argv("r1") == ["tmux", "attach", "-t", "=bmad-auto-r1"]


def test_attach_argv_inside_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    assert runs.attach_argv("r1") == ["tmux", "switch-client", "-t", "=bmad-auto-r1"]
