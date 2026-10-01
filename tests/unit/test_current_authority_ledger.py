"""PRD aads-current-authority-context v1.0.0 — P0 회귀 가드.

과거 문서를 현재 구성처럼 읽는 사고(2026-06-23 "3서버")를 막는 장치가
**실제로 프롬프트에 들어가고 그 턴의 provenance 에 남는지** 를 본다.
구현이 되돌아가면 여기서 깨진다.

검사 범위
  1. 원장 스냅샷이 코드 레지스트리(`list_ledger_servers`)에서 나오고
     원장 4대 / 감시 3대를 **별개 값**으로 센다.
  2. 구역 텍스트에 출처·구성 조회시각·스냅샷 hash 가 있고, 파일 변경일을
     건강 실측시각과 구분해 적는다.
  3. 두 진입점이 같은 함수를 호출하고, 60초 TTL 캐시 밖에서 매 턴 새로 만든다.
  4. 조회 실패를 다른 소스로 조용히 덮지 않는다(DB 없음/오류 경로 포함).
  5. provenance 가 **세션별**로 전달된다 — 모듈 전역 "직전 값" 이 없고,
     두 세션이 동시에 조립해도 서로의 원장이 섞이지 않는다.
  6. provenance 테이블이 없는 조기-return 경로에서도 증거가 붙는다.
  7. 꼬리 블록은 비캐시이고 cache_control breakpoint 는 한도 안에 있다.
  8. 승인 정본 포인터는 보존되고, 권한 실패·DB 오류는 fail-closed 다.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.services import context_builder as cb
from app.services import prompt_compiler as pc
from app.services.server_registry import CANONICAL_SERVER_IDS, LEDGER_SERVER_IDS


# ─── 1. 스냅샷 ───────────────────────────────────────────────────────────────

def test_snapshot_counts_ledger_and_health_separately():
    snap = cb.build_server_ledger_snapshot()

    assert snap["ledger_count"] == len(LEDGER_SERVER_IDS) == 4
    assert snap["health_monitored_count"] == len(CANONICAL_SERVER_IDS) == 3
    # 두 수치가 같아지면 "감시 안 하는 서버" 가 사라졌다는 뜻이다.
    assert snap["ledger_count"] != snap["health_monitored_count"]

    ids = [s["server_id"] for s in snap["servers"]]
    assert ids == list(LEDGER_SERVER_IDS)
    for server in snap["servers"]:
        assert server["host"], f"{server['server_id']} 주소 누락"
        assert server["health_monitored"] is (server["server_id"] in CANONICAL_SERVER_IDS)


def test_snapshot_source_is_code_registry_not_db():
    snap = cb.build_server_ledger_snapshot()
    assert "server_registry.py" in snap["source"]
    assert "list_ledger_servers" in snap["source"]


def test_snapshot_hash_covers_config_only():
    """조회시각은 달라지고 hash 는 같아야 한다."""
    first = cb.build_server_ledger_snapshot()
    second = cb.build_server_ledger_snapshot()
    assert first["snapshot_hash"] == second["snapshot_hash"]
    assert len(first["snapshot_hash"]) == 16


def test_snapshot_hash_changes_when_ledger_changes(monkeypatch):
    base = cb.build_server_ledger_snapshot()["snapshot_hash"]
    monkeypatch.setattr(
        "app.services.server_registry.list_ledger_servers",
        lambda: [{"id": "x", "host": "1.2.3.4", "projects": ["X"], "display_name": "x"}],
    )
    assert cb.build_server_ledger_snapshot()["snapshot_hash"] != base


# ─── 2. 구역 텍스트 ─────────────────────────────────────────────────────────

def test_section_reports_source_read_time_and_hash():
    section = cb.build_server_ledger_block().section
    snap = cb.build_server_ledger_snapshot()

    assert "현재 서버 원장" in section
    assert snap["source"] in section
    assert "구성 조회시각" in section
    assert snap["snapshot_hash"] in section
    assert "원장 서버 4대" in section
    assert "건강 감시 대상 3대" in section


def test_section_separates_file_mtime_from_health_observation():
    section = cb.build_server_ledger_block().section
    assert "원장 파일 변경일" in section
    # 파일 변경일을 건강 실측시각으로 읽지 말라는 경고가 같이 있어야 한다.
    assert "건강 실측시각이 아니다" in section
    assert "실측시각이 아니다" in section


def test_registry_read_at_is_not_the_file_mtime():
    """구성 조회시각 · 파일 변경일은 **다른 값**이다 (root 검수 2026-10-01)."""
    prov = cb.build_server_ledger_block().provenance
    assert prov["registry_read_at"] == prov["observed_at"]
    assert prov["registry_file_mtime"] != prov["registry_read_at"]


def test_section_lists_every_ledger_server_with_address():
    section = cb.build_server_ledger_block().section
    for entry in cb.build_server_ledger_snapshot()["servers"]:
        assert entry["server_id"] in section
        assert entry["host"] in section


def test_section_marks_unmonitored_server_as_not_a_failure():
    section = cb.build_server_ledger_block().section
    assert "감시대상 아님" in section
    assert "장애로 해석하지" in section


def test_section_warns_against_historical_topology():
    assert "3서버" in cb.build_server_ledger_block().section


# ─── 3. 두 진입점 + 캐시 ────────────────────────────────────────────────────

@pytest.mark.parametrize("entrypoint", ["build_messages_context", "build"])
def test_both_entrypoints_inject_the_same_ledger(entrypoint):
    src = inspect.getsource(getattr(cb, entrypoint))
    assert "build_server_ledger_block()" in src, (
        f"{entrypoint} 가 공통 원장 함수를 호출하지 않는다"
    )
    # 텍스트만 받는 래퍼를 쓰면 provenance 가 사라진다.
    assert "build_server_ledger_section()" not in src


@pytest.mark.parametrize("entrypoint", ["build_messages_context", "build"])
def test_both_entrypoints_stash_provenance_for_their_session(entrypoint):
    src = inspect.getsource(getattr(cb, entrypoint))
    assert "_stash_ledger_provenance(session_id" in src


def test_ledger_is_not_inside_the_ttl_cache():
    """원장이 `_get_cached_or_build` 인자로 들어가면 60초간 낡는다."""
    for entrypoint in ("build_messages_context", "build"):
        for line in inspect.getsource(getattr(cb, entrypoint)).splitlines():
            if "build_server_ledger_block" in line:
                assert "_get_cached_or_build" not in line


def test_ledger_is_rebuilt_on_every_call_even_with_a_stale_l2_cache(monkeypatch):
    """Layer 2 가 60초 캐시에서 낡은 값을 줘도 원장은 매 턴 새로 읽는다."""
    real = cb.build_server_ledger_snapshot
    calls = []

    def _counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(cb, "build_server_ledger_snapshot", _counting)
    # L2 캐시에 낡은 값을 심어 둔다 — 원장은 이 캐시를 타지 않아야 한다.
    cb._layer_cache["l2:AADS:stale-session"] = (0.0, "낡은 Layer 2")
    for _ in range(3):
        cb.build_server_ledger_block()
    assert len(calls) == 3, "원장이 캐시되어 매 턴 재조회되지 않는다"


def test_layer2_no_longer_copies_server_config():
    """구성은 원장 구역이 책임진다. Layer 2 에 복사본을 두면 다시 갈라진다."""
    layer2 = asyncio.run(cb._build_layer2_dynamic("AADS", db_conn=None, session_id=""))
    assert "등록부" not in layer2
    assert "## 서버 " not in layer2
    # 시각과 워크스페이스는 그대로 남아야 한다.
    assert "현재 시각" in layer2

    src = inspect.getsource(cb._build_layer2_dynamic)
    assert "FROM server_registry" not in src
    # 관측(감시 결과)은 보존한다 — 구성만 뺀 것이다.
    assert "monitored_services" in src


def test_ledger_section_needs_no_database():
    """DB 없는 진입점에서도 원장 4대·감시 3대가 나온다."""
    section = cb.build_server_ledger_block().section
    assert "원장 서버 4대" in section
    assert "조회 실패" not in section


# ─── 4. 실패 경로 ───────────────────────────────────────────────────────────

def test_lookup_failure_is_declared_not_silently_substituted(monkeypatch):
    def _boom():
        raise RuntimeError("registry import blew up")

    monkeypatch.setattr("app.services.server_registry.list_ledger_servers", _boom)
    block = cb.build_server_ledger_block()

    assert "조회 실패" in block.section
    assert "단정하지 마라" in block.section
    # 실패했는데 서버 목록이 그대로 보이면 다른 소스를 권위처럼 쓴 것이다.
    assert "5.104.86.116" not in block.section
    # 실패도 증거로 남는다 — 그 턴의 서버 발언은 근거가 없었다는 뜻이다.
    assert block.provenance["available"] is False
    assert block.provenance["error"]
    assert "servers" not in block.provenance


# ─── 5. provenance 는 세션별 ────────────────────────────────────────────────

def test_no_module_level_last_provenance():
    """모듈 전역 '직전 값' 은 동시 세션을 섞는다 — 존재 자체를 금지한다."""
    assert not hasattr(cb, "_LEDGER_PROVENANCE_LAST")
    assert "_LEDGER_PROVENANCE_LAST" not in inspect.getsource(pc)


def test_ledger_provenance_reaches_prompt_compiler():
    block = cb.build_server_ledger_block()
    cb._stash_ledger_provenance("sess-reach", block.provenance)

    prov = pc._server_ledger_snapshot("sess-reach")
    assert prov["available"] is True
    assert prov["source"] == cb._LEDGER_SOURCE
    assert prov["ledger_count"] == 4
    assert prov["health_monitored_count"] == 3
    assert prov["snapshot_hash"]
    assert prov["registry_read_at"] == prov["observed_at"]
    assert prov["registry_file_mtime"]
    assert prov["server_ids"] == list(LEDGER_SERVER_IDS)
    # 본문(서버 전체 dict)은 provenance 에 싣지 않는다 — 기록이 비대해진다.
    assert "servers" not in prov


def test_two_concurrent_sessions_do_not_mix_provenance():
    """세션 B 의 조립이 세션 A 의 증거를 덮지 않는다 (root 검수 2026-10-01)."""
    a = cb.build_server_ledger_block()
    cb._stash_ledger_provenance("sess-A", {**a.provenance, "marker": "A"})
    # A 가 기록되기 **전에** B 가 조립을 끝내는 순서를 재현한다.
    b = cb.build_server_ledger_block()
    cb._stash_ledger_provenance("sess-B", {**b.provenance, "marker": "B"})

    assert pc._server_ledger_snapshot("sess-A")["marker"] == "A"
    assert pc._server_ledger_snapshot("sess-B")["marker"] == "B"


def test_taking_clears_the_slot_so_the_next_turn_cannot_inherit_it():
    cb._stash_ledger_provenance("sess-once", {"available": True, "marker": "once"})
    assert pc._server_ledger_snapshot("sess-once")["marker"] == "once"
    # 두 번째 턴에 원장 주입이 없었다면 증거도 없어야 한다.
    assert pc._server_ledger_snapshot("sess-once") == {}


def test_stash_is_bounded():
    for i in range(cb._LEDGER_STASH_MAX + 20):
        cb._stash_ledger_provenance(f"bound-{i}", {"available": True})
    assert len(cb._LEDGER_PROVENANCE_BY_SESSION) <= cb._LEDGER_STASH_MAX


# ─── 6. 조기-return 경로에도 증거가 붙는다 ──────────────────────────────────

class _NoTableConn:
    """provenance 테이블이 없는 환경(SDK 조기-return 경로)."""

    async def fetchval(self, *_a, **_k):
        return False

    async def execute(self, *_a, **_k):  # pragma: no cover - 호출되면 실패다
        raise AssertionError("테이블이 없으면 INSERT 하지 않는다")


def test_evidence_is_attached_even_when_the_table_is_missing():
    """INSERT 는 건너뛰어도 메모리 provenance 는 채워야 한다.

    예전에는 테이블 확인에서 바로 return 해서 `server_ledger` 가 붙지 않았고,
    호출자가 메모리 provenance 를 읽는 경로는 증거 없이 끝났다.
    """
    block = cb.build_server_ledger_block()
    cb._stash_ledger_provenance("sess-notable", block.provenance)
    compiled = pc.CompiledPrompt(system_prompt="x", provenance={})

    asyncio.run(
        pc.record_prompt_provenance(
            conn=_NoTableConn(),
            session_id="sess-notable",
            execution_id=None,
            intent="status_check",
            model="claude-opus-5",
            compiled_prompt=compiled,
        )
    )

    ledger = compiled.provenance.get("server_ledger")
    assert ledger, "조기-return 경로에서 원장 증거가 사라졌다"
    assert ledger["available"] is True
    assert ledger["ledger_count"] == 4
    assert ledger["health_monitored_count"] == 3


def test_table_check_happens_after_evidence_is_filled():
    src = inspect.getsource(pc.record_prompt_provenance)
    assert src.index("server_ledger") < src.index("_table_exists"), (
        "테이블 확인이 증거 채우기보다 앞서면 조기-return 에서 증거가 사라진다"
    )


# ─── 7. 꼬리 블록 · 캐시 breakpoint 한도 ────────────────────────────────────

def test_context_result_returns_ledger_provenance_in_its_value():
    assert "server_ledger" in cb.ContextResult.__dataclass_fields__
    assert "server_ledger=_ledger_block.provenance" in inspect.getsource(cb.build)


def test_tail_blocks_are_not_cached():
    """원장·시각 블록에 cache_control 을 붙이면 프리픽스 캐시가 전부 미스된다."""
    src = inspect.getsource(cb.build)
    tail = src.split("system_blocks = system_blocks + [")[1].split("]")[0]
    assert "server_ledger" in tail
    assert "cache_control" not in tail


def test_cache_breakpoints_stay_within_the_api_limit():
    from app.core.cache_config import build_cached_system_blocks

    blocks = build_cached_system_blocks("L1", "L2", "extras", memory_text="mem")
    cached = [b for b in blocks if b.get("cache_control")]
    # Anthropic API 는 cache_control breakpoint 를 4개까지만 받는다.
    assert len(cached) <= 4, f"breakpoint {len(cached)}개 — API 한도 초과"


# ─── 8. 승인 정본 포인터 · fail-closed ──────────────────────────────────────

class _ScopeConn:
    def __init__(self, *, scope=True, allowed=True, boom=False):
        self._scope, self._allowed, self._boom = scope, allowed, boom

    async def fetchrow(self, *_a, **_k):
        if self._boom:
            raise RuntimeError("db down")
        if not self._scope:
            return None
        return {"tenant_id": "11111111-1111-1111-1111-111111111111",
                "project_key": "AADS", "user_id": "22222222-2222-2222-2222-222222222222"}

    async def fetchval(self, *_a, **_k):
        return self._allowed


def _fake_brief(monkeypatch, docs):
    async def _brief(_conn, _tenant, _project):
        return docs

    monkeypatch.setattr("app.api.canonical_documents.approved_brief", _brief)
    monkeypatch.setattr(
        "app.api.canonical_documents.format_approved_brief",
        lambda items: "APPROVED: " + " / ".join(items),
    )


def test_approved_pointer_is_used_not_the_latest_draft(monkeypatch):
    """승인 v1 이 있고 더 최근 draft v2 가 있어도 승인본만 주입한다."""
    _fake_brief(monkeypatch, ["aads-current-authority-context v1.0.0 (approved)"])
    out = asyncio.run(cb._build_approved_document_layer("s-1", _ScopeConn()))
    assert "v1.0.0" in out
    assert "v2" not in out
    assert "draft" not in out.lower()


def test_missing_grant_fails_closed(monkeypatch):
    _fake_brief(monkeypatch, ["should not appear"])
    out = asyncio.run(cb._build_approved_document_layer("s-1", _ScopeConn(allowed=False)))
    assert out == "승인된 정본 문서 없음/조회 불가"
    assert "should not appear" not in out


def test_database_error_fails_closed(monkeypatch):
    _fake_brief(monkeypatch, ["should not appear"])
    out = asyncio.run(cb._build_approved_document_layer("s-1", _ScopeConn(boom=True)))
    assert out == "승인된 정본 문서 없음/조회 불가"


def test_no_session_or_no_db_fails_closed():
    assert asyncio.run(cb._build_approved_document_layer("", None)) == "승인된 정본 문서 없음/조회 불가"
    assert asyncio.run(cb._build_approved_document_layer("s-1", None)) == "승인된 정본 문서 없음/조회 불가"
