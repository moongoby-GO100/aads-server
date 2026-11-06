"""근태 현황 '출근(기본)': 근무표만 있는 근무일은 출근이고, 대표가 바꾼 날만 결근이다."""
from datetime import date, datetime, time
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.api import obys_workspaces as api
from app.services import obys_attendance_default as dp

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=KST)
SCHEDULE = {"kind": "work", "work_date": "2026-11-05", "start_time": "09:00", "end_time": "18:00", "break_minutes": 60}
ABSENCE = {"status": "rejected", "memo": "결근(대표 확인)", "start_at": "09:00", "end_at": "18:00", "break_minutes": 60, "id": "a1"}


@pytest.mark.parametrize("day,start,last,expected", [
    ("2026-11-05", "09:00", None, True),     # 지난날
    ("2026-11-06", "09:00", None, True),     # 당일, 시작시각 경과
    ("2026-11-06", "12:00", None, True),     # 정각 시작
    ("2026-11-06", "12:01", None, False),    # 당일, 시작 전
    ("2026-11-07", "09:00", None, False),    # 미래
    ("2026-11-05", "09:00", "2026-11-04", False),  # 퇴사 이후
    ("2026-11-05", "09:00", "2026-11-05", True),   # 마지막 근무일 당일 포함
])
def test_default_present_applies(day, start, last, expected):
    schedule = {**SCHEDULE, "work_date": day, "start_time": start}
    assert dp.default_present_applies(schedule, NOW, last) is expected


def test_only_work_schedules_apply():
    assert not dp.default_present_applies({**SCHEDULE, "kind": "off"}, NOW, None)
    assert not dp.default_present_applies(None, NOW, None)


@pytest.mark.parametrize("row,expected", [
    ({}, False), (None, False),
    ({"start_at": "09:00", "status": "approved"}, True),
    ({"start_at": "09:00", "status": "pending"}, True),
    ({"status": "rejected", "memo": "결근(대표 확인)"}, True),
    ({"status": "rejected", "memo": "일반 반려"}, True),
])
def test_blocks_default_present(row, expected):
    assert dp.blocks_default_present(row) is expected


def test_default_record_subtracts_break_and_marks_source():
    record = dp.default_present_record(SCHEDULE)
    assert record["worked_minutes"] == 480 and record["source"] == "schedule_default" and record["default_present"] is True
    assert dp.default_present_minutes({**SCHEDULE, "start_time": time(22), "end_time": time(6), "break_minutes": 60}) == 420


def test_judgment_default_manual_absence_and_restore():
    result = api.attendance_schedule_judgment(SCHEDULE, now=NOW)
    assert result["labels"] == ["출근(기본)"] and result["default_present"] is True
    assert api.attendance_schedule_judgment(SCHEDULE, ABSENCE, now=NOW)["labels"] == ["결근"]
    restored = {**ABSENCE, "status": "approved", "memo": "출근(수기 확인)"}
    assert api.attendance_schedule_judgment(SCHEDULE, restored, now=NOW)["labels"] == ["정상"]


def test_judgment_after_last_work_date_is_scheduled_and_real_record_unchanged():
    assert api.attendance_schedule_judgment(SCHEDULE, now=NOW, last_work_date="2026-11-01")["labels"] == ["예정"]
    late = {"start_at": "09:30", "end_at": "18:00", "break_minutes": 60, "status": "approved"}
    result = api.attendance_schedule_judgment(SCHEDULE, late, now=NOW)
    assert result["labels"] == ["지각"] and result["late_minutes"] == 30 and "default_present" not in result


@pytest.mark.asyncio
async def test_list_counts_default_present_and_absence_and_separation(monkeypatch):
    def sched(email, day):
        return {**SCHEDULE, "work_date": date.fromisoformat(day), "employee_email": email, "id": f"{email}-{day}"}
    rows = [sched("a@x.test", "2026-11-04"), sched("a@x.test", "2026-11-05"),
            sched("freelancer@x.test", "2026-11-04"), sched("gone@x.test", "2026-11-04"), sched("gone@x.test", "2026-11-05")]
    absence = {**ABSENCE, "employee_email": "a@x.test", "work_date": date(2026, 11, 5)}
    connection = AsyncMock()
    connection.fetch.side_effect = [rows, [absence]]
    monkeypatch.setattr(api, "_schedule_business", AsyncMock(return_value={"name": "사업자"}))
    monkeypatch.setattr(api, "_require_route_read", lambda *args: None)
    monkeypatch.setattr(api.upload_svc, "_tenant", lambda user: "tenant")
    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(api, "_legacy_people_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(api.finance_svc, "separated_last_work_dates", lambda *a: {"gone@x.test": "2026-11-04"})
    result = await api.list_work_schedules("b1", date_from="2026-11-04", date_to="2026-11-05",
                                           employee_email=None, offset=0, current_user={})
    by = {(s["employee_email"], s["work_date"]): s for s in result["schedules"]}
    assert by[("a@x.test", "2026-11-04")]["judgment"]["status"] == "출근(기본)"
    assert by[("a@x.test", "2026-11-04")]["default_present"] is True and by[("a@x.test", "2026-11-04")]["action_record"] is None
    assert by[("a@x.test", "2026-11-05")]["judgment"]["status"] == "결근"
    assert by[("a@x.test", "2026-11-05")]["action_record"]["id"] == "a1"
    assert by[("freelancer@x.test", "2026-11-04")]["judgment"]["status"] == "출근(기본)"
    assert by[("gone@x.test", "2026-11-05")]["judgment"]["status"] == "예정"
    assert result["counts"] == {"출근": 3, "출근(기본)": 3, "결근": 1, "예정": 1}


@pytest.mark.asyncio
async def test_list_fails_with_503_when_separation_info_unreadable(monkeypatch):
    connection = AsyncMock()
    connection.fetch.side_effect = [[{**SCHEDULE, "work_date": date(2026, 11, 5), "employee_email": "a@x.test"}], []]
    monkeypatch.setattr(api, "_schedule_business", AsyncMock(return_value={"name": "사업자"}))
    monkeypatch.setattr(api, "_require_route_read", lambda *args: None)
    monkeypatch.setattr(api.upload_svc, "_tenant", lambda user: "tenant")
    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(api, "_legacy_people_rows", AsyncMock(return_value=[]))

    def boom(*a):
        raise RuntimeError("down")
    monkeypatch.setattr(api.finance_svc, "separated_last_work_dates", boom)
    with pytest.raises(api.HTTPException) as exc:
        await api.list_work_schedules("b1", date_from="2026-11-05", date_to="2026-11-05",
                                      employee_email=None, offset=0, current_user={})
    assert exc.value.status_code == 503


def test_manual_absence_wins_over_normal_record_same_day():
    normal = {"id": "n1", "status": "approved", "start_at": "09:00", "end_at": "18:00",
              "check_in_at": "2026-11-05T09:00:00+09:00", "check_out_at": "2026-11-05T18:00:00+09:00"}
    schedule = {**SCHEDULE, "actual_records": [normal, ABSENCE]}
    assert api.attendance_schedule_judgment(schedule, now=NOW)["labels"] == ["결근"]
    # 복원 버튼이 붙을 대상은 결근 기록이다.
    assert api._schedule_action_record([normal, ABSENCE])["id"] == "a1"
    assert api._schedule_action_record([normal])["id"] == "n1"


def test_separated_last_work_dates_raises_when_db_read_fails(monkeypatch):
    from app.services import yeoljeong_finance_service as svc
    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_run_db", lambda coro: (coro.close(), None)[1])
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "00000000-0000-0000-0000-000000000001")
    with pytest.raises(RuntimeError):
        svc.separated_last_work_dates({}, "biz")


def test_absence_click_handler_has_no_client_side_record_judgment():
    """정책 변경: 옛 단언(openClock 으로 PUT 본문 시각 보정)은 버렸다. 이제 실제 기록은 건드리지 않고
    표지만 서버가 만들므로 화면에는 기록 선택·PUT/DELETE 분기가 없어야 한다."""
    html = open("app/static/apps/obys/admin.html", encoding="utf-8").read()
    handler = html[html.index('querySelectorAll("[data-absence-action]")'):]
    handler = handler[:handler.index("});", handler.index("finally{b.disabled=false}"))]
    assert "/attendance/absence" in handler
    for banned in ("openClock", "sib", "dropAbsence", "personal.", "recordKey", "/attendance/records", '"DELETE"', '"PUT"'):
        assert banned not in handler, banned
