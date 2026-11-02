"""직원 근태 정정 버전. 모든 쓰기는 업무 DB의 단일 트랜잭션에서 실행한다."""
import json
import asyncpg
from datetime import date, time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from app.api import obys_workspaces as ws


class CorrectionIn(BaseModel):
    model_config = {"extra": "forbid"}
    work_date: date
    start_at: time
    end_at: time
    break_minutes: int = Field(ge=0, lt=1440)
    reason: str = Field(min_length=1, max_length=1000)
    supersedes_id: str | None = None

    @model_validator(mode="after")
    def validate_values(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("정정 사유가 필요합니다")
        if self.start_at.second or self.end_at.second or self.start_at.microsecond or self.end_at.microsecond:
            raise ValueError("시각은 분 단위여야 합니다")
        ws.attendance_worked_minutes(self.start_at, self.end_at, self.break_minutes)
        return self


class DecisionIn(BaseModel):
    model_config = {"extra": "forbid"}
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_reason(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("처리 사유가 필요합니다")
        return self


def snapshot(row):
    return ws._json_value(dict(row))


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def transition(row, action, version, *, actor=None, reason=None):
    """Only the same actor, action, reason and expected version replay without writes/audit."""
    target = {"approve": "approved", "reject": "rejected", "withdraw": "withdrawn"}[action]
    if row["status"] == target and row["version"] == version + 1:
        if actor is not None and (email(row["decided_by"]) != email(actor) or row["decision_reason"] != reason):
            raise HTTPException(409, "다른 처리의 재시도입니다")
        return False
    if row["status"] != "pending" or row["version"] != version:
        raise HTTPException(409, "정정 요청 상태 또는 버전이 변경되었습니다")
    return True


def email(value):
    return value.strip().lower()


def attendance_revision(row):
    """Compare business inputs only; JSONB and DB temporal values normalize identically."""
    row = decoded(row)
    result = {key: row.get(key) for key in (
        "employee_email", "work_date", "start_at", "end_at", "break_minutes",
        "hourly_wage", "status", "check_in_at", "check_out_at", "deleted_at")}
    for key in ("start_at", "end_at"):
        if result[key] is not None:
            result[key] = time.fromisoformat(str(result[key])).isoformat()
    for key in ("work_date", "check_in_at", "check_out_at", "deleted_at"):
        if result[key] is not None:
            result[key] = str(result[key]).replace(" ", "T")
    if result["hourly_wage"] is not None:
        from decimal import Decimal
        result["hourly_wage"] = Decimal(str(result["hourly_wage"]))
    result["employee_email"] = email(result["employee_email"])
    return result


async def run(user, business_id, operation, *, record_id=None, correction_id=None, payload=None):
    identity = {**ws._clock_identity(user)}
    identity["email"] = email(identity["email"])
    await ws._clock_business(user, business_id)
    tenant = ws.upload_svc._tenant(user)
    manager = ws.upload_svc.tenant_session_for_user(user)["permissions"]["can_manage_settings"]
    if operation in {"approve", "reject"}:
        ws._require_attendance_admin(user)
    conn = await ws.upload_svc._connect()
    try:
        async with conn.transaction():
            await ws.upload_svc._require_business(conn, tenant, business_id)
            if operation == "list":
                rows = await conn.fetch(
                    "SELECT * FROM obys_attendance_corrections WHERE tenant_id=$1 AND business_id=$2 "
                    "AND ($3::boolean OR lower(employee_email)=$4) ORDER BY created_at DESC LIMIT 500",
                    tenant, business_id, manager, identity["email"])
                return {"items": [ws._json_value(dict(r)) for r in rows]}
            # Serialize requests/decisions within the business, including weekly totals.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "correction:" + business_id)
            request = None
            if correction_id:
                request = await conn.fetchrow(
                    "SELECT * FROM obys_attendance_corrections WHERE id=$1 AND tenant_id=$2 AND business_id=$3 FOR UPDATE",
                    correction_id, tenant, business_id)
                if not request or (operation == "withdraw" and email(request["employee_email"]) != identity["email"]):
                    raise HTTPException(404, "정정 요청을 찾을 수 없습니다")
                record_id = request["record_id"]
                if not transition(request, operation, payload.version, actor=identity["email"], reason=payload.reason):
                    return ws._json_value(dict(request))
            record = await conn.fetchrow(
                "SELECT * FROM yeoljeong_attendance_records WHERE id=$1 AND business_id=$2 AND deleted_at IS NULL FOR UPDATE",
                record_id, business_id)
            if not record or (operation == "create" and email(record["employee_email"]) != identity["email"]):
                raise HTTPException(404, "본인의 근태를 찾을 수 없습니다")
            before = snapshot(record)
            if operation == "create":
                request = await create_request(conn, tenant, business_id, record_id, identity, payload, before)
            else:
                if operation == "approve":
                    await approve(conn, tenant, business_id, record_id, record, request, before)
                request = await conn.fetchrow(
                    "UPDATE obys_attendance_corrections SET status=$4,version=version+1,decided_by=$5,decided_at=now(),decision_reason=$6 WHERE id=$1 AND tenant_id=$2 AND business_id=$3 RETURNING *",
                    correction_id, tenant, business_id, {"approve": "approved", "reject": "rejected", "withdraw": "withdrawn"}[operation], identity["email"], payload.reason)
            after = before
            if operation == "approve":
                after = snapshot(await conn.fetchrow(
                    "SELECT * FROM yeoljeong_attendance_records WHERE id=$1 AND business_id=$2",
                    record_id, business_id))
            await ws._attendance_audit(conn, business_id, user, "attendance.correction." + operation, request["id"],
                                       {"before": before, "after": after, "request": dict(request)})
            return ws._json_value(dict(request))
    except (asyncpg.UniqueViolationError, asyncpg.DeadlockDetectedError, asyncpg.SerializationError) as exc:
        raise HTTPException(409, "동시 수정 충돌입니다. 최신 정정 버전으로 다시 시도하십시오") from exc
    finally:
        await conn.close()


async def approve(conn, tenant, business_id, record_id, record, request, before):
    requested = CorrectionIn(**decoded(request["requested"]), reason=request["reason"])
    if attendance_revision(before) != attendance_revision(request["original"]):
        raise HTTPException(409, "원본 근태가 변경되었습니다. 새 정정 버전으로 신청하십시오")
    months = sorted({record["work_date"].strftime("%Y-%m"), requested.work_date.strftime("%Y-%m")})
    # Payroll writes acquire the same business lock via a DB trigger.
    # This fences confirmation/inserts without blocking other businesses.
    locked = await conn.fetch(
        "SELECT id,payroll_month,status FROM yeoljeong_payroll_statements WHERE tenant_id=$1 AND business_id=$2 "
        "AND lower(employee_email)=$3 AND left(payroll_month,7)=ANY($4::text[]) "
        "AND status IN ('confirmed','paid') AND deleted_at IS NULL FOR UPDATE",
        tenant, business_id, email(request["employee_email"]), months)
    if locked:
        raise HTTPException(409, {"reason": "마감 근태 변경은 급여 정정 버전이 필요합니다", "payroll_impact": ws._json_value([dict(r) for r in locked]), "months": months})
    if await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM yeoljeong_attendance_records WHERE business_id=$1 "
        "AND lower(employee_email)=$2 AND work_date=$3 AND id<>$4 AND deleted_at IS NULL)",
        business_id, email(request["employee_email"]), requested.work_date, record_id):
        raise HTTPException(409, "정정 날짜에 기존 근태가 있습니다")
    await conn.execute(
        "UPDATE yeoljeong_attendance_records SET work_date=$3,start_at=$4,end_at=$5,break_minutes=$6,worked_minutes=$7,night_minutes=$8,updated_at=now() WHERE id=$1 AND business_id=$2",
        record_id, business_id, requested.work_date, requested.start_at, requested.end_at, requested.break_minutes,
        ws.attendance_worked_minutes(requested.start_at, requested.end_at, requested.break_minutes),
        ws.attendance_night_minutes(requested.start_at, requested.end_at, requested.break_minutes))
    for day in {record["work_date"], requested.work_date}:
        await ws._recompute_attendance_week(conn, business_id, request["employee_email"], day)


async def create_request(conn, tenant, business_id, record_id, identity, payload, before):
    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM obys_attendance_corrections WHERE business_id=$1 AND record_id=$2 AND status='pending')", business_id, record_id):
        raise HTTPException(409, "처리 대기 중인 정정 요청이 있습니다")
    if payload.supersedes_id:
        previous = await conn.fetchrow("SELECT * FROM obys_attendance_corrections WHERE id=$1 AND tenant_id=$2 AND business_id=$3 FOR UPDATE", payload.supersedes_id, tenant, business_id)
        if not previous or previous["record_id"] != record_id or email(previous["employee_email"]) != identity["email"] or previous["status"] not in {"rejected", "withdrawn"}:
            raise HTTPException(409, "반려·철회된 본인 요청만 재신청할 수 있습니다")
    if payload.supersedes_id and await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM obys_attendance_corrections WHERE supersedes_id=$1)", payload.supersedes_id):
        raise HTTPException(409, "이미 재신청된 요청입니다")
    request = await conn.fetchrow(
        "INSERT INTO obys_attendance_corrections (id,tenant_id,business_id,record_id,employee_email,original,requested,reason,supersedes_id) "
        "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9) RETURNING *",
        str(uuid4()), tenant, business_id, record_id, identity["email"], json.dumps(before),
        payload.model_dump_json(exclude={"reason", "supersedes_id"}), payload.reason, payload.supersedes_id)
    return request
