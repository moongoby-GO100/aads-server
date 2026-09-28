"""PC observation freshness and disconnect regression tests."""

from datetime import datetime, timedelta

import pytest

from app.models.pc_agent import StreamConfig
from app.services.pc_agent_manager import PCAgentManager


class DummyWebSocket:
    async def send_json(self, message):
        self.last_message = message


def test_frame_age_uses_now_not_last_two_frame_interval(monkeypatch):
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    manager._agents["pc"].streaming = True
    now = manager._now()
    monkeypatch.setattr(manager, "_now", lambda: now)
    manager.record_frame("pc")
    monkeypatch.setattr(manager, "_now", lambda: now + timedelta(seconds=1))
    manager.record_frame("pc")
    assert manager.get_last_observation("pc")["frame_fresh"] is True
    monkeypatch.setattr(manager, "_now", lambda: now + timedelta(seconds=31))

    observation = manager.get_last_observation("pc")
    assert observation["frame_interval_seconds"] == 1
    assert observation["frame_age_seconds"] == 30
    assert observation["frame_fresh"] is False


def test_disconnect_preserves_last_observation_and_classifies_contact_loss(monkeypatch):
    manager = PCAgentManager()
    ws = DummyWebSocket()
    manager.register_agent("pc", ws, {})
    manager.update_observation("pc", {"cpu_percent": 23, "ram_percent": 42, "token": "never store"})
    assert manager.unregister_agent("pc", ws)

    observation = manager.get_last_observation("pc")
    assert observation["cpu_percent"] == 23
    assert observation["ram_percent"] == 42
    assert "token" not in observation
    assert observation["classification"] == "contact_lost"


def test_healthy_heartbeat_distinguishes_resource_pressure_and_stale_stream():
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    conn = manager._agents["pc"]
    conn.info.last_heartbeat = manager._now()
    manager.update_observation("pc", {"commit_percent": 95})
    assert manager.get_last_observation("pc")["classification"] == "resource_pressure"

    manager.update_observation("pc", {"commit_percent": 50})
    conn.streaming = True
    conn.last_frame_at = manager._now() - timedelta(seconds=15)
    assert manager.get_last_observation("pc")["classification"] == "app_unresponsive"


def test_stream_with_no_first_frame_becomes_unresponsive(monkeypatch):
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    conn = manager._agents["pc"]
    now = manager._now()
    conn.info.last_heartbeat = now
    conn.streaming = True
    conn.stream_started_at = now - timedelta(seconds=11)
    monkeypatch.setattr(manager, "_now", lambda: now)

    observation = manager.get_last_observation("pc")
    assert observation["frame_age_seconds"] is None
    assert observation["frame_fresh"] is False
    assert observation["classification"] == "app_unresponsive"


def test_old_resource_sample_does_not_claim_current_pressure(monkeypatch):
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    manager.update_observation("pc", {"cpu_percent": 99})
    now = manager._now()
    manager._agents["pc"].info.last_heartbeat = now + timedelta(seconds=121)
    monkeypatch.setattr(manager, "_now", lambda: now + timedelta(seconds=121))

    observation = manager.get_last_observation("pc")
    assert observation["observation_age_seconds"] >= 120
    assert observation["classification"] == "healthy"


def test_offline_ages_keep_advancing_in_memory_and_db_snapshot(monkeypatch):
    manager = PCAgentManager()
    ws = DummyWebSocket()
    manager.register_agent("pc", ws, {})
    start = manager._now()
    monkeypatch.setattr(manager, "_now", lambda: start)
    manager.update_observation("pc", {"cpu_percent": 20})
    manager.record_frame("pc")
    assert manager.unregister_agent("pc", ws)
    first = manager.get_last_observation("pc")
    monkeypatch.setattr(manager, "_now", lambda: start + timedelta(seconds=30))
    # The DB stores the event snapshot, so the same formatter must refresh it.
    second = manager.refresh_offline_observation(first, start + timedelta(seconds=30))
    assert second["frame_age_seconds"] == 30
    assert second["observation_age_seconds"] == 30
    assert second["classification"] == "contact_lost"
    later = manager.get_last_observation("pc")
    assert later["frame_age_seconds"] == 30
    assert later["observation_age_seconds"] == 30


def test_timeout_recovers_after_new_heartbeat_and_frame():
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    conn = manager._agents["pc"]
    conn.last_command_status = "timeout"
    conn.last_command_at = manager._now()
    conn.info.last_heartbeat = conn.last_command_at
    assert manager.get_last_observation("pc")["classification"] == "app_unresponsive"
    conn.info.last_heartbeat = conn.last_command_at + timedelta(seconds=1)
    assert manager.get_last_observation("pc")["classification"] == "healthy"
    conn.streaming = True
    conn.stream_state = "active"
    conn.stream_started_at = conn.last_command_at
    conn.last_frame_at = conn.last_command_at - timedelta(seconds=20)
    assert manager.get_last_observation("pc")["classification"] == "app_unresponsive"
    manager.record_frame("pc")
    assert manager.get_last_observation("pc")["classification"] == "healthy"


@pytest.mark.asyncio
async def test_stream_requires_ack_and_tracks_failure_stop_and_lost_frame(monkeypatch):
    manager = PCAgentManager()
    ws = DummyWebSocket()
    manager.register_agent("pc", ws, {})
    start = manager._now()
    command_id = await manager.start_stream("pc", StreamConfig())
    assert manager.get_last_observation("pc")["stream_state"] == "starting"
    assert manager.get_last_observation("pc")["streaming"] is False
    manager.receive_result(command_id, {"status": "error"})
    assert manager.get_last_observation("pc")["stream_state"] == "failed"
    command_id = await manager.start_stream("pc", StreamConfig())
    manager.receive_result(command_id, {"status": "success"})
    assert manager.get_last_observation("pc")["streaming"] is True
    manager.record_frame("pc")
    monkeypatch.setattr(manager, "_now", lambda: start + timedelta(seconds=12))
    assert manager.get_last_observation("pc")["stream_state"] == "lost_frame"
    stop_id = await manager.stop_stream("pc")
    assert manager.get_last_observation("pc")["stream_state"] == "stopping"
    manager.receive_result(stop_id, {"status": "success"})
    assert manager.get_last_observation("pc")["streaming"] is False
    assert manager.get_last_observation("pc")["stream_state"] == "stopped"


def test_empty_resource_heartbeat_preserves_sample_freshness(monkeypatch):
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    start = manager._now()
    monkeypatch.setattr(manager, "_now", lambda: start)
    manager.update_observation("pc", {"cpu_percent": 91})
    monkeypatch.setattr(manager, "_now", lambda: start + timedelta(seconds=30))
    manager.update_observation("pc", {})
    manager.update_observation("pc", {"cpu_percent": "unknown"})
    manager.update_observation("pc", {"collection_ms": 1.5})
    result = manager.get_last_observation("pc")
    assert result["cpu_percent"] == 91
    assert result["observation_age_seconds"] == 30
    assert result["classification"] == "resource_pressure"


def test_old_resource_age_is_not_clamped_to_fresh(monkeypatch):
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    manager.update_observation("pc", {"cpu_percent": 99, "sample_age_seconds": 600})
    result = manager.get_last_observation("pc")
    assert result["observation_age_seconds"] >= 600
    assert result["classification"] == "healthy"


def test_os_event_window_and_bounded_count_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("KAKAOBOT_INSTALL_DIR", str(tmp_path))
    from pc_agent.agent import _os_event_sample
    assert _os_event_sample("<Event />" * 4) == {
        "os_event_count": 4,
        "os_event_count_is_lower_bound": False,
        "os_event_window_seconds": 60,
    }
    assert _os_event_sample("<Event />" * 11) == {
        "os_event_count": 10,
        "os_event_count_is_lower_bound": True,
        "os_event_window_seconds": 60,
    }


@pytest.mark.asyncio
async def test_db_offline_agent_refreshes_persisted_ages(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-only-pc-observation-secret")
    from app.api import pc_agent as api
    from app.core import db_pool

    observed = datetime.utcnow() - timedelta(seconds=40)
    row = {
        "agent_id": "pc", "event": "disconnected", "reason": "socket_closed",
        "metadata": {"last_observation": {
            "observed_at": observed.isoformat(),
            "last_frame_at": observed.isoformat(),
            "last_heartbeat_at": observed.isoformat(),
            "frame_age_seconds": 0, "observation_age_seconds": 0,
        }},
        "identity_metadata": {"user_id": "owner", "hostname": "pc"},
        "created_at": observed,
    }

    class FakeConnection:
        async def fetch(self, *args):
            return [row]

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *args):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool())
    agents = await api._latest_known_pc_agents_from_events("owner", include_all_agents=False)
    snapshot = agents[0]["last_observation"]
    assert snapshot["frame_age_seconds"] >= 40
    assert snapshot["observation_age_seconds"] >= 40
    assert snapshot["classification"] == "contact_lost"


@pytest.mark.asyncio
async def test_stream_ack_during_socket_send_is_not_lost():
    manager = PCAgentManager()

    class ImmediateAck(DummyWebSocket):
        async def send_json(self, message):
            manager.receive_result(message["id"], {"status": "success"})

    manager.register_agent("pc", ImmediateAck(), {})
    await manager.start_stream("pc", StreamConfig())
    assert manager.get_last_observation("pc")["stream_state"] == "active"
    await manager.stop_stream("pc")
    assert manager.get_last_observation("pc")["stream_state"] == "stopped"
    assert not manager._stream_commands


@pytest.mark.asyncio
async def test_frame_before_failed_start_ack_never_claims_success():
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    command_id = await manager.start_stream("pc", StreamConfig())
    manager.record_frame("pc")
    observed = manager.get_last_observation("pc")
    assert observed["stream_state"] == "starting"
    assert observed["streaming"] is False
    assert observed["frame_fresh"] is False
    manager.receive_result(command_id, {"status": "error"})
    assert manager.get_last_observation("pc")["stream_state"] == "failed"
    assert manager.get_last_observation("pc")["classification"] == "app_unresponsive"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "stop"])
@pytest.mark.parametrize("query_before_ack", [False, True])
async def test_missing_stream_ack_times_out_even_with_frames(monkeypatch, action, query_before_ack):
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    now = manager._now()
    monkeypatch.setattr(manager, "_now", lambda: now)
    start_id = await manager.start_stream("pc", StreamConfig())
    if action == "stop":
        manager.receive_result(start_id, {"status": "success"})
        command_id = await manager.stop_stream("pc")
    else:
        command_id = start_id
    now += timedelta(seconds=11)
    manager.record_frame("pc")
    if query_before_ack:
        assert manager.get_last_observation("pc")["stream_state"] == f"{action}_timeout"
    manager.receive_result(command_id, {"status": "success"})
    observed = manager.get_last_observation("pc")
    assert observed["stream_state"] == f"{action}_timeout"
    assert observed["classification"] == "app_unresponsive"
    assert not manager._stream_commands
    # Only a new, acknowledged control request clears the timeout.
    next_id = await manager.stop_stream("pc")
    manager.receive_result(next_id, {"status": "success"})
    assert manager.get_last_observation("pc")["stream_state"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "stop"])
async def test_stream_send_failure_restores_state_and_registration(action):
    manager = PCAgentManager()
    ws = DummyWebSocket()
    manager.register_agent("pc", ws, {})
    command_id = await manager.start_stream("pc", StreamConfig())
    manager.receive_result(command_id, {"status": "success"})
    manager.record_frame("pc")
    before = manager.get_last_observation("pc")

    async def fail_send(message):
        raise OSError("disconnected during send")

    ws.send_json = fail_send
    with pytest.raises(OSError):
        if action == "start":
            await manager.start_stream("pc", StreamConfig())
        else:
            await manager.stop_stream("pc")
    after = manager.get_last_observation("pc")
    assert after["stream_state"] == before["stream_state"] == "active"
    assert after["last_frame_at"] == before["last_frame_at"]
    assert after["streaming"] is True
    assert not manager._stream_commands


@pytest.mark.asyncio
async def test_old_stream_ack_cannot_affect_reconnected_agent():
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    old_id = await manager.start_stream("pc", StreamConfig())
    manager.register_agent("pc", DummyWebSocket(), {})
    manager.receive_result(old_id, {"status": "success"})
    assert manager.get_last_observation("pc")["stream_state"] == "stopped"
    assert not manager._stream_commands


@pytest.mark.asyncio
async def test_slow_telemetry_does_not_block_websocket_loop(monkeypatch, tmp_path):
    import asyncio
    import threading
    from types import SimpleNamespace

    monkeypatch.setenv("KAKAOBOT_INSTALL_DIR", str(tmp_path))
    from pc_agent.agent import PCAgent

    started = threading.Event()
    release = threading.Event()

    def collect():
        started.set()
        release.wait(timeout=2)
        return {"resources": {"cpu_percent": 25}}

    class Socket:
        async def send(self, message):
            raise RuntimeError("end test after first heartbeat")

    agent = SimpleNamespace(hostname="test", _get_version=lambda: "test", _runtime_telemetry=collect)
    task = asyncio.create_task(PCAgent._heartbeat(agent, Socket()))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        assert not task.done(), "blocking telemetry prevented the event loop from running"
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_pending_start_ack_is_preserved_when_stop_is_requested():
    manager = PCAgentManager()
    ws = DummyWebSocket()
    manager.register_agent("pc", ws, {})
    start_id = await manager.start_stream("pc", StreamConfig())
    with pytest.raises(ValueError, match="stream_command_pending"):
        await manager.stop_stream("pc")
    assert ws.last_message["id"] == start_id  # No new command sent.
    manager.receive_result(start_id, {"status": "success"})
    assert manager.get_last_observation("pc")["stream_state"] == "active"

    async def failed_send(message):
        raise OSError("stop send failed")

    ws.send_json = failed_send
    with pytest.raises(OSError):
        await manager.stop_stream("pc")
    assert manager.get_last_observation("pc")["stream_state"] == "active"
    assert not manager._stream_commands


@pytest.mark.parametrize("value", [10**400, -(10**400), float("inf"), float("nan"), True])
def test_invalid_resource_number_cannot_disconnect_or_refresh(value, monkeypatch):
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    start = manager._now()
    monkeypatch.setattr(manager, "_now", lambda: start)
    manager.update_observation("pc", {"cpu_percent": 22})
    monkeypatch.setattr(manager, "_now", lambda: start + timedelta(seconds=30))
    manager.update_observation("pc", {"cpu_percent": value})
    observed = manager.get_last_observation("pc")
    assert observed["cpu_percent"] == 22
    assert observed["observation_age_seconds"] == 30


def test_enormous_sample_age_remains_stale_without_float_overflow():
    manager = PCAgentManager()
    manager.register_agent("pc", DummyWebSocket(), {})
    manager.update_observation("pc", {"cpu_percent": 99, "sample_age_seconds": 10**400})
    observed = manager.get_last_observation("pc")
    assert observed["observation_age_seconds"] >= 86400
    assert observed["classification"] != "resource_pressure"
