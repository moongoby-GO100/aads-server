"""슬롯에 프로젝트를 배정한다.

2026-09-15 대표님 지시 — "슬롯에 복수의 프로젝트를 지정하고 지정된 프로젝트는
해당 슬롯을 우선 사용… 미설정시 전체 프로젝트에서 사용가능하게 하고".

규칙 셋을 시험으로 고정한다.

  1. 배정 없는 슬롯 → 모든 프로젝트가 쓴다
  2. 배정 있는 슬롯 → 그 프로젝트만 쓰고, 먼저 집는다
  3. 배정된 프로젝트 안에서는 최후 수단 스위치를 묻지 않는다
"""
import asyncio

import pytest

from app.services import model_selector, slot_projects


def _patch_map(monkeypatch, mapping):
    async def _fake(force=False):
        return mapping

    monkeypatch.setattr(slot_projects, "slot_project_map", _fake)


def test_no_assignment_means_everyone(monkeypatch):
    _patch_map(monkeypatch, {})
    out = asyncio.run(slot_projects.order_slots_for_project(["1", "2", "3"], "ACCT"))
    assert out == ["1", "2", "3"]


def test_assigned_project_gets_its_slot_first(monkeypatch):
    _patch_map(monkeypatch, {"3": {"ACCT"}})
    out = asyncio.run(slot_projects.order_slots_for_project(["1", "2", "3"], "ACCT"))
    assert out == ["3", "1", "2"], out


def test_other_projects_lose_the_assigned_slot(monkeypatch):
    """배정된 슬롯은 남의 프로젝트에서 후보가 아니다 — 그것이 배정의 뜻이다."""
    _patch_map(monkeypatch, {"3": {"ACCT"}})
    out = asyncio.run(slot_projects.order_slots_for_project(["1", "2", "3"], "GO100"))
    assert out == ["1", "2"], out


def test_multiple_projects_on_one_slot(monkeypatch):
    _patch_map(monkeypatch, {"3": {"ACCT", "FOOD"}})
    for project in ("ACCT", "FOOD"):
        out = asyncio.run(slot_projects.order_slots_for_project(["1", "2", "3"], project))
        assert out[0] == "3", (project, out)
    assert asyncio.run(
        slot_projects.order_slots_for_project(["1", "2", "3"], "LAW")) == ["1", "2"]


def test_unknown_project_keeps_only_free_slots(monkeypatch):
    """프로젝트를 못 알아내도 남의 배정 슬롯을 쓰면 안 된다."""
    _patch_map(monkeypatch, {"3": {"ACCT"}})
    out = asyncio.run(slot_projects.order_slots_for_project(["1", "2", "3"], ""))
    assert out == ["1", "2"], out


def test_lookup_failure_falls_back_to_global_order(monkeypatch):
    """못 읽었을 때 배정이 있는 것으로 보면 프로젝트가 슬롯을 통째로 잃는다."""
    def _boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr("app.core.db_pool.get_pool", _boom)
    slot_projects.invalidate()
    assert asyncio.run(slot_projects.slot_project_map(force=True)) == {}


def test_assignment_exempts_the_last_resort_switch(monkeypatch):
    """배정한 것 자체가 허락이다. 배정해 두고 스위치를 또 켜게 하지 않는다."""
    async def _records(include_rate_limited=True):
        return [
            {"key_name": "ANTHROPIC_AUTH_TOKEN", "slot": "1", "priority": 2},
            {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "slot": "2", "priority": 1},
            {"key_name": "ANTHROPIC_AUTH_TOKEN_3", "slot": "3", "priority": 3},
        ]

    async def _gate_off(slot):
        return False

    async def _assigned(slot, project):
        return slot == "3" and project == "ACCT"

    monkeypatch.setattr(model_selector, "_ap_get_key_records_async", _records)
    monkeypatch.setattr(model_selector, "_slot_gate_enabled", _gate_off)
    monkeypatch.setattr(model_selector, "_slot_assigned_to_project", _assigned)

    acct = asyncio.run(model_selector._get_claude_slot_records(project="ACCT"))
    other = asyncio.run(model_selector._get_claude_slot_records(project="GO100"))
    assert "3" in acct, "배정 프로젝트인데 스위치에 막혔다"
    assert "3" not in other, "배정 밖인데 스위치 없이 통과했다"
