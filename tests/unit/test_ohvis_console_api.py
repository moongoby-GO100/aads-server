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

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return "OK"


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


def test_console_delegates_writes_to_existing_services():
    """SQL 을 여기서 다시 쓰지 않는다 — 기록도 승인도 기존 서비스에 위임한다.

    라우트 **개수**를 세지 않는다. 2026-09-18 에 command 와 recipes/run 이
    차례로 붙으면서 `== 3` 하드코딩이 두 번 깨졌고, 두 번 다 계약 위반이
    아니라 단순 카운트 불일치였다. 지켜야 할 것은 "필수 경로가 살아 있는가"와
    "여기서 SQL 을 다시 쓰지 않는가" 둘뿐이므로 그것만 단언한다.
    """
    source = Path("app/api/ohvis_console.py").read_text()
    assert {
        "/ohvis/console/summary",
        "/ohvis/console/command",
        "/ohvis/console/approvals/{approval_id}/decision",
        "/ohvis/console/recipes/run",
    } <= {route.path for route in console.router.routes}
    assert "INSERT INTO ohvis_tasks" not in source
    assert "INSERT INTO recipe_approvals" not in source
    assert "UPDATE recipe_approvals" not in source
    # 기록·완료·승인 판단은 전부 서비스 호출이다
    assert "create_ohvis_task(" in source
    assert "complete_ohvis_task(" in source
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
    dedicated = console._uuid_or_none("3f8b3f2c-0000-4000-8000-0000000000ff")

    # 요청한 세션이 내 테넌트 것이면 그대로 쓴다
    conn = FakeConn(values=[session])
    assert asyncio.run(console._resolve_session_id(conn, TENANT, "u-1", session)) == session
    owned_query, owned_args = conn.queries[0]
    assert "tenant_id IS NOT DISTINCT FROM $2::uuid" in owned_query
    assert owned_args == (session, TENANT)

    # 남의 테넌트 세션이면(소유 조회가 빈손) 오비스 전용 세션으로 떨어진다 —
    # (AADS-OHVIS-CONSOLE-SESSION-ROUTING-P0) "아무 최신 세션" 이 아니다.
    conn2 = FakeConn(values=[None, dedicated])
    assert asyncio.run(console._resolve_session_id(conn2, TENANT, "u-1", session)) == dedicated
    assert len(conn2.queries) == 2
    assert "$2 = ANY(tags)" in conn2.queries[1][0]
    assert conn2.queries[1][1] == (TENANT, console.OHVIS_SESSION_TAG)


def test_session_resolver_picks_dedicated_ohvis_session_not_arbitrary_latest():
    """(a) session_id 미전달 시 오비스 전용 세션이 선택된다.

    예전 폴백("테넌트 안에서 가장 최근 갱신된 아무 세션")은 무관한 워커 세션
    (예: "라일론 상세페이지 자동생성 담당")을 고를 수 있었다 — 실측 사고.
    """
    dedicated = console._uuid_or_none("3f8b3f2c-0000-4000-8000-0000000000dd")
    conn = FakeConn(values=[dedicated])

    result = asyncio.run(console._resolve_session_id(conn, TENANT, "u-1", None))

    assert result == dedicated
    query, args = conn.queries[0]
    assert "tenant_id IS NOT DISTINCT FROM $1::uuid" in query
    assert "$2 = ANY(tags)" in query
    assert args == (TENANT, console.OHVIS_SESSION_TAG)
    # 예전 폴백 쿼리(아무 최신 세션)는 더 이상 없다
    assert "ORDER BY (user_id" not in query


def test_session_resolver_creates_dedicated_session_when_none_exists():
    """(b) 전용 세션이 없으면 워크스페이스+세션을 만들고 그 id 로 task 가 만들어진다."""
    new_workspace = console._uuid_or_none("3f8b3f2c-0000-4000-8000-0000000000ee")
    new_session = console._uuid_or_none("3f8b3f2c-0000-4000-8000-0000000000fe")
    # 순서: 전용 세션 조회(없음) → 워크스페이스 조회(없음) → 워크스페이스 생성 → 세션 생성
    conn = FakeConn(values=[None, None, new_workspace, new_session])

    result = asyncio.run(console._resolve_session_id(conn, TENANT, "u-1", None))

    assert result == new_session
    assert len(conn.queries) == 4
    ws_lookup_query, ws_lookup_args = conn.queries[1]
    assert "chat_workspaces" in ws_lookup_query and "name = $2" in ws_lookup_query
    assert ws_lookup_args == (TENANT, console.OHVIS_WORKSPACE_NAME)
    ws_insert_query, ws_insert_args = conn.queries[2]
    assert "INSERT INTO chat_workspaces" in ws_insert_query
    assert ws_insert_args == (TENANT, console.OHVIS_WORKSPACE_NAME)
    session_insert_query, session_insert_args = conn.queries[3]
    assert "INSERT INTO chat_sessions" in session_insert_query
    assert session_insert_args == (
        TENANT, "u-1", new_workspace, console.OHVIS_SESSION_TITLE, console.OHVIS_SESSION_TAG,
    )


def test_session_resolver_returns_none_when_dedicated_session_creation_fails():
    """워크스페이스 생성마저 실패하면 None 을 돌려준다 — command 엔드포인트가 409 로 닫는다."""
    conn = FakeConn(values=[None, None, None])
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


# ──────────────────────────────── 4-b. 지시 실행 (AADS-OHVIS-CONSOLE-COMMAND)
#
# 이 절이 지키는 것은 하나다: **보낸 지시가 실제로 실행되는가.**
# 2026-09-18, 화면은 `POST /ohvis/tasks` 로 지시를 보냈고 그 경로는 INSERT 만
# 했다. pending 을 claim 하는 워커가 저장소에 없었으므로 행 하나
# (`e41f2c94-9571-4831-b687-f5997c1d91fe`)가 8분 뒤에도 pending·판단 NULL 로
# 남아 있었다. 실행 경로는 `trigger_ai_reaction()` 이고 이 테스트들은 그 체인이
# 끊기지 않았는지만 본다.


SESSION_MINE = console._uuid_or_none("3f8b3f2c-0000-4000-8000-0000000000aa")
SESSION_THEIRS = console._uuid_or_none("3f8b3f2c-0000-4000-8000-0000000000bb")


def _command_pool(monkeypatch, values: list) -> FakeConn:
    conn = FakeConn(values=values)
    monkeypatch.setattr(console, "get_pool", lambda: FakePool(conn))

    async def no_matching_recipe(*args, **kwargs):
        return None

    monkeypatch.setattr(console, "run_directive", no_matching_recipe)
    return conn


def test_command_is_behind_the_internal_admin_gate():
    """무인증·비관리자가 부르면 안 된다 — 이 라우트는 임의 문자열을 CEO 채팅
    세션에 밀어 넣는다. 게이트가 빠지면 그대로 원격 프롬프트 인젝션이다."""
    import inspect

    gate = inspect.signature(console.run_console_command).parameters["context"].default
    assert gate.dependency is console.require_console_admin

    with pytest.raises(HTTPException) as caught:
        asyncio.run(console.require_console_admin(context=ctx(admin=False)))
    assert caught.value.status_code == 403


def test_command_falls_back_to_own_tenant_session(monkeypatch):
    """남의 테넌트 세션 id 를 실어 보내도 내 테넌트 세션으로 떨어진다.

    `ohvis_tasks` 에는 tenant 컬럼이 없다 — 세션 소유 검사가 유일한 경계다.
    """
    # 소유 조회가 빈손(남의 세션) → 내 최근 세션으로 폴백
    conn = _command_pool(monkeypatch, [None, SESSION_MINE])
    created: dict = {}
    dispatched: dict = {}

    async def fake_create(*, session_id, title, task_type):
        created.update(session_id=session_id, title=title, task_type=task_type)
        return "task-1"

    async def fake_dispatch(session_id, title, task_id):
        dispatched.update(session_id=session_id, title=title, task_id=task_id)

    monkeypatch.setattr(console, "create_ohvis_task", fake_create)
    monkeypatch.setattr(console, "_dispatch_ai_reaction", fake_dispatch)

    body = console.ConsoleCommandIn(title="서버 헬스체크 결과만 보고해", session_id=SESSION_THEIRS)
    result = asyncio.run(console.run_console_command(body, context=ctx()))

    assert conn.queries[0][1] == (SESSION_THEIRS, TENANT)
    assert created["session_id"] == str(SESSION_MINE)
    assert dispatched["session_id"] == str(SESSION_MINE)
    assert result["session_id"] == str(SESSION_MINE)


def test_command_409s_when_the_tenant_has_no_chat_session(monkeypatch):
    _command_pool(monkeypatch, [None])

    async def unreachable(**kwargs):
        raise AssertionError("세션이 없으면 행을 만들면 안 된다 (FK 위반)")

    monkeypatch.setattr(console, "create_ohvis_task", unreachable)

    body = console.ConsoleCommandIn(title="아무거나")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(console.run_console_command(body, context=ctx()))
    assert caught.value.status_code == 409
    assert caught.value.detail == "no_chat_session"


def test_command_creates_a_running_row_and_triggers_the_ai(monkeypatch):
    """성공 경로. 행이 running 으로 생기고 트리거가 **그 task_id 로** 나간다.

    task_id 를 빠뜨리면 반응이 끝나도 `complete_task()` 가 돌지 않아서 행이
    영원히 running 이다 — 화면은 "돌고 있다" 고 거짓말한다.
    """
    _command_pool(monkeypatch, [SESSION_MINE])
    created: dict = {}
    triggered: dict = {}

    async def fake_create(*, session_id, title, task_type):
        created.update(session_id=session_id, title=title, task_type=task_type)
        return "task-42"

    async def fake_trigger(session_id, system_message, ohvis_task_id=None, **kwargs):
        triggered.update(
            session_id=session_id, system_message=system_message, ohvis_task_id=ohvis_task_id
        )
        return None

    from app.services import chat_service

    monkeypatch.setattr(console, "create_ohvis_task", fake_create)
    monkeypatch.setattr(chat_service, "trigger_ai_reaction", fake_trigger)

    body = console.ConsoleCommandIn(title="  서버 헬스체크 결과만 보고해  ", session_id=SESSION_MINE)
    result = asyncio.run(console.run_console_command(body, context=ctx()))

    # 기록: ohvis_task_manager.create_task 가 status='running' 으로 넣는다
    assert created == {
        "session_id": str(SESSION_MINE),
        "title": "서버 헬스체크 결과만 보고해",
        "task_type": "ceo_directive",
    }
    # 실행: 같은 task_id 로 채팅 트리거가 나갔다
    assert triggered["ohvis_task_id"] == "task-42"
    assert triggered["session_id"] == str(SESSION_MINE)
    assert triggered["system_message"] == "[오비스 창 지시] 서버 헬스체크 결과만 보고해"
    assert result == {
        "task_id": "task-42",
        "session_id": str(SESSION_MINE),
        "status": "running",
    }


def test_command_runs_matching_recipe_and_closes_ohvis_task(monkeypatch):
    """정확히 매칭되는 레시피는 LLM 채팅으로 보내지 않고 결과를 task에 기록한다."""
    _command_pool(monkeypatch, [SESSION_MINE])
    seen: dict = {}

    async def fake_create(*, session_id, title, task_type):
        return "task-recipe"

    class RecipeResult:
        def to_dict(self):
            return {"status": "success", "run_id": RUN_ID, "llm_calls": 0}

    async def fake_run(directive, tenant_id, **kwargs):
        seen.update(directive=directive, tenant_id=tenant_id, **kwargs)
        return RecipeResult()

    async def fake_complete(task_id, *, status=None, result=None, ohvis_judgement=None):
        seen.update(
            completed_task_id=task_id,
            completed_status=status,
            completed_result=result,
            ohvis_judgement=ohvis_judgement,
        )
        return True

    async def should_not_dispatch(*args, **kwargs):
        raise AssertionError("매칭된 레시피를 채팅 LLM으로 보내면 안 된다")

    monkeypatch.setattr(console, "create_ohvis_task", fake_create)
    monkeypatch.setattr(console, "run_directive", fake_run)
    monkeypatch.setattr(console, "complete_ohvis_task", fake_complete)
    monkeypatch.setattr(console, "_dispatch_ai_reaction", should_not_dispatch)

    body = console.ConsoleCommandIn(title="aads_login_public_check")
    result = asyncio.run(console.run_console_command(body, context=ctx()))

    assert seen["task_id"] == "task-recipe"
    assert seen["tenant_id"] == TENANT
    assert seen["completed_status"] == "done"
    assert seen["completed_result"]["run_id"] == RUN_ID
    assert result["execution"] == "work_recipe"
    assert result["status"] == "done"


def test_recipe_run_api_exposes_approval_wait_as_chat_artifact(monkeypatch):
    """Native/auth handoffs must remain visible to the chat client, not logs only."""
    from app.services.work_recipe.approval import ApprovalRequired

    async def waiting_for_human(*args, **kwargs):
        raise ApprovalRequired(
            "approval-42", run_id=RUN_ID, step_seq=2, risk_level="WRITE_EXTERNAL", summary="로그인 승인"
        )

    monkeypatch.setattr(console, "run_directive", waiting_for_human)
    body = console.RecipeRunIn(directive="aads login verification")
    result = asyncio.run(console.run_console_recipe(body, context=ctx()))

    assert result["status"] == "approval_required"
    assert result["artifact"] == {
        "kind": "approval_wait",
        "narration": "승인이 필요한 단계에서 안전하게 대기 중입니다.",
        "evidence": {"approval_id": "approval-42", "run_id": RUN_ID},
    }


def test_command_marks_the_row_error_and_502s_when_the_trigger_blows_up(monkeypatch):
    """트리거가 실패했는데 행이 running 으로 남으면 그것이 이번 결함이다."""
    _command_pool(monkeypatch, [SESSION_MINE])
    closed: dict = {}

    async def fake_create(*, session_id, title, task_type):
        return "task-99"

    async def boom(session_id, system_message, ohvis_task_id=None, **kwargs):
        raise RuntimeError("no running event loop")

    async def fake_complete(task_id, *, status=None, result=None, ohvis_judgement=None):
        closed.update(task_id=task_id, status=status, result=result)
        return True

    from app.services import chat_service

    monkeypatch.setattr(console, "create_ohvis_task", fake_create)
    monkeypatch.setattr(chat_service, "trigger_ai_reaction", boom)
    monkeypatch.setattr(console, "complete_ohvis_task", fake_complete)

    body = console.ConsoleCommandIn(title="헬스체크")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(console.run_console_command(body, context=ctx()))

    assert caught.value.status_code == 502
    assert "ai_reaction_dispatch_failed" in caught.value.detail
    assert closed["task_id"] == "task-99"
    assert closed["status"] == "error"
    assert "no running event loop" in closed["result"]["error"]


def test_command_rejects_blank_and_oversized_titles(monkeypatch):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        console.ConsoleCommandIn(title="")
    with pytest.raises(ValidationError):
        console.ConsoleCommandIn(title="가" * 501)

    _command_pool(monkeypatch, [SESSION_MINE])

    async def unreachable(**kwargs):
        raise AssertionError("공백만 있는 지시로 행을 만들면 안 된다")

    monkeypatch.setattr(console, "create_ohvis_task", unreachable)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(console.run_console_command(console.ConsoleCommandIn(title="   "), context=ctx()))
    assert caught.value.status_code == 422


def test_ohvis_task_http_writes_stay_ungated_and_uncalled_by_the_dashboard():
    """`POST /ohvis/tasks`·`PATCH /ohvis/tasks/{id}` 는 **아직 무인증이다**.

    이 테스트는 "닫혔다" 가 아니라 **"열려 있다"** 를 고정한다. 게이트를 붙이려면
    두 공개 함수의 시그니처를 바꿔야 하는데, 그건 이 작업(콘솔 지시 실행 결선)의
    범위 밖이라 손대지 않았다. 대신 지금 실제로 성립하는 완화책 하나를 못 박는다:
    **대시보드에는 그 경로를 부르는 코드가 없다.** 지시는 관리자 게이트가 붙은
    `POST /ohvis/console/command` 로만 나간다.

    그러니 이 테스트가 깨지는 방향은 둘이고 뜻이 서로 다르다.
      - `_admin` 이 생겨서 깨졌다 → 게이트가 붙었다는 뜻이다. 이 테스트를
        "게이트가 있다" 쪽으로 뒤집어라.
      - 화면에 `createOhvisTask(` 호출이 되살아나서 깨졌다 → 무인증 경로로
        지시가 다시 새는 것이다. 화면을 되돌려라.
    """
    import inspect

    from app.api import ohvis_tasks

    for endpoint in (ohvis_tasks.create_task, ohvis_tasks.update_task):
        assert "_admin" not in inspect.signature(endpoint).parameters, (
            f"{endpoint.__name__} 에 인증 게이트가 붙었다 — 이 테스트를 뒤집어라"
        )

    # 경로 문자열(`/ohvis/tasks`)은 api.ts 안에만 있다. 화면이 그 경로에 닿는
    # 유일한 손잡이가 `createOhvisTask` 이므로, 호출이 없는지만 보면 된다
    # — 주석에 경로 이름이 나오는 것까지 막으면 거짓 양성이 난다.
    page_source = DASHBOARD_PAGE.read_text()
    assert "createOhvisTask(" not in page_source
    assert "api.createOhvisTask" not in page_source


# ────────────────────────────────────────────── 5. 대시보드 화면 정적 검사


def test_ohvis_route_exists_and_uses_the_single_summary_endpoint():
    source = DASHBOARD_PAGE.read_text()
    api_source = DASHBOARD_API.read_text()

    assert "getOhvisConsoleSummary" in source
    assert "getOhvisConsoleSummary" in api_source
    assert "/ohvis/console/summary" in api_source
    # 지시 전송은 실행까지 거는 콘솔 라우트로만 나간다. 기록만 하던
    # POST /ohvis/tasks 로 돌아가면 지시가 다시 pending 으로 굳는다.
    assert "runOhvisConsoleCommand" in source
    assert "runOhvisConsoleCommand" in api_source
    assert '"/ohvis/console/command"' in api_source
    # 화면(page.tsx)에서는 기록 전용 경로가 사라져야 한다 — 이게 결함의 본체였다.
    assert "createOhvisTask" not in source
    # 하지만 api.ts 의 **공개 심볼은 지운다**가 아니라 **표시한다**. 지우면
    # 리포지터리 밖 호출자가 조용히 깨지고, 같은 이름이 나중에 다른 뜻으로
    # 되살아난다. 선언은 그대로 두고 위에 @deprecated 만 붙인다.
    assert "createOhvisTask" in api_source
    marker = api_source.index("createOhvisTask:")
    preamble = api_source[max(0, marker - 800) : marker]
    assert "@deprecated" in preamble, "createOhvisTask 선언 위에 @deprecated 표시가 없다"
    assert "runOhvisConsoleCommand" in preamble, "@deprecated 는 대체 경로를 가리켜야 한다"
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


# ────────────────────────── 6. 트리거 회신 수집 (AADS-OHVIS-CONSOLE-SESSION-ROUTING-P0)
#
# "세션의 마지막 assistant 메시지" 를 그대로 답으로 채택하면, 그 사이 러너
# 알림 같은 다른 자동 메시지가 끼어들었을 때 그 알림 문구가 답변으로 표시된다.
# 이 절은 두 가지만 본다: (c) ⏳/⚠️ 안내문을 답으로 채택하지 않는다,
# (d) 채택할 답이 없으면 done 대신 error 로 닫는다.


def test_progress_and_interrupt_placeholders_are_never_adopted_as_the_reply():
    """순수 필터 단위 테스트 — 알려진 진행/중단 안내문은 전부 걸러진다."""
    from app.services import chat_service

    for placeholder in (
        "⏳ _AI가 응답을 생성 중입니다... (도구 2회 호출 중)_",
        "⚠️ 응답이 중단되었습니다. 다시 시도해 주세요.",
        "⚠️ 응답 생성에 실패했습니다. 동일한 지시를 다시 보내주세요.",
        "⚠️ 장시간 응답이 중단되었습니다. 동일한 지시를 다시 보내주세요.",
        chat_service._INTERRUPT_MARKER_GENERIC,
        chat_service._RESUME_FAIL_SUFFIX,
        "",
    ):
        assert chat_service._is_progress_or_interrupt_only(placeholder) is True, placeholder

    assert chat_service._is_progress_or_interrupt_only("서버 헬스체크 결과: 정상입니다.") is False


class _FakeChatConn:
    """`trigger_ai_reaction` 내부 DB 왕복을 대본대로 돌려주는 커넥션."""

    def __init__(self, values: list):
        self._values = list(values)
        self.queries: list[tuple[str, tuple]] = []

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        return self._values.pop(0) if self._values else None

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return "OK"


class _FakeChatPool:
    def __init__(self, conn: _FakeChatConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def _empty_stream(**kwargs):
    """`send_message_stream` 대역 — 아무 것도 만들지 않는 async generator."""
    return
    yield  # pragma: no cover


def _patch_trigger_ai_reaction_plumbing(monkeypatch, chat_service, conn: _FakeChatConn):
    """게이트(활성 슬롯·라이브 실행 여부)를 통과시키고 DB 왕복을 대본 커넥션으로 돌린다."""
    monkeypatch.setattr(chat_service, "get_pool", lambda: _FakeChatPool(conn))
    monkeypatch.setattr(chat_service, "_is_local_active_api_slot", lambda: True)

    async def _no_live_execution(_sid):
        return False

    monkeypatch.setattr(chat_service, "_session_has_live_execution", _no_live_execution)
    monkeypatch.setattr(chat_service, "send_message_stream", _empty_stream)


def _run_trigger_and_wait(chat_service, session_id: str, message: str, task_id: str) -> None:
    async def _drive():
        task = await chat_service.trigger_ai_reaction(session_id, message, task_id)
        assert task is not None, "트리거가 게이트에 막혀 태스크를 못 띄웠다"
        await task

    asyncio.run(_drive())


def test_trigger_reply_collection_rejects_a_stray_placeholder_and_closes_as_error(monkeypatch):
    """(c) 세션이 바쁠 때 끼어든 진행/중단 안내문을 답으로 채택하지 않는다."""
    import uuid as _uuid

    from app.services import chat_service

    session_id = str(_uuid.uuid4())
    # 순서: 중단-재개 검사(없음) → 트리거 시작 시각 → 마지막 assistant 메시지
    conn = _FakeChatConn([
        None,
        datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc),
        "⚠️ 응답이 중단되었습니다. 다시 시도해 주세요.",
    ])
    completed: dict = {}

    async def fake_complete(task_id, status="done", result=None, ohvis_judgement=None):
        completed.update(task_id=task_id, status=status, result=result)
        return True

    _patch_trigger_ai_reaction_plumbing(monkeypatch, chat_service, conn)
    monkeypatch.setattr("app.services.ohvis_task_manager.complete_task", fake_complete)

    _run_trigger_and_wait(chat_service, session_id, "[오비스 창 지시] 안녕", "task-c1")

    assert completed["status"] == "error"
    assert completed["result"] is None


def test_trigger_reply_collection_closes_as_error_when_nothing_new_was_found(monkeypatch):
    """(d) 이번 트리거로 만든 답이 없으면 done 대신 error 로 닫는다."""
    import uuid as _uuid

    from app.services import chat_service

    session_id = str(_uuid.uuid4())
    conn = _FakeChatConn([None, datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc), None])
    completed: dict = {}

    async def fake_complete(task_id, status="done", result=None, ohvis_judgement=None):
        completed.update(task_id=task_id, status=status, result=result)
        return True

    _patch_trigger_ai_reaction_plumbing(monkeypatch, chat_service, conn)
    monkeypatch.setattr("app.services.ohvis_task_manager.complete_task", fake_complete)

    _run_trigger_and_wait(chat_service, session_id, "[오비스 창 지시] 안녕", "task-d1")

    assert completed["status"] == "error"
    assert completed["result"] is None


def test_trigger_reply_collection_accepts_a_real_new_answer(monkeypatch):
    """회귀 방지 — 진짜 새 답은 여전히 done 으로 채택된다."""
    import uuid as _uuid

    from app.services import chat_service

    session_id = str(_uuid.uuid4())
    conn = _FakeChatConn([
        None,
        datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc),
        "서버 헬스체크 결과: 정상입니다.",
    ])
    completed: dict = {}

    async def fake_complete(task_id, status="done", result=None, ohvis_judgement=None):
        completed.update(task_id=task_id, status=status, result=result)
        return True

    _patch_trigger_ai_reaction_plumbing(monkeypatch, chat_service, conn)
    monkeypatch.setattr("app.services.ohvis_task_manager.complete_task", fake_complete)

    _run_trigger_and_wait(chat_service, session_id, "[오비스 창 지시] 안녕", "task-e1")

    assert completed["status"] == "done"
    assert completed["result"] == {"ai_reaction": "서버 헬스체크 결과: 정상입니다."}
