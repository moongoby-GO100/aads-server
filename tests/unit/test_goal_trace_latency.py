import asyncio
from types import SimpleNamespace

from app.services import goal_manager


def test_goal_trace_passes_elapsed_monotonic_latency(monkeypatch):
    captured = {}
    readings = iter((10.0, 10.125))

    async def record_trace(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(goal_manager, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    monkeypatch.setattr(
        "app.services.ohvis_harness_trace.record_trace", record_trace, raising=False
    )
    started_at = goal_manager._trace_started_at()
    asyncio.run(goal_manager.GoalStateMachine()._trace("goal_create", started_at=started_at))

    assert captured["latency_ms"] == 125


def test_goal_trace_clock_failure_is_non_fatal(monkeypatch):
    captured = {}

    async def record_trace(**kwargs):
        captured.update(kwargs)
        return True

    def broken_clock():
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(goal_manager, "time", SimpleNamespace(monotonic=broken_clock))
    monkeypatch.setattr(
        "app.services.ohvis_harness_trace.record_trace", record_trace, raising=False
    )
    asyncio.run(goal_manager.GoalStateMachine()._trace("goal_create", started_at=1.0))

    assert captured["latency_ms"] is None
