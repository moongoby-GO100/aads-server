"""스스로 갱신하지 못하는 슬롯은 죽는 순간을 알아야 한다.

2026-09-15 대표님 확인 — "진아 토큰 자동리프레시반영했잖아… 안되었으면
즉시 조치해".

**되어 있지 않고, 지금 구조로는 할 수 없다.** 자동 갱신은 리프레시 토큰이
있어야 하는데 슬롯 3 에는 액세스 토큰만 있다. 진아 서버에도 없다
(`.credentials.json` 부재, 크론 0건, 리스 파일은 리프레시 없이 이미 만료).

죽는 것을 막을 수 없으면 **죽는 순간 바로 아는** 것이 다음으로 좋다.
"""
import asyncio
import inspect

import pytest

from app.services import slot_token_health as health


def test_probe_does_not_spend_quota():
    """살아 있는지만 보는 데 한도를 쓰면 확인 자체가 비용이 된다."""
    src = inspect.getsource(health.check_slot)
    assert "/v1/models" in src
    assert "/v1/messages" not in src


def test_only_watches_slots_that_cannot_refresh():
    """슬롯 1·2 는 keeper 가 지킨다. 여기서 또 보면 호출만 는다."""
    src = inspect.getsource(health.slot_token_health_poller)
    assert "LAST_RESORT_SLOTS" in src


def test_alerts_once_on_death_not_every_round(monkeypatch):
    """10분마다 같은 알림이 쌓이면 정작 중요한 것이 묻힌다."""
    src = inspect.getsource(health.check_slot)
    assert "was_alive and not result" in src
    assert "dedupe_minutes" in src


def test_network_error_does_not_flip_state_to_dead(monkeypatch):
    """네트워크 문제와 토큰 죽음은 다르다. 섞으면 거짓 경보가 된다."""
    health._state["3"] = {"alive": True, "checked_at": 0, "status": 200}

    async def _boom(include_rate_limited=True):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "app.core.auth_provider.get_oauth_key_records_async", _boom)
    out = asyncio.run(health.check_slot("3"))
    assert out["alive"] is True, "네트워크 오류를 토큰 죽음으로 보고 있다"


def test_snapshot_is_a_copy():
    health._state["3"] = {"alive": True, "checked_at": 1, "status": 200}
    snap = health.snapshot()
    snap["3"]["alive"] = False
    assert health._state["3"]["alive"] is True
