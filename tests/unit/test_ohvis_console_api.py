"""오비스 창 콘솔 API 단위 테스트 (AADS-OHVIS-CONSOLE-ROUTE-20260917).

지키려는 계약 넷.

1. 화면 다섯 칸이 한 응답에 다 온다 — 칸 하나가 조용히 사라지면 화면은
   "빈 상태" 와 "장애" 를 구분하지 못한다.
2. **테넌트 경계.** 87개 테넌트가 같은 DB 를 쓴다. run/승인/프레임 조회는
   호출자의 tenant 로 정확히 좁혀야 한다.
3. **시크릿과 목업 수치가 응답·화면에 없다.** `recipe_runs.inputs` 에는
   자격증명이 들어오고, 목업의 예시 금액(88,600원)은 실화면에 남으면 안 된다.
4. 승인 결정은 `work_recipe.approval` 에 위임한다 — 재확인 문구 검사를
   콘솔이 따로 구현하면 둘이 갈라진다.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import ohvis_console as console
from app.services.work_recipe.approval import ApprovalError, ConfirmationRequired

TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"
OTHER_TENANT = "bfee6f4e-8112-412b-9240-e8b6d539f160"
RUN_ID = "11111111-2222-3333-4444-555555555555"

REAL_DASHBOARD_PAGE = Path("../aads-dashboard/src/app/ohvis/page.tsx")
DASHBOARD_PAGE = (
    REAL_DASHBOARD_PAGE
    if REAL_DASHBOARD_PAGE.exists()
    else Path("aads-dashboard/src/app/ohvis/page.tsx")
)
REAL_DASHBOARD_API = Path("../aads-dashboard/src/lib/api.ts")
DASHBOARD_API = (
    REAL_DASHBOARD_API if REAL_DASHBOARD_API.exists() else Path("aads-dashboard/src/lib/api.ts")
)
REAL_DASHBOARD_NAV = Path("../aads-dashboard/src/lib/navigation.ts")
DASHBOARD_NAV = (
    REAL_DASHBOARD_NAV if REAL_DASHBOARD_NAV.exists() else Path("aads-dashboard/src/lib/navigation.ts")
)


# ─────────────────────────────────────────────────────────── 가짜 커넥션


class FakeConn:
    """fetch/fetchrow 를 대본대로 돌려주고 SQL·인자를 기록하는 커넥션."""

    def __init__(self, rows: list | None = None, row: dict | None = None, values: list | None = None):
        self._rows = rows if rows is not None else []
        self._row = row
        self._values = values if values is not None else []
        self.queries: list[tuple[str, tuple]] = []

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        return self._values.pop(0) if self._values else None

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        result = self._rows.pop(0) if self._rows else []
        return result

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if isinstance(self._row, list):
            return self._row.pop(0) if self._row else None
        row, self._row = self._row, None
        return row


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def ctx(tenant: str = TENANT, *, admin: bool = True) -> dict:
    return {
        "tenant": {"id": tenant},
        "membership": {"user_id": "u-1", "role": "owner"},
        "user": {"email": "moong76@gmail.com", "is_internal_admin": admin},
    }


# ─────────────────────────────────────────────── 1. 다섯 칸 계약 / 라우팅


def test_summary_exposes_exactly_five_sections():
    assert console.SECTION_KEYS == ("conversation", "live", "approvals", "schedules", "reports")
    assert len(set(console.SECTION_KEYS)) == 5


def test_router_is_registered_under_ohvis_console():
    paths = {route.path for route in console.router.routes}
    assert "/ohvis/console/summary" in paths
    assert "/ohvis/console/approvals/{approval_id}/decision" in paths

    main_source = Path("app/main.py").read_text()
    assert "from app.api.ohvis_console import router as ohvis_console_router" in main_source
    assert 'app.include_router(ohvis_console_router, prefix="/api/v1"' in main_source


def test_console_does_not_duplicate_existing_task_or_approval_endpoints():
    """지시 생성·승인 판단은 기존 경로에 위임한다 (지시서 2항)."""
    source = Path("app/api/ohvis_console.py").read_text()
    # 같은 뜻의 생성 엔드포인트를 새로 만들지 않았다 — 라우트는 둘뿐이다
    assert len(console.router.routes) == 2
    assert "INSERT INTO ohvis_tasks" not in source
    assert "INSERT INTO recipe_approvals" not in source
    assert "UPDATE recipe_approvals" not in source
    # 승인 판단은 서비스 호출로만 한다
    assert "resolve_approval(" in source
    assert "list_pending(" in source


# ───────────────────────────────────────────────── 2. 테넌트 경계 / 조회


def test_active_run_query_is_scoped_to_caller_tenant():
    conn = FakeConn(row={
        "id": RUN_ID,
        "recipe_name": "ably_orders",
        "domain": "partners.a-bly.com",
        "recipe_version": 3,
        "status": "running",
        "inputs": json.dumps({"password": "sk-super-secret-value-1234567890"}),
        "llm_calls": 2,
        "error": "",
        "failed_step_seq": None,
        "blocked_step_seq": None,
        "blocked_risk": "",
        "duration_ms": 6200,
        "triggered_by": "ceo",
        "task_id": "t-1",
        "started_at": datetime(2026, 9, 17, 8, 10, tzinfo=timezone.utc),
        "finished_at": None,
    })
    run = asyncio.run(console._fetch_active_run(conn, TENANT))

    query, args = conn.queries[0]
    assert "tenant_id IS NOT DISTINCT FROM $1::uuid" in query
    assert args[0] == TENANT
    assert run is not None
    assert run["id"] == RUN_ID
    # inputs 는 절대 나가지 않는다 — 레시피 입력에는 자격증명이 들어온다.
    assert "inputs" not in run
    assert "password" not in json.dumps(run)


def test_active_run_returns_none_without_rows():
    conn = FakeConn(row=None)
    assert asyncio.run(console._fetch_active_run(conn, TENANT)) is None


def test_live_frame_returns_metadata_only_and_is_tenant_scoped():
    conn = FakeConn(row={
        "task_id": "29778d5d-2a43-4133-9b53-2c983ab8a97c",
        "frame_url": "",
        "media_type": "image/jpeg",
        "width": 1280,
        "height": 720,
        "current_url": "https://partners.a-bly.com/orders",
        "page_title": "주문관리",
        "current_step": "목록 수집",
        "captured_at": datetime(2026, 9, 17, 8, 11, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 17, 8, 11, tzinfo=timezone.utc),
        "has_image": True,
    })
    frame = asyncio.run(console._fetch_live_frame(conn, TENANT))

    query, args = conn.queries[0]
    assert "WHERE tenant_id = $1::uuid" in query
    assert args[0] == TENANT
    assert frame is not None
    assert frame["has_image"] is True
    # 2.5MB base64 를 3초 폴링에 얹지 않는다
    assert "frame_base64" not in frame
    assert "frame_base64" not in query.split("FROM")[0].replace("(frame_base64 <> '')", "")


def test_timeline_hides_server_screenshot_paths():
    conn = FakeConn(rows=[[
        {
            "seq": 1,
            "phase": "step",
            "action": "navigate",
            "risk": "READ",
            "status": "success",
            "attempts": 1,
            "duration_ms": 400,
            "llm_calls": 0,
            "error": "",
            "url": "https://partners.a-bly.com/login",
            "screenshot_path": "/var/lib/aads/captures/run-1/step-1.png",
            "approval_id": None,
            "created_at": datetime(2026, 9, 17, 8, 10, tzinfo=timezone.utc),
        },
        {
            "seq": 2,
            "phase": "step",
            "action": "click",
            "risk": "IRREVERSIBLE",
            "status": "blocked",
            "attempts": 1,
            "duration_ms": 0,
            "llm_calls": 1,
            "error": "",
            "url": "",
            "screenshot_path": "",
            "approval_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "created_at": datetime(2026, 9, 17, 8, 11, tzinfo=timezone.utc),
        },
    ]])
    timeline = asyncio.run(console._fetch_timeline(conn, RUN_ID, 100))

    assert [step["seq"] for step in timeline] == [1, 2]
    assert timeline[0]["has_screenshot"] is True
    assert timeline[1]["has_screenshot"] is False
    assert "screenshot_path" not in json.dumps(timeline)
    assert timeline[1]["approval_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_approvals_drop_other_tenants(monkeypatch):
    async def fake_list_pending(*, limit):
        return [
            {
                "id": "a-mine", "tenant_id": TENANT, "run_id": RUN_ID, "step_seq": 4,
                "action": "click", "risk": "IRREVERSIBLE", "status": "pending",
                "summary": "결제하기", "requested_by": "player", "confirm_text": "쿠팡 결제 실행에 동의합니다",
                "requested_at": datetime(2026, 9, 17, 8, 12, tzinfo=timezone.utc), "expires_at": None,
            },
            {
                "id": "a-theirs", "tenant_id": OTHER_TENANT, "run_id": RUN_ID, "step_seq": 2,
                "action": "click", "risk": "WRITE_EXTERNAL", "status": "pending",
                "summary": "남의 테넌트", "requested_by": "player", "confirm_text": "",
                "requested_at": datetime(2026, 9, 17, 8, 12, tzinfo=timezone.utc), "expires_at": None,
            },
            {
                "id": "a-global", "tenant_id": None, "run_id": RUN_ID, "step_seq": 1,
                "action": "click", "risk": "READ", "status": "pending",
                "summary": "tenant 없는 전역 실행", "requested_by": "player", "confirm_text": "",
                "requested_at": datetime(2026, 9, 17, 8, 12, tzinfo=timezone.utc), "expires_at": None,
            },
        ]

    monkeypatch.setattr(console, "list_pending", fake_list_pending)
    result = asyncio.run(console._fetch_approvals(TENANT, 10))

    assert result["count"] == 1
    assert [item["id"] for item in result["items"]] == ["a-mine"]
    # IRREVERSIBLE 은 재확인 문구를 화면에 보여줘야 승인자가 입력할 수 있다
    assert result["items"][0]["requires_confirmation"] is True
    assert result["items"][0]["confirm_text"] == "쿠팡 결제 실행에 동의합니다"


def test_reports_scope_to_today_and_session():
    conn = FakeConn(rows=[[]])
    asyncio.run(console._fetch_reports(conn, None, 10))
    query, args = conn.queries[0]
    assert "AT TIME ZONE 'Asia/Seoul'" in query
    assert "session_id = $2" not in query
    assert args == (10,)

    session_uuid = console._uuid_or_none("3f8b3f2c-0000-4000-8000-000000000001")
    conn2 = FakeConn(rows=[[]])
    asyncio.run(console._fetch_reports(conn2, session_uuid, 10))
    query2, args2 = conn2.queries[0]
    assert "session_id = $2" in query2
    assert args2 == (10, session_uuid)


def test_conversation_orders_oldest_first_and_derives_quick_commands():
    rows = [
        {"id": "t3", "session_id": None, "title": "광고분석 보고해", "status": "running",
         "task_type": "ceo_directive", "steps": "[]", "result": None, "ohvis_judgement": "",
         "cost_usd": 0, "created_at": None, "completed_at": None, "reported_at": None},
        {"id": "t2", "session_id": None, "title": "주문건 확인해", "status": "done",
         "task_type": "ceo_directive", "steps": '[{"name":"a"}]', "result": '{"summary":"37건"}',
         "ohvis_judgement": "READ", "cost_usd": 0.1, "created_at": None,
         "completed_at": None, "reported_at": None},
        {"id": "t1", "session_id": None, "title": "주문건 확인해", "status": "done",
         "task_type": "ceo_directive", "steps": "[]", "result": None, "ohvis_judgement": "",
         "cost_usd": 0, "created_at": None, "completed_at": None, "reported_at": None},
    ]
    conn = FakeConn(rows=[rows])
    result = asyncio.run(console._fetch_conversation(conn, None, 30))

    # DB 는 최신순으로 주고 화면은 대화처럼 오래된 것부터 읽는다
    assert [m["id"] for m in result["messages"]] == ["t1", "t2", "t3"]
    assert result["messages"][1]["result_summary"] == "37건"
    assert result["messages"][1]["step_count"] == 1
    # 목업의 데모 버튼 3개 대신 실제로 내렸던 지시가 들어간다 (중복 제거)
    assert result["quick_commands"] == ["광고분석 보고해", "주문건 확인해"]


def test_session_resolver_rejects_a_session_from_another_tenant():
    """ohvis_tasks 에는 tenant 컬럼이 없다 — 세션 소유 검사가 유일한 경계다."""
    session = console._uuid_or_none("3f8b3f2c-0000-4000-8000-000000000001")
    fallback = console._uuid_or_none("3f8b3f2c-0000-4000-8000-0000000000ff")

    # 요청한 세션이 내 테넌트 것이면 그대로 쓴다
    conn = FakeConn(values=[session])
    assert asyncio.run(console._resolve_session_id(conn, TENANT, "u-1", session)) == session
    owned_query, owned_args = conn.queries[0]
    assert "tenant_id IS NOT DISTINCT FROM $2::uuid" in owned_query
    assert owned_args == (session, TENANT)

    # 남의 테넌트 세션이면(소유 조회가 빈손) 최근 내 세션으로 떨어진다
    conn2 = FakeConn(values=[None, fallback])
    assert asyncio.run(console._resolve_session_id(conn2, TENANT, "u-1", session)) == fallback
    assert len(conn2.queries) == 2
    assert "ORDER BY (user_id = $2) DESC" in conn2.queries[1][0]


def test_session_resolver_returns_none_when_tenant_has_no_session():
    conn = FakeConn(values=[None])
    assert asyncio.run(console._resolve_session_id(conn, TENANT, "u-1", None)) is None


# ─────────────────────────────────────────────── 3. 마스킹 / 값 다듬기


def test_free_text_is_masked_and_truncated():
    assert "sk-" in console._text("token sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in console._text(
        "token sk-abcdefghijklmnopqrstuvwxyz0123456789"
    )
    assert len(console._text("가" * 900)) == 500
    assert console._text(None) == ""


def test_json_helpers_accept_asyncpg_strings():
    assert console._json_obj('{"a": 1}') == {"a": 1}
    assert console._json_obj("[1,2]") == {}
    assert console._json_obj(None) == {}
    assert console._json_list('[{"x":1}]') == [{"x": 1}]
    assert console._json_list("{}") == []
    assert console._uuid_or_none("not-a-uuid") is None


def test_naive_loop_timestamps_get_a_timezone():
    """ohvis_loops 는 timestamp(무 timezone) 다 — 화면이 시각을 밀어 쓰면 안 된다."""
    value = console._iso(datetime(2026, 9, 17, 8, 10))
    assert value is not None and value.endswith("+00:00")


def test_kpi_counts_match_timeline():
    timeline = [
        {"llm_calls": 1, "status": "success"},
        {"llm_calls": 0, "status": "blocked"},
        {"llm_calls": 0, "status": "success"},
    ]
    kpis = console._kpis({"llm_calls": 4}, timeline, approval_count=2)
    assert kpis == {"llm_calls": 4, "steps": 3, "approvals": 2, "blocked": 1}
    # run 이 없으면 단계 합으로 채운다
    assert console._kpis(None, timeline, 0)["llm_calls"] == 1


def test_loop_card_maps_status_to_on_off():
    active = console._loop_card({
        "id": 7, "original_command": "판매채널 4종 수집", "loop_type": "monitor",
        "project": "AADS", "status": "active", "interval_seconds": 3600,
        "current_iteration": 5, "max_iterations": 10, "consecutive_failures": 0,
        "last_result": '{"success": true}', "completed_at": None,
        "started_at": datetime(2026, 9, 17, 4, 0), "next_run_at": None, "created_at": None,
    })
    assert active["enabled"] is True and active["failed"] is False
    assert active["last_success"] is True

    failed = console._loop_card({
        "id": 8, "original_command": "광고 리포트", "loop_type": "task", "project": "AADS",
        "status": "failed", "interval_seconds": None, "current_iteration": 0,
        "max_iterations": None, "consecutive_failures": 3, "last_result": None,
        "completed_at": None, "started_at": None, "next_run_at": None, "created_at": None,
    })
    assert failed["enabled"] is False and failed["failed"] is True
    assert failed["last_success"] is None


# ──────────────────────────────────────────── 4. 엔드포인트 동작


def test_summary_endpoint_returns_all_sections(monkeypatch):
    conn = FakeConn(
        rows=[[], [], []],   # conversation / timeline / reports
        row=[None, None],    # run 없음 / frame 없음
        values=[None],       # 세션 해석: 붙을 채팅 세션 없음
    )
    monkeypatch.setattr(console, "get_pool", lambda: FakePool(conn))

    async def fake_list_pending(*, limit):
        return []

    async def fake_list_active_loops(**kwargs):
        return []

    monkeypatch.setattr(console, "list_pending", fake_list_pending)
    monkeypatch.setattr(console, "list_active_loops", fake_list_active_loops)

    payload = asyncio.run(console.get_console_summary(session_id=None, limit=30, context=ctx()))

    for key in console.SECTION_KEYS:
        assert key in payload, f"섹션 {key} 가 빠졌다"
    assert payload["live"]["is_live"] is False
    assert payload["live"]["run"] is None
    assert payload["approvals"] == {"count": 0, "items": []}
    assert payload["conversation"]["messages"] == []
    assert payload["reports"] == {"count": 0, "items": []}
    # 목업 수치는 한 톨도 없다
    assert "88,600" not in json.dumps(payload, ensure_ascii=False)


def test_summary_survives_missing_loop_table(monkeypatch):
    conn = FakeConn(rows=[[], [], []], row=[None, None], values=[None])
    monkeypatch.setattr(console, "get_pool", lambda: FakePool(conn))

    async def fake_list_pending(*, limit):
        return []

    async def boom(**kwargs):
        raise RuntimeError("relation ohvis_loops does not exist")

    monkeypatch.setattr(console, "list_pending", fake_list_pending)
    monkeypatch.setattr(console, "list_active_loops", boom)

    payload = asyncio.run(console.get_console_summary(session_id=None, limit=30, context=ctx()))
    assert payload["schedules"] == {"count": 0, "items": []}


def test_non_internal_admin_cannot_open_the_console():
    with pytest.raises(HTTPException) as caught:
        asyncio.run(console.require_console_admin(context=ctx(admin=False)))
    assert caught.value.status_code == 403

    assert asyncio.run(console.require_console_admin(context=ctx()))["tenant"]["id"] == TENANT


def _approval_row(tenant: str = TENANT) -> dict:
    return {
        "id": "a-1", "tenant_id": tenant, "run_id": RUN_ID, "step_seq": 4,
        "action": "click", "risk": "IRREVERSIBLE", "status": "pending",
        "summary": "결제하기", "requested_by": "player",
        "confirm_text": "쿠팡 결제 실행에 동의합니다",
        "requested_at": datetime(2026, 9, 17, 8, 12, tzinfo=timezone.utc), "expires_at": None,
    }


def test_decision_rejects_another_tenants_approval(monkeypatch):
    async def fake_get_approval(approval_id):
        return _approval_row(OTHER_TENANT)

    monkeypatch.setattr(console, "get_approval", fake_get_approval)
    body = console.ApprovalDecisionIn(decision="approve", confirm_text="x")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(console.decide_console_approval("a-1", body, context=ctx()))
    assert caught.value.status_code == 404


def test_decision_delegates_to_resolve_approval(monkeypatch):
    seen: dict = {}

    async def fake_get_approval(approval_id):
        return _approval_row()

    async def fake_resolve(approval_id, decision, decided_by, reason, *, confirm_text=None):
        seen.update(
            approval_id=approval_id, decision=decision, decided_by=decided_by,
            reason=reason, confirm_text=confirm_text,
        )
        row = _approval_row()
        row["status"] = "approved"
        return row

    monkeypatch.setattr(console, "get_approval", fake_get_approval)
    monkeypatch.setattr(console, "resolve_approval", fake_resolve)

    body = console.ApprovalDecisionIn(
        decision="approve", confirm_text="쿠팡 결제 실행에 동의합니다", reason=""
    )
    result = asyncio.run(console.decide_console_approval("a-1", body, context=ctx()))

    assert seen["decision"] == "approve"
    assert seen["confirm_text"] == "쿠팡 결제 실행에 동의합니다"
    assert seen["decided_by"] == "moong76@gmail.com"
    assert result["approval"]["status"] == "approved"


@pytest.mark.parametrize(
    "raised, expected_status",
    [
        (ConfirmationRequired("재확인 문구가 일치하지 않습니다"), 400),
        (ApprovalError("이미 결정된 승인입니다"), 409),
    ],
)
def test_decision_translates_service_errors(monkeypatch, raised, expected_status):
    async def fake_get_approval(approval_id):
        return _approval_row()

    async def fake_resolve(*args, **kwargs):
        raise raised

    monkeypatch.setattr(console, "get_approval", fake_get_approval)
    monkeypatch.setattr(console, "resolve_approval", fake_resolve)

    body = console.ApprovalDecisionIn(decision="approve", confirm_text="틀린 문구")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(console.decide_console_approval("a-1", body, context=ctx()))
    assert caught.value.status_code == expected_status


# ────────────────────────────────────────────── 5. 대시보드 화면 정적 검사


def test_ohvis_route_exists_and_uses_the_single_summary_endpoint():
    source = DASHBOARD_PAGE.read_text()
    api_source = DASHBOARD_API.read_text()

    assert "getOhvisConsoleSummary" in source
    assert "getOhvisConsoleSummary" in api_source
    assert "/ohvis/console/summary" in api_source
    # 지시 전송은 기존 POST /ohvis/tasks 를 그대로 쓴다
    assert "createOhvisTask" in source
    assert '"/ohvis/tasks"' in api_source
    # 승인 결정은 콘솔 위임 라우트로
    assert "decideOhvisConsoleApproval" in source
    assert "/ohvis/console/approvals/" in api_source


def test_ohvis_page_keeps_mockup_layout_and_empty_states():
    source = DASHBOARD_PAGE.read_text()

    for text in (
        "오비스가 보는 화면이 여기 그대로 표시됩니다",
        "대기 중인 승인이 없습니다.",
        "오비스 대화",
        "자동실행",
        "승인 대기",
        "오늘 보고",
        "실행 단계 · 감사 기록(append-only)",
        "오비스에게 지시…",
        "보내기",
    ):
        assert text in source, f"목업 구조/문구 누락: {text}"

    # 3분할 폭(좌 330 / 우 300)과 목업 팔레트
    assert "330" in source and "300" in source
    assert "#0f1117" in source and "#151722" in source and "#2d3148" in source


def test_ohvis_page_polls_fast_only_while_a_run_is_live():
    source = DASHBOARD_PAGE.read_text()
    assert "POLL_LIVE_MS = 3000" in source
    assert "POLL_IDLE_MS = 15000" in source
    assert "isLive ? POLL_LIVE_MS : POLL_IDLE_MS" in source


def test_ohvis_page_collapses_side_panes_on_mobile():
    source = DASHBOARD_PAGE.read_text()
    assert "(max-width: 900px)" in source
    # 모바일 기본 노출은 중앙 라이브
    assert 'useState<MobileTab>("live")' in source
    assert "showChat" in source and "showRight" in source


def test_ohvis_page_carries_no_mockup_demo_data():
    """AC-4: 목업 수치·데모 시나리오가 실화면에 남으면 안 된다."""
    source = DASHBOARD_PAGE.read_text()
    for demo in ("88,600", "88600", "쌈장", "열정국밥", "해찬들", "run-A1042", "run-C2071", "run-D3355"):
        assert demo not in source, f"목업 데모 데이터가 남아 있다: {demo}"
    assert "88,600" not in Path("app/api/ohvis_console.py").read_text()


def test_ohvis_page_never_mints_its_own_session_uuid():
    """ohvis_tasks.session_id → chat_sessions(id) FK. 지어낸 id 는 500 을 부른다."""
    source = DASHBOARD_PAGE.read_text()
    assert "crypto.randomUUID" not in source
    # 지시는 서버가 확인해 준 세션으로만 나간다
    assert "summary?.conversation.session_id" in source
    assert "session_id: activeSessionId" in source


def test_ohvis_is_registered_in_dashboard_navigation():
    nav_source = DASHBOARD_NAV.read_text()
    assert '{ href: "/ohvis"' in nav_source
    assert "오비스 창" in nav_source


def test_browser_tasks_reference_page_is_untouched():
    """참조만 하고 고치지 않는다 — 기존 화면 동작을 바꾸지 않기로 했다."""
    real = Path("../aads-dashboard/src/app/browser-tasks/page.tsx")
    page = real if real.exists() else Path("aads-dashboard/src/app/browser-tasks/page.tsx")
    source = page.read_text()
    assert "getOhvisConsoleSummary" not in source
    assert "ohvis/console" not in source
