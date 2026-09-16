"""AADS-AAG-DEBT-002 — 카카오봇 신규 6경로의 순수 함수 검증.

DB 없이 도는 것만 본다: 파라미터 클램프, 행 직렬화, 통계 조립, 설정 병합.

이 여섯 경로는 2026-09-16 AAG 스캔에서 ROUTE_MISSING(P0) 으로 잡혔던 것이고,
호출부가 전부 `r.ok ? r.json() : null` 이라 응답 모양이 어긋나도 예외가 나지
않고 화면만 조용히 빈다 — 라우트가 생겼다는 것만으로는 고쳐지지 않는다.
그래서 여기서 고정하는 것은 "200 이 나온다" 가 아니라 **응답의 키와 모양**이다.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.api.kakao_bot import (
    _KAKAO_SETTINGS_DEFAULTS,
    _kakao_clamp_int,
    _kakao_days_until,
    _kakao_history_items,
    _kakao_history_stats,
    _kakao_iso,
    _kakao_json,
    _kakao_merge_settings,
    _kakao_month_sequence,
    _kakao_send_channel,
    _kakao_send_time,
    _kakao_settings_payload,
    _kakao_upcoming_count,
)

KST = timezone(timedelta(hours=9))


# ─── 파라미터 클램프 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        (50, 50),
        (0, 1),            # 하한
        (-10, 1),
        (99999, 500),      # 상한
        (None, 50),        # 기본값
        ("", 50),
        ("abc", 50),
        ("200", 200),      # 숫자 문자열은 받는다
        (12.9, 12),        # float 는 버린다
    ],
)
def test_clamp_int(value, expected):
    assert _kakao_clamp_int(value, default=50, minimum=1, maximum=500) == expected


def test_clamp_int_never_raises_on_garbage():
    """422 대신 잘라낸다 — 호출부가 res.ok 만 보고 또 조용히 비는 것을 막는다."""
    assert _kakao_clamp_int([1, 2], default=50, minimum=1, maximum=500) == 50
    assert _kakao_clamp_int({"a": 1}, default=50, minimum=1, maximum=500) == 50


# ─── 직렬화 헬퍼 ────────────────────────────────────────────────────────────

def test_iso_or_none():
    assert _kakao_iso(None) is None
    assert _kakao_iso("2026-09-16T00:00:00+09:00") == "2026-09-16T00:00:00+09:00"
    aware = datetime(2026, 9, 16, 6, 0, 0, tzinfo=timezone.utc)
    assert _kakao_iso(aware) == "2026-09-16T15:00:00+09:00"


def test_coerce_json_accepts_str_and_obj():
    assert _kakao_json({"a": 1}) == {"a": 1}
    assert _kakao_json('{"a": 1}') == {"a": 1}
    assert _kakao_json("not json") is None
    assert _kakao_json("") is None
    assert _kakao_json(None) is None


def test_send_channel_defaults_to_sms():
    """지금 자동 발송 경로는 알리고 SMS 하나뿐이다(kakaobot_scheduler.py)."""
    assert _kakao_send_channel(None) == "sms"
    assert _kakao_send_channel('{"result_code": 1}') == "sms"
    assert _kakao_send_channel({"channel": ""}) == "sms"
    assert _kakao_send_channel({"channel": "kakao"}) == "kakao"


# ─── GET /history ───────────────────────────────────────────────────────────

def test_history_items_shape_matches_frontend():
    rows = [{
        "id": 7,
        "message": "생일 축하드려요",
        "status": "sent",
        "sent_at": datetime(2026, 9, 16, 0, 0, tzinfo=KST),
        "scheduled_at": datetime(2026, 9, 15, 0, 0, tzinfo=KST),
        "send_result": '{"result_code": 1}',
        "contact_name": "홍길동",
        "category": "birthday",
    }]
    item = _kakao_history_items(rows)[0]
    assert set(item) == {
        "id", "contact_name", "message", "category", "sent_at", "status", "channel",
    }
    # HistoryItem.id 는 string 이고 React key 로 쓰인다.
    assert item["id"] == "7" and isinstance(item["id"], str)
    assert item["sent_at"] == "2026-09-16T00:00:00+09:00"
    assert item["channel"] == "sms"


def test_history_item_falls_back_to_scheduled_at():
    """발송 실패로 sent_at 이 비어도 목록에서 날짜 칸이 Invalid Date 가 되면 안 된다."""
    rows = [{
        "id": 9,
        "message": "x",
        "status": "failed",
        "sent_at": None,
        "scheduled_at": datetime(2026, 9, 14, 9, 0, tzinfo=KST),
        "send_result": None,
        "contact_name": "김철수",
        "category": None,
    }]
    item = _kakao_history_items(rows)[0]
    assert item["sent_at"] == "2026-09-14T09:00:00+09:00"
    assert item["category"] == "custom"


def test_history_items_empty():
    assert _kakao_history_items([]) == []


# ─── GET /history/stats ─────────────────────────────────────────────────────

def test_month_sequence_crosses_year_boundary():
    assert _kakao_month_sequence(date(2026, 2, 3), 4) == [
        "2025-11", "2025-12", "2026-01", "2026-02",
    ]


def test_history_stats_fills_missing_months():
    """발송 0 인 달을 빼면 막대 차트의 x 축이 조용히 압축돼 추이가 왜곡된다."""
    rows = [
        {"month": "2026-09", "category": "birthday", "cnt": 3},
        {"month": "2026-07", "category": "greeting", "cnt": 2},
        {"month": "2026-09", "category": "greeting", "cnt": 1},
    ]
    out = _kakao_history_stats(rows, date(2026, 9, 16), months=4)
    assert out["total_sent"] == 6
    assert out["this_month"] == 4          # 3 + 1
    assert out["by_month"] == [
        {"month": "2026-06", "count": 0},
        {"month": "2026-07", "count": 2},
        {"month": "2026-08", "count": 0},
        {"month": "2026-09", "count": 4},
    ]


def test_history_stats_category_sorted_by_count_desc():
    rows = [
        {"month": "2026-09", "category": "greeting", "cnt": 1},
        {"month": "2026-09", "category": "birthday", "cnt": 5},
        {"month": "2026-08", "category": "birthday", "cnt": 2},
    ]
    out = _kakao_history_stats(rows, date(2026, 9, 16), months=2)
    assert list(out["by_category"].items()) == [("birthday", 7), ("greeting", 1)]


def test_history_stats_empty_still_returns_axis():
    out = _kakao_history_stats([], date(2026, 9, 16), months=12)
    assert out["total_sent"] == 0 and out["this_month"] == 0
    assert out["by_category"] == {}
    assert len(out["by_month"]) == 12
    assert out["by_month"][-1] == {"month": "2026-09", "count": 0}


# ─── GET /stats (다가오는 기념일) ───────────────────────────────────────────

def test_days_until_rolls_over_to_next_year():
    today = date(2026, 9, 16)
    assert _kakao_days_until(date(1990, 9, 16), today) == 0
    assert _kakao_days_until(date(1990, 9, 20), today) == 4
    # 이미 지난 날은 내년으로 넘어간다 — 음수가 되면 안 된다.
    assert _kakao_days_until(date(1990, 9, 15), today) == 364


def test_days_until_leap_day_is_skipped_not_crashed():
    """2/29 는 평년에 존재하지 않는다. 예외로 500 을 내지 말고 세지 않는다."""
    assert _kakao_days_until(date(2024, 2, 29), date(2026, 3, 1)) is None
    assert _kakao_days_until("2026-09-16", date(2026, 9, 16)) is None


def test_upcoming_count_within_window():
    today = date(2026, 9, 16)
    dates = [
        date(1990, 9, 16),   # 오늘 (0일)
        date(1990, 10, 1),   # 15일
        date(1990, 11, 1),   # 46일 — 창 밖
        date(2024, 2, 29),   # 윤일 — 세지 않는다
    ]
    assert _kakao_upcoming_count(dates, today, within_days=30) == 2
    assert _kakao_upcoming_count([], today) == 0


# ─── GET/PUT /settings ──────────────────────────────────────────────────────

def test_settings_payload_without_row_is_frontend_defaults():
    """첫 방문은 행이 없다. 404 나 빈 dict 가 아니라 화면의 기본값과 같아야 한다."""
    assert _kakao_settings_payload(None) == _KAKAO_SETTINGS_DEFAULTS
    assert _kakao_settings_payload(None) is not _KAKAO_SETTINGS_DEFAULTS  # 방어적 복사


def test_settings_payload_drops_unknown_columns():
    row = {
        "user_id": "u1", "created_at": "x", "updated_at": "y",
        "auto_send_enabled": False, "default_tone": "formal", "send_channel": "both",
        "send_time": "07:30", "birthday_days_before": 3,
        "anniversary_days_before": 1, "greeting_frequency": "weekly",
        "marketing_enabled": True,
    }
    out = _kakao_settings_payload(row)
    assert set(out) == set(_KAKAO_SETTINGS_DEFAULTS)
    assert out["default_tone"] == "formal" and out["send_time"] == "07:30"
    assert out["birthday_days_before"] == 3 and out["marketing_enabled"] is True


@pytest.mark.parametrize(
    "field,bad,fallback",
    [
        ("default_tone", "sarcastic", "friendly"),
        ("send_channel", "telegram", "kakao"),
        ("greeting_frequency", "hourly", "monthly"),
        ("send_time", "25:00", "09:00"),
        ("send_time", "9:00", "09:00"),
        ("send_time", "아침", "09:00"),
    ],
)
def test_settings_out_of_range_falls_back_instead_of_422(field, bad, fallback):
    out = _kakao_settings_payload({**_KAKAO_SETTINGS_DEFAULTS, field: bad})
    assert out[field] == fallback


def test_send_time_accepts_boundaries():
    assert _kakao_send_time("00:00") == "00:00"
    assert _kakao_send_time("23:59") == "23:59"
    assert _kakao_send_time("24:00") == "09:00"


def test_settings_days_before_clamped():
    out = _kakao_settings_payload({**_KAKAO_SETTINGS_DEFAULTS, "birthday_days_before": 999})
    assert out["birthday_days_before"] == 30
    out = _kakao_settings_payload({**_KAKAO_SETTINGS_DEFAULTS, "anniversary_days_before": -4})
    assert out["anniversary_days_before"] == 0


def test_merge_settings_keeps_unsent_fields():
    """화면은 전체를 보내지만, 부분 수정이 와도 나머지가 기본값으로 밀리면 안 된다."""
    current = {**_KAKAO_SETTINGS_DEFAULTS, "default_tone": "formal", "send_time": "07:30"}
    merged = _kakao_merge_settings(current, {"marketing_enabled": True})
    assert merged["default_tone"] == "formal"
    assert merged["send_time"] == "07:30"
    assert merged["marketing_enabled"] is True


def test_merge_settings_ignores_none_and_unknown_keys():
    current = dict(_KAKAO_SETTINGS_DEFAULTS)
    merged = _kakao_merge_settings(
        current, {"default_tone": None, "send_channel": "sms", "is_admin": True},
    )
    assert merged["default_tone"] == "friendly"
    assert merged["send_channel"] == "sms"
    assert "is_admin" not in merged


def test_merge_settings_validates_incoming():
    merged = _kakao_merge_settings(dict(_KAKAO_SETTINGS_DEFAULTS), {"send_channel": "telegram"})
    assert merged["send_channel"] == "kakao"


# ─── 라우트 계약 ────────────────────────────────────────────────────────────

def test_six_missing_routes_are_registered():
    """AAG ROUTE_MISSING 6건이 실제로 마운트됐는지 — 경로/메서드까지 본다."""
    from app.api.kakao_bot import router

    mounted = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }
    for method, path in [
        ("GET", "/kakao-bot/history"),
        ("GET", "/kakao-bot/history/stats"),
        ("GET", "/kakao-bot/stats"),
        ("POST", "/kakao-bot/scheduled/{scheduled_id}/cancel"),
        ("GET", "/kakao-bot/settings"),
        ("PUT", "/kakao-bot/settings"),
    ]:
        assert (method, path) in mounted, f"{method} {path} 미등록"


def test_no_duplicate_method_path_in_router():
    """ROUTE_SHADOWED(P0) — 같은 METHOD+경로를 두 번 등록하면 뒤엣것이 조용히 죽는다."""
    from app.api.kakao_bot import router

    seen = []
    for route in router.routes:
        for method in getattr(route, "methods", set()):
            seen.append((method, route.path))
    dupes = {pair for pair in seen if seen.count(pair) > 1}
    assert not dupes, f"중복 등록: {sorted(dupes)}"
