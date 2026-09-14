"""최후 수단 슬롯은 켜야 쓰인다.

2026-09-15 대표님 지시: "내가 슬롯3을 켜면 사용가능한거지".

순서를 최하로 두는 것만으로는 부족하다 — 슬롯 1·2 가 동시에 막히는 날이
오면 아무도 켜지 않았는데 남의 한도가 새어나간다. 스위치를 둔다.
"""
import asyncio

import pytest

from app.services import model_selector, slot_gate


def _records():
    return [
        {"key_name": "ANTHROPIC_AUTH_TOKEN", "slot": "1", "priority": 2},
        {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "slot": "2", "priority": 1},
        {"key_name": "ANTHROPIC_AUTH_TOKEN_3", "slot": "3", "priority": 3},
    ]


def _patch_records(monkeypatch):
    async def _fetch(include_rate_limited=True):
        return _records()

    monkeypatch.setattr(model_selector, "_ap_get_key_records_async", _fetch)


def test_disabled_slot_is_not_a_candidate(monkeypatch):
    _patch_records(monkeypatch)

    async def _off(slot):
        return False

    monkeypatch.setattr(model_selector, "_slot_gate_enabled", _off)
    slots = asyncio.run(model_selector._get_claude_slot_records())
    assert set(slots) == {"1", "2"}


def test_enabled_slot_joins_as_last(monkeypatch):
    _patch_records(monkeypatch)

    async def _on(slot):
        return True

    monkeypatch.setattr(model_selector, "_slot_gate_enabled", _on)
    slots = asyncio.run(model_selector._get_claude_slot_records())
    order = [s for s, _ in sorted(slots.items(), key=lambda i: model_selector._slot_sort_key(i[0], i[1]))]
    # 1·2 의 앞뒤는 DB 우선순위가 정한다(여기선 slot2 가 priority 1). 이 시험이
    # 지키는 것은 하나다 — 최후 수단은 언제나 맨 뒤다.
    assert set(order) == {"1", "2", "3"}
    assert order[-1] == "3"


def test_gate_read_failure_means_off(monkeypatch):
    """DB 를 못 읽었을 때 켜진 것으로 보면, 장애 순간에 남의 계정을 쓴다."""
    slot_gate.invalidate()

    def _boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr("app.core.db_pool.get_pool", _boom)
    assert asyncio.run(slot_gate.is_enabled("3")) is False


def test_normal_slots_are_never_gated():
    assert asyncio.run(slot_gate.is_enabled("1")) is True
    assert asyncio.run(slot_gate.is_enabled("2")) is True


def test_set_enabled_refuses_non_gated_slot():
    out = asyncio.run(slot_gate.set_enabled("2", True))
    assert out["ok"] is False
    assert out["error"] == "not_gated"


def test_probe_reads_slot_assigned_records(monkeypatch):
    """슬롯 번호는 auth_provider 가 붙인다.

    `llm_key_provider` 의 원본 레코드에는 slot 이 없어서, 그걸 보면 언제나
    token_missing 이 된다 — 2026-09-15 실제로 그렇게 실패했다.
    """
    import inspect

    src = inspect.getsource(slot_gate.probe_usage)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "get_oauth_key_records_async" in body
    assert "get_provider_key_records" not in body
