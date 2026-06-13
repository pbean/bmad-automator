"""RunState serialization + lifecycle-flag tests."""

from automator.model import RunState


def _state(**kw) -> RunState:
    return RunState(run_id="r1", project="/p", started_at="now", **kw)


def test_stopped_round_trips():
    state = _state(stopped=True)
    assert RunState.from_dict(state.to_dict()).stopped is True


def test_stopped_defaults_false_for_legacy_state():
    doc = _state().to_dict()
    del doc["stopped"]  # a state.json written before the field existed
    assert RunState.from_dict(doc).stopped is False


def test_clear_pause_also_clears_stopped():
    state = _state(stopped=True, paused_reason="escalation", paused_stage="x")
    state.clear_pause()
    assert state.stopped is False
    assert state.paused is False
