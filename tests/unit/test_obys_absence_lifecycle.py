"""결근 복원은 행의 '현재 수명주기' 감사만 쓴다 — ID 재사용·연속 결근 PUT·직전 상태 없음·운영 2건·judgment 일치."""
from datetime import date, datetime, time
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import obys_workspaces as api
from tests.unit.test_obys_absence_server_judgment import A, DAY, MEMO, FakeDb, rec, run, sched
from tests.unit.test_obys_absence_server_judgment import _audit_to_db  # noqa: F401 (autouse fixture)

T1, T2, T3, T4 = (datetime(2026, 11, 6, h) for h in (10, 13, 14, 15))


def audit(rid, action, at, **details):
    return {"action": action, "resource_id": rid, "details": details, "created_at": at}


# 지적 1 — 표지 mark → restore(soft delete) → 같은 키 POST 로 같은 ID 부활 → PUT 결근 → restore
@pytest.mark.asyncio
async def test_id_reuse_old_marker_audit_does_not_delete_revived_real_record():
    db = FakeDb([], [sched(A)])
    db.clock = T1
    await run(db, A, "mark-absent")
    marker = db.live(A, DAY)[0]
    assert marker["created_at"] == T1
    await run(db, A, "restore-present")
    assert marker.get("deleted_at")
    # POST(ON CONFLICT … created_at=now(), deleted_at=NULL) 가 같은 ID 를 실제 기록으로 되살린다
    marker.update(deleted_at=None, created_at=T2, status="approved", memo="정상", worked_minutes=475, source="manual")
    db.audits.append(audit(marker["id"], "attendance.create", T2, status="approved", memo="정상"))
    # PUT 으로 결근 처리
    marker.update(status="rejected", memo=MEMO)
    db.audits.append(audit(marker["id"], "attendance.update", T3, status="rejected", memo=MEMO))
    db.clock = T4
    result = await run(db, A, "restore-present")
    assert not marker.get("deleted_at")
    assert (marker["status"], marker["memo"]) == ("approved", "정상")
    assert result["changes"] == [{"id": marker["id"], "kind": "real", "result": "restored", "status": "approved", "memo": "정상"}]


# 지적 2 — 승인 대기 + 원래 메모 → 결근 PUT 두 번 → 복원은 pending + 원래 메모
@pytest.mark.asyncio
async def test_consecutive_absence_puts_restore_pending_with_original_memo():
    row = rec("r1", A, "rejected", 480, MEMO)
    db = FakeDb([row], [sched(A)], [
        audit("r1", "attendance.create", datetime(2026, 11, 1, 10), status="pending", memo="지하철 지연 확인 중"),
        audit("r1", "attendance.update", datetime(2026, 11, 2), status="rejected", memo=MEMO),
        audit("r1", "attendance.update", datetime(2026, 11, 3), status="rejected", memo=MEMO)])
    await run(db, A, "restore-present")
    assert (row["status"], row["memo"]) == ("pending", "지하철 지연 확인 중") and not row.get("deleted_at")


# 지적 2 — 직전 상태 없음: backfill(status 없음) → 결근 PUT → 409, 행 불변
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["restore-present", "mark-present"])
async def test_no_previous_state_returns_409_and_row_unchanged(action):
    row = rec("r1", A, "rejected", 660, MEMO)
    before = dict(row)
    db = FakeDb([row], [sched(A)], [
        audit("r1", "attendance.backfill", datetime(2026, 10, 9), source="legacy"),
        audit("r1", "attendance.update", datetime(2026, 10, 10), status="rejected", memo=MEMO)])
    with pytest.raises(HTTPException) as exc:
        await run(db, A, action)
    assert exc.value.status_code == 409 and exc.value.detail == "직전 상태를 확인할 수 없습니다. 근태 기록 수정에서 처리하세요"
    assert row == before
    assert not [q for q in db.queries if q[0].startswith("UPDATE yeoljeong_attendance_records")]


# 운영 2건과 같은 감사 순서 (biz-junghwa, 21:00→09:00)
@pytest.mark.asyncio
async def test_production_row_b3658c5e94b5_backfill_then_update_goes_409():
    row = rec("att-b3658c5e94b5", A, "rejected", 660, MEMO, start="21:00")
    db = FakeDb([row], [sched(A)], [
        audit(row["id"], "attendance.backfill", datetime(2026, 10, 9, 22)),   # details.status 없음
        audit(row["id"], "attendance.update", datetime(2026, 10, 12), status="rejected", memo=MEMO)])
    with pytest.raises(HTTPException) as exc:
        await run(db, A, "restore-present")
    assert exc.value.status_code == 409 and (row["status"], row["memo"]) == ("rejected", MEMO)


@pytest.mark.asyncio
async def test_production_row_72ee660d4f6f_create_approved_then_update_restores():
    row = rec("att-72ee660d4f6f", A, "rejected", 630, MEMO, start="21:00")
    db = FakeDb([row], [sched(A)], [
        audit(row["id"], "attendance.create", datetime(2026, 11, 2, 22), status="approved", memo="출근(수기 확인)"),
        audit(row["id"], "attendance.update", datetime(2026, 11, 4), status="rejected", memo=MEMO)])
    result = await run(db, A, "restore-present")
    assert (row["status"], row["memo"]) == ("approved", "출근(수기 확인)") and not row.get("deleted_at")
    assert result["changes"][0]["result"] == "restored"


# 지적 3 — 같은 픽스처에서 /attendance/absence 응답 judgment == list_work_schedules judgment (퇴근 완료 PWA 기록 포함)
@pytest.mark.asyncio
async def test_absence_response_judgment_equals_list_judgment_for_completed_pwa_record(monkeypatch):
    pwa = rec("p1", A, "approved", 475, source="pwa", check_in=datetime(2026, 11, 5, 9, 0))
    pwa["check_out_at"] = datetime(2026, 11, 5, 18, 0)
    schedule = sched(A)
    db = FakeDb([pwa], [schedule])
    response = await run(db, A, "restore-present")

    connection = AsyncMock()
    connection.fetch.side_effect = [[{**schedule, "work_date": DAY, "start_time": time(9), "end_time": time(18)}], [dict(pwa)]]
    monkeypatch.setattr(api, "_schedule_business", AsyncMock(return_value={"name": "사업자"}))
    monkeypatch.setattr(api, "_require_route_read", lambda *args: None)
    monkeypatch.setattr(api.upload_svc, "_tenant", lambda user: "tenant")
    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(api, "_legacy_people_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(api.finance_svc, "separated_last_work_dates", lambda *a: {})
    listed = await api.list_work_schedules("biz", date_from=str(DAY), date_to=str(DAY), employee_email=None,
                                           offset=0, current_user={})
    assert response["judgment"] == listed["schedules"][0]["judgment"]
    assert "퇴근누락" not in response["judgment"]["labels"]
    # 두 곳이 같은 컬럼 상수를 쓴다
    assert api.ATTENDANCE_JUDGMENT_COLUMNS in api.ABSENCE_DAY_ROWS_SQL


# runner-9d0918fc 지적 — 서버 '출근으로 처리' → 기존 PUT 으로 결근 → 복원: 409 가 아니라 출근 상태로 돌아온다
@pytest.mark.asyncio
async def test_mark_present_then_absence_put_then_restore_returns_present():
    db = FakeDb([], [sched(A)])
    db.clock = T1
    await run(db, A, "mark-present")
    row = db.live(A, DAY)[0]
    present = [a for a in db.audits if a["action"] == "attendance.absence.present"]
    assert [a["resource_id"] for a in present] == [row["id"]]
    assert (present[0]["details"]["status"], present[0]["details"]["memo"]) == ("approved", "출근(수기 확인)")
    row.update(status="rejected", memo=MEMO)  # 기존 PUT /attendance/records/{id} 로 결근 처리
    db.audits.append(audit(row["id"], "attendance.update", T2, status="rejected", memo=MEMO))
    db.clock = T3
    result = await run(db, A, "restore-present")
    assert (row["status"], row["memo"]) == ("approved", "출근(수기 확인)") and not row.get("deleted_at")
    assert result["changes"] == [{"id": row["id"], "kind": "real", "result": "restored",
                                  "status": "approved", "memo": "출근(수기 확인)"}]


# 같은 경로 — 근무표 시각에 soft delete 행이 있으면 고유키 충돌(500) 대신 같은 ID 로 되살리고 새 수명주기로 본다
@pytest.mark.asyncio
async def test_mark_present_revives_soft_deleted_row_at_same_start_and_ignores_old_audits():
    old = rec("old", A, "rejected", 0, MEMO)
    old["deleted_at"] = datetime(2026, 11, 2)
    db = FakeDb([old], [sched(A)], [audit("old", "attendance.absence.mark", datetime(2026, 11, 1, 9), kind="absence_marker")])
    db.clock = T1
    await run(db, A, "mark-present")
    assert [r["id"] for r in db.records] == ["old"]  # 새 행을 넣지 않고 되살렸다
    assert not old.get("deleted_at") and old["created_at"] == T1 and (old["status"], old["memo"]) == ("approved", "출근(수기 확인)")
    old.update(status="rejected", memo=MEMO)
    db.audits.append(audit("old", "attendance.update", T2, status="rejected", memo=MEMO))
    db.clock = T3
    await run(db, A, "restore-present")
    # 되살리기 전의 표지 감사(T0)는 현재 수명주기 밖이라 표지로 보지 않는다 → 삭제 대신 출근으로 복원
    assert not old.get("deleted_at") and (old["status"], old["memo"]) == ("approved", "출근(수기 확인)")


# 같은 시각에 살아 있는 다른 기록(일반 반려)이 있으면 덮어쓰지 않고 409
@pytest.mark.asyncio
async def test_mark_present_live_row_at_same_start_returns_409_and_keeps_row():
    rejected = rec("rj", A, "rejected", 475, "사유 확인 필요")
    before = dict(rejected)
    db = FakeDb([rejected], [sched(A)])
    with pytest.raises(HTTPException) as exc:
        await run(db, A, "mark-present")
    assert exc.value.status_code == 409 and exc.value.detail == api.ABSENCE_PRESENT_SLOT_TAKEN_DETAIL
    assert rejected == before and [r["id"] for r in db.records] == ["rj"]


# runner-6cf44f55 지적 — 같은 시각에 지워진 PWA 기록(출근 타각만 있음)은 되살리지 않는다: 타각이 남아
# '퇴근누락'·급여 incomplete 가 되거나 위치 증거를 덮어쓰지 않게 409, 행은 그대로
@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [{"source": "pwa", "check_in": datetime(2026, 11, 5, 9, 2)},
                                      {"source": "manual", "check_in": datetime(2026, 11, 5, 9, 2)}])
async def test_mark_present_does_not_revive_deleted_row_with_clock_evidence(evidence):
    old = rec("pw", A, "approved", 0, start="09:00", source=evidence["source"], check_in=evidence["check_in"])
    old["deleted_at"] = datetime(2026, 11, 5, 12)
    before = dict(old)
    db = FakeDb([old], [sched(A)])
    with pytest.raises(HTTPException) as exc:
        await run(db, A, "mark-present")
    assert exc.value.status_code == 409 and exc.value.detail == api.ABSENCE_PRESENT_SLOT_TAKEN_DETAIL
    assert old == before and [r["id"] for r in db.records] == ["pw"]
    for guard in ("check_in_at IS NULL", "check_out_at IS NULL", "source IS DISTINCT FROM 'pwa'"):
        assert guard in api._ABSENCE_PRESENT_UPSERT_SQL


# runner-be4aa6f8 지적 P1 — 표지 → 기존 PUT 으로 실제 근무·approved → PUT 결근 → 복원: 지우지 않고 approved 로
@pytest.mark.asyncio
async def test_marker_edited_into_real_record_by_put_is_restored_not_deleted():
    db = FakeDb([], [sched(A)])
    db.clock = T1
    await run(db, A, "mark-absent")
    row = db.live(A, DAY)[0]
    row.update(status="approved", memo="실근무 확인", worked_minutes=470)
    db.audits.append(audit(row["id"], "attendance.update", T2, status="approved", memo="실근무 확인"))
    row.update(status="rejected", memo=MEMO)
    db.audits.append(audit(row["id"], "attendance.update", T3, status="rejected", memo=MEMO))
    db.clock = T4
    result = await run(db, A, "restore-present")
    assert not row.get("deleted_at") and (row["status"], row["memo"], row["worked_minutes"]) == ("approved", "실근무 확인", 470)
    assert result["changes"][0]["result"] == "restored"


def test_classify_marker_then_update_is_real_but_marker_alone_is_marker():
    from app.services import obys_attendance_default as dp
    mark = audit("x", "attendance.absence.mark", T1, kind="absence_marker")
    upd = audit("x", "attendance.update", T2, status="approved", memo="m")
    assert dp.classify_absence_row({}, [mark]) == dp.MARKER_KIND
    assert dp.classify_absence_row({}, [mark, upd]) == dp.REAL_KIND


# runner-be4aa6f8 지적 P2 — 지난 근무일에 일반 반려 기록만 있으면 '출근(기본)'이 아니라 '확인필요'(급여 초안과 같음)
@pytest.mark.asyncio
async def test_ordinary_rejected_record_blocks_default_present_in_absence_response():
    rejected = rec("rj", A, "rejected", 475, "사유 확인 필요")
    db = FakeDb([rejected], [sched(A)])
    result = await run(db, A, "restore-present")  # 복원할 결근 없음 — 판정만 돌려준다
    assert result["judgment"]["labels"] == ["확인필요"] and not result["judgment"].get("default_present")
