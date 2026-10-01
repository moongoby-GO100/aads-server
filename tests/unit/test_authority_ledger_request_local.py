"""서버 원장 provenance 는 **요청 단위 값**으로만 전달된다 (f4b0866d 잔여 경합).

예전에는 session_id 만 키로 쓰는 모듈 전역 칸에 stash 하고 pop 으로 꺼냈다.
그 칸은 같은 세션의 동시 턴을 구분하지 못한다.

  1. A 가 stash 한 뒤 compaction await 에서 멈춘 사이 같은 세션의 B 가
     stash+take 하면 A 는 {} 를 받는다.
  2. resume/discussion 이 stash 만 하고 take 하지 않으면, 뒤이은 main 조립이
     stash 전에 실패할 때 이전 칸 값을 꺼낸다.
  3. 값이 없는 턴은 server_ledger 키 자체가 빠져 `available:false` 도 남지 않는다.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from app.services import chat_service as cs
from app.services import context_builder as cb
from app.services import prompt_compiler as pc

SESSION = "00000000-0000-0000-0000-00000000a001"


@pytest.fixture
def barrier_env(monkeypatch):
    """원장 스냅샷은 호출마다 다른 값, compaction 은 Event 에서 대기."""
    counter = {"n": 0}
    real = cb.build_server_ledger_snapshot

    def _distinct_snapshot():
        counter["n"] += 1
        snap = real()
        n = counter["n"]
        snap["snapshot_hash"] = f"hash-{n:04d}"
        snap["observed_at"] = f"2026-10-01T16:00:{n:02d}+09:00"
        snap["registry_read_at"] = snap["observed_at"]
        return snap

    monkeypatch.setattr(cb, "build_server_ledger_snapshot", _distinct_snapshot)

    async def _empty(*_a, **_k):
        return ""

    for name in (
        "_build_layer2_dynamic",
        "_build_memory_layer",
        "_build_auto_rag_layer_bounded",
        "_build_workspace_preload_layer",
        "_build_artifact_context_layer",
        "_build_approved_document_layer",
    ):
        monkeypatch.setattr(cb, name, _empty)

    async def _no_cache(_key, coro):
        return await coro

    monkeypatch.setattr(cb, "_get_cached_or_build", _no_cache)

    import app.services.compaction_service as compaction
    import app.services.context_compressor as compressor

    gate = asyncio.Event()
    reached = asyncio.Event()
    calls = {"compact": 0}

    async def _blocking_compact(_sid, messages, db_conn=None):
        calls["compact"] += 1
        # 첫 호출(A)만 멈추고, 이후 호출(B 등)은 바로 통과한다.
        if calls["compact"] == 1:
            reached.set()
            await gate.wait()
        return messages

    monkeypatch.setattr(compressor, "needs_structured_summary", lambda *a, **k: True)
    monkeypatch.setattr(compaction, "check_and_compact", _blocking_compact)
    return {"gate": gate, "reached": reached, "calls": calls}


def _build(ledger_out, session_id=SESSION, **kw):
    return cb.build_messages_context(
        workspace_name="AADS",
        session_id=session_id,
        raw_messages=[{"id": "m1", "role": "user", "content": "서버 구성 알려줘"}],
        base_system_prompt="",
        ledger_out=ledger_out,
        **kw,
    )


# ─── 1. 같은 세션 동시 조립 ─────────────────────────────────────────────────

async def test_same_session_barrier_keeps_each_request_value(barrier_env):
    a_out: dict = {}
    b_out: dict = {}
    task_a = asyncio.create_task(_build(a_out))
    await asyncio.wait_for(barrier_env["reached"].wait(), 5)

    # A 는 stash 지점을 지나 compaction 에서 멈춰 있다. 같은 세션의 B 가 끝까지 돈다.
    assert a_out["snapshot_hash"] == "hash-0001"
    await _build(b_out)
    assert b_out["snapshot_hash"] == "hash-0002"

    barrier_env["gate"].set()
    await asyncio.wait_for(task_a, 5)

    assert a_out["snapshot_hash"] == "hash-0001"
    assert a_out["registry_read_at"] == "2026-10-01T16:00:01+09:00"
    assert a_out["available"] is True
    assert a_out != b_out and a_out != {}


async def test_ledger_out_is_optional_and_return_shape_is_unchanged(barrier_env):
    barrier_env["gate"].set()
    result = await _build(None)
    messages, system_prompt = result
    assert isinstance(messages, list) and isinstance(system_prompt, str)
    assert "현재 서버 원장" in system_prompt


# ─── 2. 읽기만 하는 helper(resume/discussion 형태) ──────────────────────────

async def test_helper_builders_do_not_disturb_a_paused_request(barrier_env):
    a_out: dict = {}
    task_a = asyncio.create_task(_build(a_out))
    await asyncio.wait_for(barrier_env["reached"].wait(), 5)
    before = dict(a_out)

    # resume 형태(db_conn 포함, ledger_out 없음)와 discussion 형태(apply_prompt_assets=False).
    await _build(None, db_conn=None)
    await _build(None, apply_prompt_assets=False)

    assert a_out == before
    barrier_env["gate"].set()
    await asyncio.wait_for(task_a, 5)
    assert a_out == before
    assert a_out["snapshot_hash"] == "hash-0001"


def test_no_process_global_state_survives_a_build(barrier_env):
    barrier_env["gate"].set()
    asyncio.run(_build(None))
    assert cb.take_ledger_provenance(SESSION) == {
        "available": False,
        "reason": "no_request_local_snapshot",
    }


# ─── 3. helper 가 먼저 돈 뒤 main 조립이 stash 전에 실패 ─────────────────────

async def _main_path_snippet(ledger_out_factory=dict):
    """chat_service 의 main 경로(13421 부근)와 같은 순서·같은 값 규칙."""
    _ledger_out = ledger_out_factory()
    _turn_server_ledger = {"available": False, "reason": "context_not_built"}
    try:
        await _build(_ledger_out)
        _turn_server_ledger = dict(_ledger_out)
    except Exception as _ctx_err:
        _turn_server_ledger = {
            "available": False,
            "reason": "context_build_failed",
            "error": str(_ctx_err)[:200],
        }
    return _turn_server_ledger


async def test_failed_main_build_after_helper_records_unavailable(barrier_env, monkeypatch):
    barrier_env["gate"].set()
    await _build(None)  # 더 이른 helper 조립
    helper_hash = "hash-0001"

    def _boom(*_a, **_k):
        raise RuntimeError("layer1 exploded before the ledger was built")

    monkeypatch.setattr(cb, "build_layer1", _boom)
    ledger = await _main_path_snippet()

    assert ledger["available"] is False
    assert ledger["reason"] == "context_build_failed"
    assert "layer1 exploded" in ledger["error"]
    assert helper_hash not in json.dumps(ledger)
    assert "snapshot_hash" not in ledger


async def test_failure_after_ledger_filled_still_overrides(barrier_env, monkeypatch):
    """원장을 채운 뒤(compaction 이후 등) 예외가 나도 main 은 실패값으로 덮는다."""
    barrier_env["gate"].set()

    # compaction 에러는 build 내부에서 삼켜지므로 messages 조립 쪽에서 터뜨린다.
    monkeypatch.setattr(cb, "_build_layer3_messages", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("late failure")))
    out: dict = {}
    ledger = await _main_path_snippet(lambda: out)
    assert out.get("snapshot_hash"), "예외 직전에 ledger_out 이 채워졌어야 이 케이스가 의미 있다"
    assert ledger["reason"] == "context_build_failed"
    assert "snapshot_hash" not in ledger


def test_chat_service_main_path_is_request_local():
    src = inspect.getsource(cs.send_message_stream)
    assert "ledger_out=_ledger_out" in src
    assert "_turn_server_ledger = dict(_ledger_out)" in src
    assert '"reason": "context_build_failed"' in src
    assert 'locals().get("_turn_server_ledger")' not in src
    assert "take_ledger_provenance" not in src
    # try 이전에 초기화
    assert src.index("_turn_server_ledger: dict = {") < src.index("_turn_server_ledger = dict(_ledger_out)")
    # SDK 조기 return 경로와 normal 경로가 같은 명시 변수를 쓴다.
    assert src.count("server_ledger=_turn_server_ledger") == 2


def test_resume_and_discussion_leave_no_global_state():
    helpers = [
        (name, inspect.getsource(fn))
        for name, fn in inspect.getmembers(cs, inspect.isfunction)
        if name != "send_message_stream" and "build_messages_context" in inspect.getsource(fn)
    ]
    assert helpers, "resume/discussion helper 를 찾지 못했다"
    for name, src in helpers:
        assert "take_ledger_provenance" not in src, name
        assert "_stash_ledger_provenance" not in src, name


def test_no_global_ledger_state_anywhere_in_app():
    import pathlib

    root = pathlib.Path(cb.__file__).resolve().parents[1]
    defining = pathlib.Path(cb.__file__).resolve()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "_LEDGER_PROVENANCE_BY_SESSION" not in text, path
        # 호환 shim 은 정의만 남고, 앱 안에서 부르는 곳이 없어야 한다.
        if path.resolve() != defining:
            assert "_stash_ledger_provenance(" not in text, path
            assert "take_ledger_provenance(" not in text, path
        else:
            assert text.count("_stash_ledger_provenance(") == 1, path
            assert text.count("take_ledger_provenance(") == 1, path


# ─── 4. record row ──────────────────────────────────────────────────────────

class _CapturingConn:
    def __init__(self):
        self.args = None

    async def fetchval(self, *_a, **_k):
        return True

    async def execute(self, query, *args):
        self.args = args


async def _record(server_ledger, provenance=None):
    conn = _CapturingConn()
    compiled = pc.CompiledPrompt(system_prompt="x", provenance=dict(provenance or {}))
    await pc.record_prompt_provenance(
        conn=conn,
        session_id=SESSION,
        execution_id=None,
        intent="status_check",
        model="claude-opus-5",
        compiled_prompt=compiled,
        server_ledger=server_ledger,
    )
    return json.loads(conn.args[-1]), compiled


async def test_record_row_carries_request_values_normal_and_sdk_paths(barrier_env):
    barrier_env["gate"].set()
    out: dict = {}
    await _build(out)
    expected = dict(out)

    # normal 경로와 SDK 조기 return 경로는 같은 record 함수에 같은 변수를 넘긴다.
    for _path in ("normal", "sdk_early_return"):
        row, _ = await _record(dict(out))
        led = row["server_ledger"]
        assert led["snapshot_hash"] == expected["snapshot_hash"]
        assert led["registry_read_at"] == expected["registry_read_at"]
        assert led["available"] is True


async def test_record_row_without_snapshot_is_explicitly_unavailable():
    for passed in (None, {}):
        row, compiled = await _record(passed)
        assert row["server_ledger"] == {
            "available": False,
            "reason": "caller_did_not_pass_snapshot",
        }
        assert compiled.provenance["server_ledger"]["available"] is False


async def test_record_row_keeps_failed_build_marker():
    marker = {"available": False, "reason": "context_build_failed", "error": "boom"}
    row, _ = await _record(marker)
    assert row["server_ledger"] == marker


def test_record_does_not_read_any_session_state():
    src = inspect.getsource(pc.record_prompt_provenance)
    assert "take_ledger_provenance" not in src
    assert "_server_ledger_snapshot" not in src


def test_snapshot_registry_read_at_is_the_read_time_and_hash_is_unchanged():
    first = cb.build_server_ledger_snapshot()
    second = cb.build_server_ledger_snapshot()
    assert first["registry_read_at"] == first["observed_at"]
    assert first["snapshot_hash"] == second["snapshot_hash"]
    # 기록 시점에 원장을 다시 읽어 값을 바꿔 넣는 코드가 없다.
    assert "build_server_ledger" not in inspect.getsource(pc.record_prompt_provenance)


async def test_failed_build_record_row_fields(barrier_env, monkeypatch):
    """main 조립 실패 → 그 요청 값 그대로 INSERT 행에 available:false 로 남는다."""
    barrier_env["gate"].set()
    helper_out: dict = {}
    await _build(helper_out)  # 먼저 돈 helper 조립 — 이 값이 새어 들면 안 된다

    def _boom(*_a, **_k):
        raise RuntimeError("layer1 exploded")

    monkeypatch.setattr(cb, "build_layer1", _boom)
    ledger = await _main_path_snippet()
    row, _ = await _record(ledger)
    led = row["server_ledger"]
    assert led["available"] is False
    assert led["reason"] == "context_build_failed"
    assert "snapshot_hash" not in led and "registry_read_at" not in led
    assert helper_out["snapshot_hash"] not in json.dumps(row)
