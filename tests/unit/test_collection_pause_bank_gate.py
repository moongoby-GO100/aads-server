"""은행 자동수집 런타임 일시정지 스위치 계약.

`_run_bank_auto_collect` 는 배달과 달리 `is_collection_paused` 를 부르지 않아,
system_memory(ops/collection_paused) 로 멈춰도 CronTrigger 로 계속 돌았다
(2026-09-30 실측: .bank_auto_collect.lock mtime 이 멈춤 이후에도 갱신).
app/main.py 의 중첩 함수를 AST 로 뽑아 가짜 의존성으로 실행한다.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
import textwrap

import pytest

_MAIN = Path(__file__).resolve().parents[2] / "app" / "main.py"


def _extract(name: str):
    src = _MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(src, node))
    raise AssertionError(f"{name} not found in app/main.py")


class _Calls:
    def __init__(self):
        self.acquire = 0
        self.wait_online = 0
        self.queue_snapshot = 0


def _fake_modules(monkeypatch, calls: _Calls, paused: dict, raises: bool = False):
    async def is_paused(kind="delivery"):
        if raises:
            raise RuntimeError("db down")
        return bool(paused.get(kind))

    async def pause_reason(kind="delivery"):
        return "ceo-hold"

    cp = types.ModuleType("app.services.collection_pause")
    cp.is_collection_paused = is_paused
    cp.pause_reason = pause_reason
    monkeypatch.setitem(sys.modules, "app.services.collection_pause", cp)
    monkeypatch.setattr("app.services.collection_pause", cp, raising=False)

    class _Mgr:
        async def wait_for_agent_online(self, agent_id="", timeout=30):
            calls.wait_online += 1
            return {"status": "offline", "error_code": "PC_AGENT_OFFLINE"}

        def list_agent_statuses(self):
            return []

    pm = types.ModuleType("app.services.pc_agent_manager")
    pm.pc_agent_manager = _Mgr()
    monkeypatch.setitem(sys.modules, "app.services.pc_agent_manager", pm)

    async def queue_snapshot_async(limit=100):
        calls.queue_snapshot += 1
        return []

    q = types.ModuleType("app.services.pc_agent_collection_queue")
    q.FINANCIAL_RESOURCE_KEY = "financial_exclusive"
    q.queue_snapshot_async = queue_snapshot_async
    monkeypatch.setitem(sys.modules, "app.services.pc_agent_collection_queue", q)


def _namespace(calls: _Calls, tmp_path: Path):
    logger = logging.getLogger("test_bank_gate")

    async def peer(_excluded):
        return {"status": "offline", "error_code": "PC_AGENT_OFFLINE"}

    def acquire(_path):
        calls.acquire += 1
        return None  # already_running 으로 빠져 수집 본체는 실행하지 않는다

    return {
        "__file__": str(tmp_path / "app" / "main.py"),
        "os": os,
        "logger": logger,
        "datetime": datetime,
        "KST": timezone.utc,
        "_is_active_api_container_for_background_jobs": lambda: True,
        "_process_lock_is_active": lambda _p: False,
        "_try_acquire_process_lock": acquire,
        "_release_process_lock": lambda _fd: None,
        "_delivery_auto_collect_peer_agent": peer,
        "_delivery_auto_collect_parse_time": lambda _v: None,
        "_bank_auto_collect_excluded_agent_ids": lambda: set(),
        "_delivery_auto_collect_excluded_agent_ids": lambda: set(),
        "_PC_AGENT_QUEUE_SKIP_STREAK": {},
        "_PC_AGENT_QUEUE_SKIP_LOG_EVERY": 10,
        "_env_int": lambda _n, d: d,
    }


def _load_fn(name: str, ns: dict):
    exec(compile(_extract(name), str(_MAIN), "exec"), ns)
    return ns[name]


def test_bank_paused_skips_before_lock(monkeypatch, tmp_path, caplog):
    calls = _Calls()
    _fake_modules(monkeypatch, calls, {"bank": True})
    fn = _load_fn("_run_bank_auto_collect", _namespace(calls, tmp_path))
    with caplog.at_level(logging.INFO, logger="test_bank_gate"):
        asyncio.run(fn("scheduled_bank"))
    assert calls.acquire == 0
    assert "bank_auto_collect_skip: paused" in caplog.text
    assert "pause_reason=ceo-hold" in caplog.text


def test_all_switch_also_pauses_bank(monkeypatch, tmp_path):
    calls = _Calls()
    _fake_modules(monkeypatch, calls, {"bank": True})
    fn = _load_fn("_run_bank_auto_collect", _namespace(calls, tmp_path))
    asyncio.run(fn())
    assert calls.acquire == 0


def test_bank_not_paused_keeps_existing_path(monkeypatch, tmp_path):
    calls = _Calls()
    _fake_modules(monkeypatch, calls, {"delivery": True})
    fn = _load_fn("_run_bank_auto_collect", _namespace(calls, tmp_path))
    asyncio.run(fn())
    assert calls.acquire == 1


def test_switch_lookup_failure_does_not_pause_bank(monkeypatch, tmp_path):
    calls = _Calls()
    _fake_modules(monkeypatch, calls, {"bank": True}, raises=True)
    fn = _load_fn("_run_bank_auto_collect", _namespace(calls, tmp_path))
    asyncio.run(fn())
    assert calls.acquire == 1


def test_global_queue_skipped_when_bank_and_delivery_paused(monkeypatch, tmp_path, caplog):
    calls = _Calls()
    _fake_modules(monkeypatch, calls, {"bank": True, "delivery": True})
    fn = _load_fn("_run_pc_agent_global_collection_queue", _namespace(calls, tmp_path))
    with caplog.at_level(logging.INFO, logger="test_bank_gate"):
        asyncio.run(fn())
    assert calls.queue_snapshot == 0
    assert calls.wait_online == 0
    assert "pc_agent_global_collection_queue_skip: paused" in caplog.text


@pytest.mark.parametrize("paused", [{"bank": True}, {"delivery": True}, {}])
def test_global_queue_runs_unless_both_paused(monkeypatch, tmp_path, paused):
    calls = _Calls()
    _fake_modules(monkeypatch, calls, paused)
    fn = _load_fn("_run_pc_agent_global_collection_queue", _namespace(calls, tmp_path))
    asyncio.run(fn())
    assert calls.queue_snapshot == 1
    assert calls.wait_online == 1
