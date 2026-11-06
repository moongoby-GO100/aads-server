"""근무표만 있는 근무일의 '출근(기본)' 판정. DB·네트워크 접근이 없는 순수 함수만 둔다.

근태 현황의 기본값은 출근이다. 근무표(kind=work)가 있고 시작시각이 지났는데 실제 기록이
없으면 '출근(기본)'으로 보고, 대표가 결근으로 바꾼 날(status=rejected, memo='결근(대표 확인)')만
결근이다. 지각·조퇴 판정과 실제 타각 기록, 금액 산식은 여기서 건드리지 않는다.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DEFAULT_PRESENT_LABEL = "출근(기본)"
DEFAULT_PRESENT_SOURCE = "schedule_default"
MANUAL_ABSENCE_MEMO = "결근(대표 확인)"


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _clock(value: Any) -> time:
    if isinstance(value, datetime):
        return (value.astimezone(KST) if value.tzinfo else value).time()
    if isinstance(value, time):
        return value
    text = str(value)
    if "T" in text or " " in text:
        return _clock(datetime.fromisoformat(text.replace("Z", "+00:00")))
    return time.fromisoformat(text)


def _minutes(value: Any) -> int:
    t = _clock(value)
    return t.hour * 60 + t.minute


def is_manual_absence(row: dict[str, Any] | None) -> bool:
    row = row or {}
    return str(row.get("status") or "").strip().lower() == "rejected" and row.get("memo") == MANUAL_ABSENCE_MEMO


def default_present_applies(schedule: dict[str, Any] | None, now: datetime | None = None,
                            last_work_date: Any = None) -> bool:
    """근무표 기준으로 '출근(기본)'을 줄 수 있는 날인가. 기록 유무는 blocks_default_present 가 본다."""
    if not schedule or schedule.get("kind") != "work":
        return False
    now = now or datetime.now(KST)
    now = now.astimezone(KST) if now.tzinfo else now.replace(tzinfo=KST)
    try:
        day = _day(schedule["work_date"])
        if last_work_date and day > _day(last_work_date):
            return False
        today = now.date()
        if day < today:
            return True
        if day > today:
            return False
        return _minutes(schedule["start_time"]) <= now.hour * 60 + now.minute
    except (KeyError, TypeError, ValueError):
        return False


def blocks_default_present(row: dict[str, Any] | None) -> bool:
    """실제 기록(출근 시각이 있는 기록) 또는 반려·결근 지정이 있으면 기본 출근을 만들지 않는다."""
    row = row or {}
    if str(row.get("status") or "").strip().lower() == "rejected":
        return True  # 수동 결근 + 일반 반려(확인필요 유지)
    return bool(row.get("start_at") or row.get("check_in_at"))


def default_present_minutes(schedule: dict[str, Any]) -> int:
    """근무표 시간에서 휴게시간을 뺀 분. 자정을 넘기면 익일 종료로 본다."""
    start, end = _minutes(schedule["start_time"]), _minutes(schedule["end_time"])
    if end <= start:
        end += 1440
    return max(0, end - start - int(schedule.get("break_minutes") or 0))


def default_present_record(schedule: dict[str, Any]) -> dict[str, Any]:
    day = _day(schedule["work_date"]).isoformat()
    return {
        "id": f"default:{day}",
        "work_date": day,
        "start_at": str(schedule["start_time"])[:5],
        "end_at": str(schedule["end_time"])[:5],
        "break_minutes": int(schedule.get("break_minutes") or 0),
        "worked_minutes": default_present_minutes(schedule),
        "hourly_wage": 0,
        "status": "approved",
        "source": DEFAULT_PRESENT_SOURCE,
        "default_present": True,
    }



# ── 수동 결근 행 판정 (서버 단일 지점) ──────────────────────────────────────
MARKER_KIND = "marker"  # 결근 표지만 있는 행 — 복원 시 soft delete
REAL_KIND = "real"      # 실제 기록을 결근으로 바꾼 행 — 복원 시 삭제하지 않고 되돌림
PRESENT_MEMO = "출근(수기 확인)"
# 직전 상태(status·memo)를 실어 나르는 감사. 서버의 '출근으로 처리'(attendance.absence.present)가
# 만든 기록도 여기 있어야 그 기록을 결근으로 바꾼 뒤 복원할 때 409 로 끊기지 않는다.
RESTORE_SOURCE_ACTIONS = ("attendance.create", "attendance.backfill", "attendance.update", "attendance.absence.present")


def _detail(audit: dict[str, Any] | None) -> dict[str, Any]:
    details = (audit or {}).get("details")
    if isinstance(details, str):
        try:
            import json
            details = json.loads(details)
        except ValueError:
            return {}
    return details if isinstance(details, dict) else {}


def classify_absence_row(row: dict[str, Any], lifecycle_audits: list[dict[str, Any]] | None = None) -> str:
    """수동 결근 행이 표지인지 실제 기록인지 판정한다. 이 함수 하나만 쓴다.

    lifecycle_audits 는 그 행의 **현재 수명주기**(audit.created_at >= row.created_at) 감사만이다.
    같은 ID 가 soft delete 후 실제 기록으로 되살아나면 옛 표지 감사는 수명주기 밖이다.
    표지 = 현재 수명주기 안에 attendance.absence.mark 감사(details.kind=absence_marker)가 있고, 그 뒤에
    일반 수정(attendance.update)이 없는 행. 표지를 기존 PUT 으로 실제 근무로 고쳤다면 그 뒤로는 실제 기록이다 —
    복원 때 지우지 않고 직전 상태로 되돌린다. 그 밖의 수동 결근 행도 전부 실제 기록이다(데이터 손실 금지).
    lifecycle_audits 는 시간순이다.
    """
    marker = False
    for audit in lifecycle_audits or []:
        action = audit.get("action")
        if action == "attendance.absence.mark" and _detail(audit).get("kind") == "absence_marker":
            marker = True
        elif action == "attendance.update":
            marker = False
    return MARKER_KIND if marker else REAL_KIND


PREVIOUS_UNKNOWN_DETAIL = "직전 상태를 확인할 수 없습니다. 근태 기록 수정에서 처리하세요"


def previous_values_before_absence(lifecycle_audits: list[dict[str, Any]]) -> dict[str, str] | None:
    """현재 수명주기 감사(시간순) 가운데 결근 상태가 아닌 마지막 항목의 status·memo.

    details.status 가 있고 결근(status=rejected·memo='결근(대표 확인)')이 아닌 항목만 후보다.
    없으면 None — 호출자는 추측(approved 기본값)하지 않고 409 로 멈춘다.
    """
    for audit in reversed(lifecycle_audits or []):
        if audit.get("action") not in RESTORE_SOURCE_ACTIONS:
            continue
        details = _detail(audit)
        status = str(details.get("status") or "").strip().lower()
        if status and not is_manual_absence(details):
            return {"status": status, "memo": str(details.get("memo") or "")}
    return None
