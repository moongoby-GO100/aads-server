"""Execute production judgment functions without requiring API/runtime dependencies.

These are policy unit tests, not DB transaction or browser integration evidence.
"""
import ast
import math
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def policy():
    source = Path("app/api/obys_workspaces.py").read_text()
    tree = ast.parse(source)
    names = {"judge_geofence", "geofence_radius_m", "haversine_m",
             "_schedule_time", "_schedule_day_record", "attendance_schedule_judgment"}
    module = ast.Module(body=[node for node in tree.body
                             if isinstance(node, ast.FunctionDef) and node.name in names],
                        type_ignores=[])
    from app.services import obys_attendance_default
    namespace = dict(math=math, attendance_default=obys_attendance_default, date=date, datetime=datetime, time=time, Any=Any,
                     ZoneInfo=ZoneInfo, GEOFENCE_DEFAULT_RADIUS_M=50,
                     GEOFENCE_MAX_RADIUS_M=50, _EARTH_RADIUS_M=6_371_000)
    exec(compile(module, "app/api/obys_workspaces.py", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("consent,latitude,accuracy", [
    (True, 37.5, 5), (False, 37.5, 5), (True, None, None), (True, 37.5, 500),
])
def test_location_evidence_never_approves_attendance(policy, consent, latitude, accuracy):
    result = policy["judge_geofence"](
        consent=consent, latitude=latitude, longitude=127.0,
        accuracy_m=accuracy, branch={"latitude": 37.5, "longitude": 127.0})
    assert result["status"] == "pending"
    if not consent:
        assert result["latitude"] is None
    if consent and latitude and accuracy == 5:
        assert result["result"] == "inside"


@pytest.mark.parametrize("day,label", [(1, "출근(기본)"), (2, "출근(기본)"), (3, "예정")])
def test_schedule_without_record_is_never_absence(policy, day, label):
    # 2026-11-02 12:00 KST 기준: 지난날과 시작시각이 지난 당일은 기본 출근, 미래는 예정. 결근은 되지 않는다.
    schedule = {"kind": "work", "work_date": date(2026, 11, day), "start_time": "09:00", "end_time": "18:00"}
    result = policy["attendance_schedule_judgment"](
        schedule, now=datetime(2026, 11, 2, 12, tzinfo=ZoneInfo("Asia/Seoul")))
    assert result["labels"] == [label]
    assert "결근" not in result["labels"]


def test_schedule_today_before_start_is_scheduled(policy):
    schedule = {"kind": "work", "work_date": date(2026, 11, 2), "start_time": "13:00", "end_time": "18:00"}
    result = policy["attendance_schedule_judgment"](
        schedule, now=datetime(2026, 11, 2, 12, tzinfo=ZoneInfo("Asia/Seoul")))
    assert result["labels"] == ["예정"]


def test_open_clock_placeholder_does_not_imply_completed_work(policy):
    result = policy["attendance_schedule_judgment"](
        {"kind": "work", "work_date": date(2026, 11, 1)},
        {"start_at": "10:00", "end_at": "10:00", "status": "pending",
         "check_in_at": "2026-11-01T10:00:00+09:00", "check_out_at": None})
    assert result["labels"] == ["확인필요", "퇴근누락"]
    assert result["overtime_minutes"] == 0
