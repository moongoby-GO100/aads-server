"""AADS-AAG-DEBT-001 — 대시보드용 /ops 조회 엔드포인트의 순수 함수 검증.

DB 없이 도는 것만 본다: 파라미터 클램프, 행 직렬화, 상태 payload 조립.
이 6개 경로는 2026-09-16 AAG 스캔에서 ROUTE_MISSING(P0) 으로 잡혔던 것이고,
호출부가 `Promise.allSettled` + `res.ok` 라 응답 모양이 어긋나도 예외가 나지 않고
화면만 조용히 빈다 — 계약을 테스트로 고정해 둔다.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.api.ops import (
    _clamp_int,
    _coerce_json,
    _cost_trend_items,
    _design_review_items,
    _iso_or_none,
    _ops_status_payload,
    _pipeline_history_items,
    _pipeline_title,
    _project_stat_items,
    _qa_detail,
    _qa_result_items,
    _screenshot_url,
    _truncate,
    _worst_circuit,
)

KST = timezone(timedelta(hours=9))


# ─── 파라미터 클램프 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        (7, 7),
        (0, 1),          # 하한
        (-5, 1),
        (9999, 90),      # 상한
        (None, 7),       # 기본값
        ("", 7),
        ("abc", 7),
        ("30", 30),      # 숫자 문자열은 받는다
        (12.9, 12),      # float 는 버린다
    ],
)
def test_clamp_int(value, expected):
    assert _clamp_int(value, default=7, minimum=1, maximum=90) == expected


def test_clamp_int_never_raises_on_garbage():
    """422 대신 잘라낸다 — 호출부가 res.ok 만 보고 또 조용히 비는 것을 막는다."""
    assert _clamp_int([1, 2], default=10, minimum=1, maximum=100) == 10
    assert _clamp_int({"a": 1}, default=10, minimum=1, maximum=100) == 10


# ─── 공용 직렬화 헬퍼 ───────────────────────────────────────────────────────

def test_iso_or_none():
    assert _iso_or_none(None) is None
    assert _iso_or_none("2026-09-16T00:00:00+09:00") == "2026-09-16T00:00:00+09:00"
    aware = datetime(2026, 9, 16, 6, 0, 0, tzinfo=timezone.utc)
    assert _iso_or_none(aware) == "2026-09-16T15:00:00+09:00"


def test_coerce_json_accepts_str_and_obj():
    assert _coerce_json({"a": 1}) == {"a": 1}
    assert _coerce_json([1, 2]) == [1, 2]
    assert _coerce_json('{"a": 1}') == {"a": 1}
    assert _coerce_json("not json") is None
    assert _coerce_json("") is None
    assert _coerce_json(None) is None


def test_truncate_collapses_whitespace_and_marks_cut():
    assert _truncate("  a   b \n c ", 50) == "a b c"
    out = _truncate("x" * 100, 10)
    assert len(out) == 10 and out.endswith("…")


# ─── cost-trend ─────────────────────────────────────────────────────────────

def test_cost_trend_fills_missing_days():
    """비용이 0 인 날을 빼면 LineChart 의 x 축이 조용히 압축돼 추이가 왜곡된다."""
    today = date(2026, 9, 16)
    rows = [
        {"day": date(2026, 9, 14), "cost": 1.5, "records": 3},
        {"day": date(2026, 9, 16), "cost": 2.25, "records": 7},
    ]
    items = _cost_trend_items(rows, days=3, today=today)
    assert [i["date"] for i in items] == ["2026-09-14", "2026-09-15", "2026-09-16"]
    assert [i["cost"] for i in items] == [1.5, 0.0, 2.25]
    assert [i["records"] for i in items] == [3, 0, 7]


def test_cost_trend_empty_still_returns_days_points():
    items = _cost_trend_items([], days=7, today=date(2026, 9, 16))
    assert len(items) == 7
    assert all(i["cost"] == 0.0 for i in items)
    assert items[0]["date"] == "2026-09-10"
    assert items[-1]["date"] == "2026-09-16"


def test_cost_trend_accepts_str_day_and_none_cost():
    items = _cost_trend_items(
        [{"day": "2026-09-16", "cost": None, "records": None}],
        days=1,
        today=date(2026, 9, 16),
    )
    assert items == [{"date": "2026-09-16", "cost": 0.0, "records": 0}]


# ─── project-stats ──────────────────────────────────────────────────────────

def test_project_stat_items_buckets_statuses():
    rows = [
        {"project": "AADS", "status": "done", "cnt": 3},
        {"project": "AADS", "status": "rejected_done", "cnt": 62},
        {"project": "AADS", "status": "error", "cnt": 1},
        {"project": "AADS", "status": "cancelled", "cnt": 4},
        {"project": "AADS", "status": "running", "cnt": 1},
        {"project": "GO100", "status": "done", "cnt": 2},
    ]
    items = _project_stat_items(rows)
    aads = next(i for i in items if i["project"] == "AADS")
    assert aads["total"] == 71
    assert aads["completed"] == 3
    assert aads["failed"] == 63      # error + rejected_done
    assert aads["cancelled"] == 4
    assert aads["active"] == 1
    assert aads["by_status"]["rejected_done"] == 62
    # 건수 많은 프로젝트가 앞
    assert [i["project"] for i in items] == ["AADS", "GO100"]


def test_project_stat_items_handles_null_project_and_status():
    items = _project_stat_items([{"project": None, "status": None, "cnt": 2}])
    assert items[0]["project"] == "UNKNOWN"
    assert items[0]["by_status"] == {"unknown": 2}
    assert items[0]["active"] == 2


def test_project_stat_items_empty():
    assert _project_stat_items([]) == []


# ─── pipeline-history ───────────────────────────────────────────────────────

def test_pipeline_title_single_line_header():
    """현재 큐 행은 머리말이 한 줄이다 — 줄머리 매칭만으로는 통째로 놓친다."""
    got = _pipeline_title(
        "TASK_ID: AADS-AAG-DEBT-001 TITLE: AAG ROUTE_MISSING 상환 1차 "
        "PRIORITY: P1-HIGH SIZE: M"
    )
    assert got == "AAG ROUTE_MISSING 상환 1차"


def test_pipeline_title_multiline_header():
    got = _pipeline_title(
        "TASK_ID: GO100-FLOOR-QUALITY-DEPLOY\nTITLE: floor 교정 + 품질 게이트\n\n본문"
    )
    assert got == "floor 교정 + 품질 게이트"


def test_pipeline_title_falls_back_to_first_line():
    assert _pipeline_title("# 그냥 지시서\n\n본문") == "그냥 지시서"
    assert _pipeline_title(None) == ""
    assert _pipeline_title("   \n  \n") == ""


def test_pipeline_title_truncates():
    got = _pipeline_title("TITLE: " + "가" * 300, max_len=20)
    assert len(got) == 20 and got.endswith("…")


def test_pipeline_history_items_shape():
    rows = [{
        "job_id": "runner-24de409a",
        "project": "AADS",
        "status": "running",
        "phase": "claude_code_work",
        "instruction": "TASK_ID: X TITLE: 제목 PRIORITY: P1",
        "created_at": datetime(2026, 9, 16, 15, 32, tzinfo=KST),
        "completed_at": None,
        "source": "live",
    }]
    item = _pipeline_history_items(rows)[0]
    # 호출부(ArtifactDashboard) 가 읽는 키
    assert item["task_id"] == "runner-24de409a"
    assert item["title"] == "제목"
    assert item["status"] == "running"
    assert item["project"] == "AADS"
    assert item["completed_at"] is None
    assert item["created_at"].startswith("2026-09-16T15:32")
    assert item["source"] == "live"


def test_pipeline_history_items_defaults():
    item = _pipeline_history_items([{"job_id": "j1"}])[0]
    assert item["status"] == "unknown"
    assert item["project"] == "UNKNOWN"
    assert item["source"] == "live"
    assert item["title"] == ""


# ─── qa-results ─────────────────────────────────────────────────────────────

def test_qa_result_items_normalizes_verdict():
    """code_reviews 는 APPROVE/FLAG/REQUEST_CHANGES 를 쓰고 화면은 PASS 만 통과로 본다."""
    rows = [
        {"job_id": "a", "project": "AADS", "verdict": "APPROVE", "score": 0.93,
         "review_cycle": 1, "needs_retry": False, "flag_category": None,
         "feedback": {"score": 0.93, "issues": []},
         "created_at": datetime(2026, 9, 16, 15, 33, tzinfo=KST)},
        {"job_id": "b", "project": "GO100", "verdict": "FLAG", "score": 0.3,
         "review_cycle": 2, "needs_retry": True, "flag_category": "logic",
         "feedback": '{"issues": ["첫 번째 지적", "두 번째"]}',
         "created_at": datetime(2026, 9, 16, 15, 32, tzinfo=KST)},
    ]
    a, b = _qa_result_items(rows)
    assert a["verdict"] == "PASS" and a["raw_verdict"] == "APPROVE"
    assert a["retry_count"] == 0 and a["score"] == 0.93
    assert b["verdict"] == "FAIL" and b["raw_verdict"] == "FLAG"
    assert b["retry_count"] == 1          # review_cycle 2 = 재시도 1회
    assert b["detail"] == "첫 번째 지적"
    assert b["needs_retry"] is True


def test_qa_result_items_missing_fields():
    item = _qa_result_items([{"job_id": "x"}])[0]
    assert item["verdict"] == "FAIL"       # 판정 불명은 통과로 치지 않는다
    assert item["raw_verdict"] is None
    assert item["project"] == "UNKNOWN"
    assert item["retry_count"] == 0
    assert item["score"] is None
    assert item["detail"] == ""


def test_qa_detail_prefers_issue_then_summary_then_flag():
    assert _qa_detail({"issues": ["i1"], "summary": "s"}, "cat") == "i1"
    assert _qa_detail({"summary": "요약"}, "cat") == "요약"
    assert _qa_detail({}, "cat") == "cat"
    assert _qa_detail(None, None) == ""
    assert _qa_detail(["리스트 첫 항목"], None) == "리스트 첫 항목"


def test_qa_detail_truncates():
    out = _qa_detail({"issues": ["가" * 500]}, None, max_len=30)
    assert len(out) == 30 and out.endswith("…")


# ─── design-reviews ─────────────────────────────────────────────────────────

def test_screenshot_url_only_for_browser_reachable_paths():
    assert _screenshot_url("https://x/y.png") == "https://x/y.png"
    assert _screenshot_url("/static/shots/a.png") == "/static/shots/a.png"
    # 서버 파일시스템 경로는 <img src> 를 깨뜨린다 — 내보내지 않는다
    assert _screenshot_url("/root/aads/shots/a.png") is None
    assert _screenshot_url("") is None
    assert _screenshot_url(None) is None


def test_design_review_items_shape():
    rows = [{
        "id": 1, "task_id": "T-1", "project": "AADS", "verdict": "pass",
        "page_url": "https://x/p", "before_path": "/root/b.png",
        "after_path": "/static/a.png", "reviewer_model": "claude",
        "issues_json": ["대비 부족"], "scores_json": {"a11y": 80},
        "cost_usd": Decimal("0.0125"),
        "created_at": datetime(2026, 9, 16, 12, 0, tzinfo=KST),
    }]
    item = _design_review_items(rows)[0]
    assert item["verdict"] == "PASS"            # 화면이 대문자 PASS 로 비교한다
    assert item["screenshot_url"] == "/static/a.png"
    assert item["detail"] == "대비 부족"
    assert item["scores"] == {"a11y": 80}
    # Decimal 은 JSON 직렬화가 안 된다 — float 으로 바꿔 내보낸다
    assert item["cost_usd"] == 0.0125 and isinstance(item["cost_usd"], float)


def test_design_review_items_omits_unreachable_screenshot_key():
    """키 자체를 빼야 한다 — 호출부가 truthy 검사로 <img> 를 그린다."""
    item = _design_review_items([{"task_id": "T", "after_path": "/root/a.png"}])[0]
    assert "screenshot_url" not in item
    assert item["verdict"] == "PENDING"
    assert item["project"] == "UNKNOWN"
    assert item["issues"] == [] and item["scores"] == {}
    assert item["cost_usd"] is None


# ─── status ─────────────────────────────────────────────────────────────────

SERVERS_META = [
    {"id": "contabo116", "host": "5.104.86.116", "display_name": "contabo116 (AADS 본체)",
     "type": "local", "legacy_ids": ["68"]},
    {"id": "contabo14", "host": "5.104.86.14", "display_name": "contabo14 (GO100/KIS)",
     "type": "ssh", "legacy_ids": ["211"]},
    {"id": "cafe24_114", "host": "114.207.244.86", "display_name": "cafe24_114 (SF/NTV2/NAS)",
     "type": "ssh", "legacy_ids": ["114"]},
]


def test_ops_status_payload_maps_health_and_circuit():
    health = {
        "pipeline_healthy": True, "stalled_count": 0, "active_count": 2,
        "running_count": 1, "completed_today": 5, "error_count": 0,
        "maintenance_active": False, "issues": [],
        "infra": {"memory_pct": 61.2, "disk_pct": 40.0, "load_1m": 1.1},
        "checked_at": "2026-09-16T15:40:00+09:00",
    }
    circuit = [
        {"server": "contabo14", "state": "open", "failure_count": 5},
        {"server": "cafe24_114", "state": "closed", "failure_count": 0},
    ]
    payload = _ops_status_payload(health, circuit, SERVERS_META, threshold=3)

    by_ip = {s["ip"]: s for s in payload["servers"]}
    assert by_ip["5.104.86.116"]["status"] == "ok"
    assert by_ip["5.104.86.116"]["mem"] == 61.2
    assert by_ip["5.104.86.14"]["status"] == "error"     # open
    assert by_ip["114.207.244.86"]["status"] == "ok"     # closed
    # 위젯은 서킷을 한 덩어리로 그린다 — 가장 나쁜 쪽이 대표
    assert payload["circuit_breaker"] == {
        "server": "contabo14", "state": "open", "fail_count": 5, "threshold": 3,
    }
    assert payload["checked_at"] == "2026-09-16T15:40:00+09:00"
    assert payload["completed_today"] == 5


def test_ops_status_payload_local_server_without_infra_omits_metric_keys():
    """호출부가 `srv.cpu !== undefined` 로 본다 — null 을 넣으면 'null%' 가 뜬다."""
    payload = _ops_status_payload(
        {"pipeline_healthy": False, "infra": {}}, [], SERVERS_META, threshold=3
    )
    local = payload["servers"][0]
    assert local["status"] == "warn"
    for key in ("cpu", "mem", "disk", "load_1m"):
        assert key not in local
    # 서킷 상태를 전혀 못 읽었을 때 원격 서버는 unknown
    assert payload["servers"][1]["status"] == "unknown"


def test_ops_status_payload_health_error_marks_local_error():
    payload = _ops_status_payload(
        {"error": "db down", "pipeline_healthy": False, "stalled_count": -1},
        None, SERVERS_META, threshold=3,
    )
    assert payload["servers"][0]["status"] == "error"
    assert payload["pipeline_healthy"] is False
    assert payload["stalled_count"] == -1
    assert payload["circuit_breaker"]["state"] == "closed"
    assert payload["checked_at"]


def test_ops_status_servers_accepts_legacy_circuit_server_ids():
    """서킷 테이블에 구 ID('211') 로 쌓인 행이 있어도 카드에 반영돼야 한다."""
    payload = _ops_status_payload(
        {"pipeline_healthy": True, "infra": {}},
        [{"server": "211", "state": "half_open", "failure_count": 2}],
        SERVERS_META, threshold=3,
    )
    assert payload["servers"][1]["status"] == "warn"
    assert payload["servers"][1]["fail_count"] == 2


def test_worst_circuit_ranks_open_over_half_open_over_closed():
    states = [
        {"server": "a", "state": "closed", "failure_count": 0},
        {"server": "b", "state": "half_open", "failure_count": 1},
        {"server": "c", "state": "open", "failure_count": 4},
    ]
    assert _worst_circuit(states, 3)["server"] == "c"
    assert _worst_circuit(states[:2], 3)["server"] == "b"
    assert _worst_circuit([], 3) == {
        "server": None, "state": "closed", "fail_count": 0, "threshold": 3,
    }


# ─── 라우트 등록 계약 ───────────────────────────────────────────────────────

def test_six_missing_routes_are_registered_once():
    """AAG ROUTE_MISSING 으로 잡혔던 6경로가 정확히 한 번씩 등록돼 있어야 한다.

    중복 등록은 DOUBLE_MOUNT 로, 누락은 ROUTE_MISSING 으로 되돌아온다.
    """
    from app.api.ops import router

    expected = {
        "/ops/cost-trend", "/ops/project-stats", "/ops/status",
        "/ops/pipeline-history", "/ops/qa-results", "/ops/design-reviews",
    }
    registered = [
        r.path for r in router.routes
        if getattr(r, "path", None) in expected and "GET" in getattr(r, "methods", set())
    ]
    assert sorted(registered) == sorted(expected)
    assert len(registered) == len(set(registered))
