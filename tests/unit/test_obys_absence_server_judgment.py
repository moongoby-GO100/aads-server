"""결근·복원 서버 판정(POST /attendance/absence) — 실제 함수를 인메모리 DB 로 실행하고 SQL 파라미터를 검증한다."""
import asyncio
import itertools
from datetime import date, datetime, time

import pytest
from fastapi import HTTPException

from app.api import obys_workspaces as api
from app.services import obys_attendance_default as dp
from app.services import yeoljeong_finance_service as svc

DAY = date(2026, 11, 5)
A, B = "a@x.test", "b@x.test"
MEMO = "결근(대표 확인)"
T0 = datetime(2026, 11, 1, 9, 0)
T_NOW = datetime(2026, 11, 6, 12, 0)


class FakeDb:
    """absence 함수가 쓰는 SQL 상수만 해석한다. 조회는 (email, 날짜) 조건을 실제로 적용한다."""

    def __init__(self, records, schedules, audits=None, roster=()):
        self.records, self.schedules, self.audits = records, schedules, audits or []
        self.roster, self.locks, self.held = list(roster), {}, []
        self.queries, self.weekly = [], {}
        self.ids = itertools.count(100)
        self.clock = T_NOW  # DB now() — 테스트가 시간을 앞으로 돌린다

    def live(self, email, day):
        return [r for r in self.records if r["employee_email"].lower() == email and r["work_date"] == day and not r.get("deleted_at")]

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        await asyncio.sleep(0)  # 동시 요청이 끼어들 틈
        if sql == api.ABSENCE_DAY_ROWS_SQL:
            assert args[0] == "biz" and "lower(a.employee_email)=$2" in sql and "work_date=$3" in sql and "FOR UPDATE" in sql
            return sorted(self.live(args[1], args[2]), key=lambda r: str(r["start_at"]))
        if sql == api.ABSENCE_SLOTS_SQL:
            return [r for r in self.records if r["employee_email"].lower() == args[0] and r["work_date"] == args[1]]
        if sql == api.ABSENCE_AUDITS_SQL:
            # 실제 SQL 처럼 현재 수명주기(created_at >= 행 created_at)만 돌려준다
            return [a for a in self.audits if a["resource_id"] == args[1] and a.get("created_at", T0) >= args[2] and a["action"] in (
                *dp.RESTORE_SOURCE_ACTIONS, "attendance.absence.mark")]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        if sql == api.ABSENCE_EMPLOYEE_ROSTER_SQL:
            return {"employee_email": args[1], "employee_name": f"이름-{args[1]}", "branch": "본점"} if args[1] in self.roster else None
        if sql == api.ABSENCE_EMPLOYEE_SQL:
            rows = [s for s in self.schedules if s["employee_email"] == args[1]]
            return {"employee_email": args[1], "employee_name": f"이름-{args[1]}", "branch": "본점"} if rows else None
        if sql == api.ABSENCE_SCHEDULE_DAY_SQL:
            return next((s for s in self.schedules if s["employee_email"] == args[1] and s["work_date"] == args[2]), None)
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        if sql == api._ABSENCE_INSERT_SQL:
            (biz, email, _mask, name, branch, day, start, end, brk, worked, status, memo, actor) = args
            rid = f"n{next(self.ids)}"
            self.records.append({"id": rid, "employee_email": email, "employee_name": name, "branch": branch,
                                 "work_date": day, "start_at": start, "end_at": end, "break_minutes": brk,
                                 "worked_minutes": worked, "hourly_wage": 0, "status": status, "memo": memo,
                                 "source": "manual", "check_in_at": None, "created_at": self.clock})
            return rid
        if sql == api._ABSENCE_PRESENT_UPSERT_SQL:  # 고유키(이메일·날짜·시작시각)는 soft delete 행도 포함한다
            (biz, email, _mask, name, branch, day, start, end, brk, worked, status, memo, actor) = args
            same = next((r for r in self.records if r["employee_email"].lower() == email and r["work_date"] == day
                         and r["start_at"] == start), None)
            if same is not None and (not same.get("deleted_at") or same.get("check_in_at") or same.get("check_out_at")
                                     or same.get("source") == "pwa"):
                return None  # ON CONFLICT ... WHERE (지워졌고 타각 증거 없음) 이 거짓 -> RETURNING 없음
            if same is not None:  # soft delete 행을 같은 ID 로 되살린다 — created_at=now()
                same.update(deleted_at=None, created_at=self.clock, end_at=end, break_minutes=brk,
                            worked_minutes=worked, status=status, memo=memo, source="manual")
                return same["id"]
            rid = f"n{next(self.ids)}"
            self.records.append({"id": rid, "employee_email": email, "employee_name": name, "branch": branch,
                                 "work_date": day, "start_at": start, "end_at": end, "break_minutes": brk,
                                 "worked_minutes": worked, "hourly_wage": 0, "status": status, "memo": memo,
                                 "source": "manual", "check_in_at": None, "created_at": self.clock})
            return rid
        if sql == api._ATTENDANCE_WEEK_SUM_SQL:
            _biz, email, lo, hi = args
            return sum(r["worked_minutes"] for r in self.records if r["employee_email"] == email and lo <= r["work_date"] < hi
                       and not r.get("deleted_at") and r["status"] != "rejected")
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.queries.append((sql, args))
        if sql == api.ABSENCE_DAY_LOCK_SQL:  # 트랜잭션 끝(release_locks)까지 보유
            lock = self.locks.setdefault(args[0], asyncio.Lock())
            await lock.acquire()
            self.held.append(lock)
        elif sql.startswith("UPDATE yeoljeong_attendance_records SET deleted_at"):
            next(r for r in self.records if r["id"] == args[0]).update(deleted_at=datetime(2026, 11, 6))
        elif sql.startswith("UPDATE yeoljeong_attendance_records SET status"):
            next(r for r in self.records if r["id"] == args[0]).update(status=args[2], memo=args[3])
        elif sql.startswith("INSERT INTO yeoljeong_audit_logs"):
            self.audits.append({"action": args[2], "resource_id": args[3], "details": args[4], "created_at": self.clock})
        elif sql == api._ATTENDANCE_WEEK_UPSERT_SQL:
            self.weekly[(args[0], args[2])] = args[3]
        else:
            raise AssertionError(sql)


def rec(rid, email, status="approved", minutes=475, memo="", start="09:00", source="manual", check_in=None, day=DAY):
    return {"id": rid, "employee_email": email, "employee_name": email, "branch": "본점", "work_date": day,
            "start_at": time.fromisoformat(start), "end_at": time.fromisoformat("18:00"), "break_minutes": 60,
            "worked_minutes": minutes, "hourly_wage": 0, "status": status, "memo": memo, "source": source, "check_in_at": check_in, "created_at": T0}


def sched(email, day=DAY):
    return {"employee_email": email, "work_date": day, "kind": "work", "start_time": time(9), "end_time": time(18), "break_minutes": 60}


@pytest.fixture(autouse=True)
def _audit_to_db(monkeypatch):
    async def audit(connection, business_id, user, action, record_id, details):
        await connection.execute("INSERT INTO yeoljeong_audit_logs (x)", business_id, "actor", action, record_id, details)
    monkeypatch.setattr(api, "_attendance_audit", audit)
    monkeypatch.setattr(api.upload_svc, "_actor", lambda user: "actor")


async def run(db, email, action, day=DAY):
    try:
        return await api.apply_attendance_absence(db, "biz", {}, email, day, action, None)
    finally:  # 트랜잭션 종료 = 어드바이저리 락 해제
        while db.held:
            db.held.pop().release()


def sums(db, email):
    return sum(r["worked_minutes"] for r in db.live(email, DAY) if r["status"] != "rejected")


# 테스트 2 (지적 2) — A 정상 + B 결근 1건: B 복원은 B 이메일+그날로만 조회하고 A 기록은 그대로
@pytest.mark.asyncio
async def test_restore_b_queries_only_b_and_leaves_a_untouched():
    marker_b = rec("mb", B, "rejected", 0, MEMO)
    db = FakeDb([rec("ra", A), marker_b], [sched(A), sched(B)],
                [{"action": "attendance.absence.mark", "resource_id": "mb", "details": {"kind": "absence_marker"}}])
    result = await run(db, B, "restore-present")
    lock_queries = [q for q in db.queries if q[0] == api.ABSENCE_DAY_ROWS_SQL]
    assert lock_queries and all(args == ("biz", B, DAY) for _sql, args in lock_queries)
    a = next(r for r in db.records if r["id"] == "ra")
    assert a["status"] == "approved" and a["worked_minutes"] == 475 and not a.get("deleted_at")
    assert marker_b.get("deleted_at") and result["changes"][0]["result"] == "soft_deleted"
    assert db.weekly[(B, date(2026, 11, 2))] == 0 and db.weekly.get((A, date(2026, 11, 2))) is None


# 테스트 3 — 같은 직원 [정상 475분 + 표지] 복원: 표지 soft delete, 정상 기록 불변, 합계·급여 초안 475분
def test_restore_marker_keeps_normal_record_and_payroll_475(monkeypatch):  # 급여 초안이 asyncio.run 을 써서 동기 테스트
    db = FakeDb([rec("r1", A), rec("m1", A, "rejected", 0, MEMO, start="00:05")], [sched(A)],
                [{"action": "attendance.absence.mark", "resource_id": "m1", "details": {"kind": "absence_marker"}}])
    result = asyncio.run(run(db, A, "restore-present"))
    r1 = next(r for r in db.records if r["id"] == "r1")
    assert (r1["status"], r1["worked_minutes"], r1["memo"]) == ("approved", 475, "") and not r1.get("deleted_at")
    assert next(r for r in db.records if r["id"] == "m1").get("deleted_at")
    assert db.weekly[(A, date(2026, 11, 2))] == 475 and sums(db, A) == 475
    assert result["judgment"]["labels"] != ["결근"]
    live = [r for r in db.records if not r.get("deleted_at")]
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "t")
    monkeypatch.setattr(svc, "_is_admin", lambda user: True)
    monkeypatch.setattr(svc, "_find_employee_record", lambda *a, **k: {"id": "j", "email": A, "business_id": "biz"})
    monkeypatch.setattr(svc, "_require_business_for_tenant", lambda b, u: b)
    monkeypatch.setattr(svc, "_read_hr", lambda *a: [])
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {"employment": {"wage_type": "hourly", "wage": 12000}})
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *a: [{**r, "work_date": "2026-11-05"} for r in live])
    monkeypatch.setattr(svc, "_payroll_default_present_rows", lambda *a: [])
    monkeypatch.setattr(svc, "_save_payroll_record", lambda request, user, server_fields: ({**request, **server_fields}, True))
    monkeypatch.setattr(svc, "_audit_payroll", lambda *a: True)
    draft = svc.payroll_draft_from_attendance({"payroll_month": "2026-11", "employee_request_id": "j"}, {})
    assert draft["attendance_basis"]["approved_minutes"] == 475


# 테스트 4 — 실제 기록을 결근으로 바꾼 행 1건: 삭제하지 않고 approved 복원, 급여 초안에 반영
@pytest.mark.asyncio
async def test_restore_legacy_real_row_is_restored_not_deleted(monkeypatch):
    row = rec("r1", A, "rejected", 480, MEMO)
    audits = [{"action": "attendance.create", "resource_id": "r1", "details": {"status": "approved", "memo": "정상"}},
              {"action": "attendance.update", "resource_id": "r1", "details": {"status": "rejected", "memo": MEMO}}]
    db = FakeDb([row], [sched(A)], audits)
    result = await run(db, A, "restore-present")
    assert not row.get("deleted_at") and (row["status"], row["memo"]) == ("approved", "정상")
    assert result["changes"][0] == {"id": "r1", "kind": "real", "result": "restored", "status": "approved", "memo": "정상"}
    assert db.weekly[(A, date(2026, 11, 2))] == 480
    # 직전 상태를 못 찾으면 추측하지 않는다 — 409, 행 불변 (이전: approved/'출근(수기 확인)' 으로 대체했었다)
    row2 = rec("r2", A, "rejected", 480, MEMO)
    db2 = FakeDb([row2], [sched(A)], [])
    with pytest.raises(HTTPException) as exc:
        await run(db2, A, "restore-present")
    assert exc.value.status_code == 409 and "직전 상태를 확인할 수 없습니다" in exc.value.detail
    assert (row2["status"], row2["memo"]) == ("rejected", MEMO) and not row2.get("deleted_at")


# 테스트 5 — mark-absent 두 번: 표지 1건, 실제 기록 불변, 결근 집계·합계 제외
@pytest.mark.asyncio
async def test_mark_absent_twice_makes_one_marker_and_keeps_real_record():
    db = FakeDb([rec("r1", A)], [sched(A)])
    before = dict(db.records[0])
    first = await run(db, A, "mark-absent")
    second = await run(db, A, "mark-absent")
    markers = [r for r in db.live(A, DAY) if dp.is_manual_absence(r)]
    assert len(markers) == 1 and markers[0]["worked_minutes"] == 0
    assert db.records[0] == before
    assert first["judgment"]["labels"] == ["결근"] and second["judgment"]["labels"] == ["결근"]
    assert second["changes"] == []
    marks = [a for a in db.audits if a["action"] == "attendance.absence.mark"]
    assert len(marks) == 1 and marks[0]["details"]["kind"] == "absence_marker"
    assert db.weekly[(A, date(2026, 11, 2))] == 475  # 주간 합계는 표지(rejected) 제외, 실제 시간은 그대로


# 테스트 6 — 근무표만 있는 날 '출근(기본)', 결근 → 복원 왕복 후 다시 '출근(기본)'
@pytest.mark.asyncio
async def test_default_present_absence_restore_roundtrip():
    db = FakeDb([], [sched(A)])
    s = sched(A)
    assert api.attendance_schedule_judgment(s, now=datetime(2026, 11, 6, 12, tzinfo=dp.KST))["labels"] == ["출근(기본)"]
    absent = await run(db, A, "mark-absent")
    assert absent["judgment"]["labels"] == ["결근"] and absent["action_record"]["status"] == "rejected"
    restored = await run(db, A, "restore-present")
    assert restored["judgment"]["labels"] == ["출근(기본)"] and restored["action_record"] is None
    assert db.live(A, DAY) == []


@pytest.mark.asyncio
async def test_mark_present_without_records_and_unknown_employee():
    db = FakeDb([], [sched(A)])
    result = await run(db, A, "mark-present")
    row = db.live(A, DAY)[0]
    assert row["status"] == "approved" and row["worked_minutes"] == 480 and row["memo"] == "출근(수기 확인)"
    assert result["changes"][0]["result"] == "inserted"
    again = await run(db, A, "mark-present")
    assert again["changes"] == [] and len(db.live(A, DAY)) == 1
    with pytest.raises(HTTPException) as exc:
        await run(db, "nobody@x.test", "mark-absent")
    assert exc.value.status_code == 404


def test_request_model_forbids_extra_fields_and_unknown_actions():
    with pytest.raises(Exception):
        api.AttendanceAbsenceIn(employee_email=A, work_date="2026-11-05", action="mark-absent", record_id="x")
    with pytest.raises(Exception):
        api.AttendanceAbsenceIn(employee_email=A, work_date="2026-11-05", action="delete")


# 레거시 표지 판정(C) — 판정 함수 하나
@pytest.mark.parametrize("row,audits,expected", [
    ({"status": "rejected", "memo": MEMO}, [{"action": "attendance.absence.mark", "details": {"kind": "absence_marker"}}], "marker"),
    ({"status": "rejected", "memo": MEMO}, [{"action": "attendance.absence.mark", "details": '{"kind": "absence_marker"}'}], "marker"),
    # 최초 create 감사가 rejected·결근 메모여도 표지 감사가 없으면 실제 기록(지적 1: create 의 status 로 판정하지 않는다)
    ({"status": "rejected", "memo": MEMO}, [{"action": "attendance.create", "details": {"status": "rejected", "memo": MEMO}}], "real"),
    ({"status": "rejected", "memo": MEMO, "check_in_at": "2026-11-05T09:00:00+09:00"},
     [{"action": "attendance.create", "details": {"status": "approved", "memo": ""}}], "real"),
    ({"status": "rejected", "memo": MEMO, "source": "pwa"}, [], "real"),
    ({"status": "rejected", "memo": MEMO}, None, "real"),  # 판정 불가 → 실제 기록(데이터 손실 금지)
])
def test_classify_absence_row(row, audits, expected):
    assert dp.classify_absence_row(row, audits) == expected


def _absence_audits(db):
    return [a for a in db.audits if a["action"].startswith("attendance.absence.")]


# 리뷰 지적 1 — 기록 없는 날 동시 mark-absent: 락이 직렬화해 표지 1건
@pytest.mark.asyncio
async def test_concurrent_mark_absent_creates_single_marker():
    db = FakeDb([], [sched(A)])
    await asyncio.gather(run(db, A, "mark-absent"), run(db, A, "mark-absent"))
    markers = [r for r in db.records if r["memo"] == MEMO and not r.get("deleted_at")]
    assert len(markers) == 1
    lock_args = [args for sql, args in db.queries if sql == api.ABSENCE_DAY_LOCK_SQL]
    assert len(lock_args) == 2 and lock_args[0] == lock_args[1] and A in lock_args[0][0] and str(DAY) in lock_args[0][0]
    assert [a["action"] for a in _absence_audits(db)].count("attendance.absence.mark") == 1


# 리뷰 지적 2 — 근무표·근태 모두 없는 등록 직원은 허용, 과거 근태만 있는 미등록 이메일과 삭제 근태는 404
@pytest.mark.asyncio
async def test_employee_check_uses_roster_not_attendance_history():
    db = FakeDb([], [], roster=[A])
    result = await run(db, A, "mark-present")
    assert [r["status"] for r in db.records] == ["approved"] and result["changes"][0]["result"] == "inserted"
    stray = FakeDb([rec("rx", B, day=date(2026, 10, 1))], [], roster=[A])
    with pytest.raises(HTTPException) as exc:
        await run(stray, B, "mark-present")
    assert exc.value.status_code == 404
    assert not any("yeoljeong_attendance_records" in sql and "ORDER BY work_date DESC" in sql for sql, _ in stray.queries)
    assert "deleted_at IS NULL" in api.ABSENCE_EMPLOYEE_ROSTER_SQL and "status='approved'" in api.ABSENCE_EMPLOYEE_ROSTER_SQL


# 리뷰 지적 3 — 변경 없는 요청도 attendance.absence.* 감사 1건
@pytest.mark.asyncio
async def test_noop_requests_still_write_one_absence_audit():
    db = FakeDb([rec("ra", A)], [sched(A)])
    await run(db, A, "restore-present")  # 복원할 결근 없음
    assert [a["action"] for a in _absence_audits(db)] == ["attendance.absence.present"]
    db2 = FakeDb([rec("m1", A, "rejected", 0, MEMO)], [sched(A)],
                 [{"action": "attendance.absence.mark", "resource_id": "m1", "details": {"kind": "absence_marker"}}])
    await run(db2, A, "mark-absent")  # 이미 결근
    assert [a["action"] for a in _absence_audits(db2)] == ["attendance.absence.mark", "attendance.absence.noop"]
    assert len([r for r in db2.records if r["memo"] == MEMO]) == 1
