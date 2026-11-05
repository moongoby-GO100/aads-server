"""직원별 앱 설치·마지막 앱 접속 기록. 원본 UA·IP·위치는 저장하지 않는다."""
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api import obys_workspaces as ws
from app.services import yeoljeong_finance_service as finance

# 같은 값이면 last_seen_at 갱신도 이 간격 안에서는 건너뛴다(하루 1회 이상 쓰기 제한).
WRITE_INTERVAL = timedelta(days=1)


class PingIn(BaseModel):
    model_config = {"extra": "forbid"}
    display_mode: Literal["standalone", "browser"]
    platform: Literal["ios", "android", "desktop", "other"] = "other"
    event: Literal["launch", "appinstalled"] = "launch"


def ua_family(user_agent: str | None) -> str:
    """UA 원문에서 브라우저 계열 이름만 뽑는다. 원문은 어디에도 남기지 않는다."""
    ua = str(user_agent or "")
    for name, pattern in (
        ("samsung", r"SamsungBrowser"), ("edge", r"Edg(?:e|A|iOS)?/"), ("firefox", r"Firefox/|FxiOS"),
        ("chrome", r"Chrome/|CriOS"), ("safari", r"Safari/"),
    ):
        if re.search(pattern, ua):
            return name
    return "other"


def _same(row, payload: PingIn, family: str) -> bool:
    return (row["display_mode"] == payload.display_mode and row["platform"] == payload.platform
            and row["user_agent_family"] == family and payload.event != "appinstalled")


def row_status(row) -> dict:
    """행 하나를 화면용 상태로 바꾼다. row 가 없으면 '기록 없음'."""
    if not row:
        return {"state": "none", "last_standalone_at": None, "appinstalled_at": None, "last_seen_at": None, "platform": None}
    if row["last_standalone_at"]:
        state = "app_used"
    elif row["appinstalled_at"]:
        state = "installed"
    else:
        state = "browser_only"
    return {"state": state, "platform": row["platform"],
            "last_standalone_at": ws._json_value(row["last_standalone_at"]),
            "appinstalled_at": ws._json_value(row["appinstalled_at"]),
            "last_seen_at": ws._json_value(row["last_seen_at"])}


async def ping(user, business_id: str, payload: PingIn, user_agent: str | None, now: datetime | None = None):
    identity = ws._clock_identity(user)  # 활성 멤버십 + 이메일 필수. 본인 이메일은 로그인 정보에서만 가져온다.
    await ws._clock_business(user, business_id)
    tenant = ws.upload_svc._tenant(user)
    family = ua_family(user_agent)
    now = now or datetime.now(timezone.utc)
    standalone = payload.display_mode == "standalone"
    conn = await ws.upload_svc._connect()
    try:
        async with conn.transaction():
            await ws.upload_svc._require_business(conn, tenant, business_id)
            row = await conn.fetchrow(
                "SELECT * FROM obys_app_install_status WHERE business_id=$1 AND user_email=$2 AND tenant_id=$3 FOR UPDATE",
                business_id, identity["email"], tenant)
            if row and _same(row, payload, family) and now - row["updated_at"] < WRITE_INTERVAL:
                return {"recorded": False}
            installed_at = now if payload.event == "appinstalled" else None
            await conn.execute(
                """INSERT INTO obys_app_install_status
                   (tenant_id,business_id,user_email,platform,display_mode,first_standalone_at,last_standalone_at,
                    last_seen_at,appinstalled_at,user_agent_family,updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,$9,$7)
                   ON CONFLICT (business_id,user_email) DO UPDATE SET
                     platform=EXCLUDED.platform, display_mode=EXCLUDED.display_mode,
                     first_standalone_at=COALESCE(obys_app_install_status.first_standalone_at, EXCLUDED.first_standalone_at),
                     last_standalone_at=COALESCE(EXCLUDED.last_standalone_at, obys_app_install_status.last_standalone_at),
                     last_seen_at=EXCLUDED.last_seen_at,
                     appinstalled_at=COALESCE(obys_app_install_status.appinstalled_at, EXCLUDED.appinstalled_at),
                     user_agent_family=EXCLUDED.user_agent_family, updated_at=EXCLUDED.updated_at
                   WHERE obys_app_install_status.tenant_id=EXCLUDED.tenant_id""",
                tenant, business_id, identity["email"], payload.platform, payload.display_mode,
                now if standalone else None, now, installed_at, family)
            return {"recorded": True}
    finally:
        await conn.close()


async def status(user, business_id: str):
    ws._require_attendance_admin(user)  # owner/admin 만
    await ws._clock_business(user, business_id)
    tenant = ws.upload_svc._tenant(user)
    employees = await run_in_threadpool(finance.list_approved_employees, user, business_id)
    conn = await ws.upload_svc._connect()
    try:
        await ws.upload_svc._require_business(conn, tenant, business_id)
        rows = await conn.fetch(
            "SELECT * FROM obys_app_install_status WHERE tenant_id=$1 AND business_id=$2", tenant, business_id)
    finally:
        await conn.close()
    by_email = {str(r["user_email"]).lower(): r for r in rows}
    items = []
    for emp in employees:
        email = str(emp.get("email") or emp.get("employee_email") or "").strip().lower()
        items.append({"employee_id": emp.get("id"), "employee_name": emp.get("name") or emp.get("employee_name") or "",
                      "branch": emp.get("branch") or "", "email_masked": emp.get("email_masked") or "",
                      **row_status(by_email.get(email))})
    used = sum(1 for i in items if i["state"] == "app_used")
    return {"items": items, "total": len(items), "app_used": used}
