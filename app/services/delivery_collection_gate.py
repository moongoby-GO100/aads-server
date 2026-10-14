"""배달앱 자동수집 시도 게이트 (순수 판정 + 상태 1행 기록).

2026-10-14 실측: 9/23 이후 수집 시도가 없고, 그 전에는 같은 로그인 실패가 수천 건 쌓였다.
  - 로그인 필요/캡차/포털 차단은 사람이 조치해야 풀리는데 45분 쿨다운이 끝나면 다시 시도했다.
  - PC Agent 가 없어도 시도해 PC_AGENT_*_REQUIRED 행이 계속 늘었다.

이 모듈은 "이번 스케줄에서 이 앱·매장을 시도해도 되는가"만 판정한다. DB·네트워크를 읽지 않는다
(상태 행 목록을 인자로 받는다). 비밀번호·쿠키·토큰은 다루지 않는다.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

KST = timezone(timedelta(hours=9))

SUCCESS_STATUSES = {"succeeded", "completed", "success", "no_records"}
RUNNING_STATUSES = {"queued", "running"}

# 사람이 조치해야 풀리는 오류 → 조치 종류. 이 코드들은 자동 재시도하지 않는다.
USER_ACTION_KIND_BY_CODE: dict[str, str] = {
    "PC_AGENT_LOGIN_REQUIRED": "login_required",
    "PC_AGENT_SESSION_REQUIRED": "login_required",
    "MISSING_CREDENTIALS": "login_required",
    "DDANGYO_NUMERIC_CAPTCHA_REQUIRED": "captcha",
    "PORTAL_AUTH_CHALLENGE": "captcha",
    "PORTAL_BLOCKED": "portal_blocked",
    "BAEMIN_SECURITY_BLOCKED": "portal_blocked",
    "COUPANGEATS_SECURITY_BLOCKED": "portal_blocked",
    "SECURITY_BLOCKED": "portal_blocked",
    "CSV_UPLOAD_REQUIRED": "csv_upload",
}

USER_ACTION_MESSAGE_KO: dict[str, str] = {
    "login_required": "로그인 필요: PC Agent를 연결하고 해당 앱에 1회 수동 로그인한 뒤 수집을 실행하세요. 자동 재시도하지 않습니다.",
    "captcha": "캡차/추가 인증 필요: 화면에서 직접 인증한 뒤 수집을 실행하세요. 자동 재시도하지 않습니다.",
    "portal_blocked": "포털 접근 차단: 자동 재시도를 멈췄습니다. 포털 정산 CSV를 업로드해 대체하세요.",
    "csv_upload": "정산 CSV 업로드 필요: 포털에서 내려받은 정산 CSV를 업로드하세요.",
}

GATE_STATE_READY = "ready"
GATE_STATE_PAUSED = "paused"
GATE_STATE_AGENT_OFFLINE = "agent_offline"
GATE_STATE_ALREADY_RUNNING = "already_running"
GATE_STATE_COOLDOWN = "cooldown"
GATE_STATE_USER_ACTION = "user_action_required"

DEFAULT_FAILURE_COOLDOWN_MINUTES = 60
DEFAULT_RUNNING_STALE_MINUTES = 15


def error_codes(value: Any) -> list[str]:
    """'A,B' 처럼 이어 붙은 오류코드를 대문자 목록으로 푼다."""
    return [part.strip().upper() for part in str(value or "").split(",") if part.strip()]


def user_action_kind(error_code: Any) -> str:
    for code in error_codes(error_code):
        kind = USER_ACTION_KIND_BY_CODE.get(code)
        if kind:
            return kind
    return ""


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=KST)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def row_time(row: dict[str, Any]) -> datetime | None:
    for key in ("updated_at", "finished_at", "started_at", "queued_at", "created_at"):
        parsed = parse_time(row.get(key))
        if parsed:
            return parsed
    return None


def _scope_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("service") or "").strip(),
        str(row.get("business_id") or "").strip(),
        str(row.get("branch") or "").strip(),
    )


def latest_rows_by_scope(
    statuses: Iterable[dict[str, Any]], service: str | None = None
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """앱·사업자·지점별 가장 최근 상태 행."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    floor = datetime.min.replace(tzinfo=KST)
    for row in statuses or []:
        if not isinstance(row, dict):
            continue
        key = _scope_key(row)
        if not key[0] or (service and key[0] != service):
            continue
        current = latest.get(key)
        if current is None or (row_time(row) or floor) >= (row_time(current) or floor):
            latest[key] = row
    return latest


def evaluate_scope(
    row: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    failure_cooldown_minutes: int = DEFAULT_FAILURE_COOLDOWN_MINUTES,
    running_stale_minutes: int = DEFAULT_RUNNING_STALE_MINUTES,
) -> dict[str, Any]:
    """한 앱·매장의 마지막 상태 행으로 이번 시도 가능 여부를 판정한다.

    사용자 조치 상태는 만료가 없다. 같은 범위에 더 새로운 성공 행이 생겨야(= 사람이 조치한 뒤
    수동 수집이 성공해야) 풀린다.
    """
    now = now or datetime.now(KST)
    if not row:
        return {"run": True, "state": GATE_STATE_READY, "reason": "no_history"}
    status = str(row.get("status") or row.get("raw_status") or "").strip().lower()
    at = row_time(row)
    if status in SUCCESS_STATUSES:
        return {"run": True, "state": GATE_STATE_READY, "reason": "last_succeeded"}
    if status in RUNNING_STATUSES:
        if at and now - at < timedelta(minutes=max(1, running_stale_minutes)):
            return {"run": False, "state": GATE_STATE_ALREADY_RUNNING, "reason": "COLLECTION_ALREADY_RUNNING"}
        return {"run": True, "state": GATE_STATE_READY, "reason": "stale_running"}
    kind = user_action_kind(row.get("error_code"))
    if kind:
        return {
            "run": False,
            "state": GATE_STATE_USER_ACTION,
            "reason": error_codes(row.get("error_code"))[0],
            "user_action": {
                "kind": kind,
                "error_code": error_codes(row.get("error_code"))[0],
                "since": str(row.get("updated_at") or row.get("finished_at") or ""),
                "message": USER_ACTION_MESSAGE_KO[kind],
                "csv_fallback": True,
            },
        }
    if status in {"failed", "error", "action_required", "blocked"} and at:
        until = at + timedelta(minutes=max(1, failure_cooldown_minutes))
        if until > now:
            return {
                "run": False,
                "state": GATE_STATE_COOLDOWN,
                "reason": (error_codes(row.get("error_code")) or ["FAILED"])[0],
                "cooldown_until": until.isoformat(timespec="seconds"),
            }
    return {"run": True, "state": GATE_STATE_READY, "reason": "retry_allowed"}


def scope_gate(
    statuses: Iterable[dict[str, Any]],
    service: str,
    business_id: str,
    branch: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """큐 항목 하나(앱·사업자·지점)에 대한 판정. business_id/branch 가 'all'/'전체' 면 앱 전체로 본다."""
    rows = latest_rows_by_scope(statuses, service)
    scoped = [
        row
        for (svc, biz, br), row in rows.items()
        if (business_id in ("", "all") or biz == business_id) and (branch in ("", "전체") or br == branch)
    ]
    if not scoped:
        return evaluate_scope(None, **kwargs)
    decisions = [evaluate_scope(row, **kwargs) for row in scoped]
    return next((item for item in decisions if item["run"]), decisions[0])


def evaluate_service(
    service: str,
    statuses: Iterable[dict[str, Any]],
    *,
    agent_online: bool,
    paused: bool = False,
    pause_reason: str = "",
    now: datetime | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """스케줄러가 앱 하나를 시도할지 판정한다. 순서: 정지 스위치 → PC Agent → 범위별 상태."""
    if paused:
        return {"run": False, "state": GATE_STATE_PAUSED, "reason": pause_reason or "collection_paused", "service": service}
    if not agent_online:
        return {"run": False, "state": GATE_STATE_AGENT_OFFLINE, "reason": "PC_AGENT_OFFLINE", "service": service}
    rows = latest_rows_by_scope(statuses, service)
    if not rows:
        return {"run": True, "state": GATE_STATE_READY, "reason": "no_history", "service": service}
    decisions = [evaluate_scope(row, now=now, **kwargs) for row in rows.values()]
    runnable = [item for item in decisions if item["run"]]
    if runnable:
        blocked = [item for item in decisions if not item["run"]]
        return {**runnable[0], "service": service, "blocked_scopes": len(blocked)}
    # 전 범위가 막혔다. 사람 조치가 있으면 그것이 우선 사유다.
    chosen = next((item for item in decisions if item["state"] == GATE_STATE_USER_ACTION), decisions[0])
    return {**chosen, "service": service, "blocked_scopes": len(decisions)}


def user_actions_from_statuses(statuses: Iterable[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """화면 알림용: 사용자 조치가 필요한 앱·사업자·지점 목록."""
    result: list[dict[str, Any]] = []
    for (service, business_id, branch), row in sorted(latest_rows_by_scope(statuses).items()):
        decision = evaluate_scope(row, now=now)
        if decision["state"] != GATE_STATE_USER_ACTION:
            continue
        result.append({"service": service, "business_id": business_id, "branch": branch, **decision["user_action"]})
    return result


# ── 상태 1행 기록 ─────────────────────────────────────────────────────────
# 게이트가 시도를 건너뛴 사실은 delivery_collection_status 에 행을 만들지 않고
# system_memory(ops/delivery_collection_gate) 한 행의 앱별 필드만 덮어쓴다 — 오프라인이 길어져도
# 행이 늘지 않는다.
_GATE_CATEGORY = "ops"
_GATE_KEY = "delivery_collection_gate"
_GATE_REWRITE_SECONDS = 1800.0
_last_written: dict[str, tuple[str, str, float]] = {}


def gate_state_payload(service: str, decision: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now_text = (now or datetime.now(KST)).isoformat(timespec="seconds")
    payload = {
        "state": decision.get("state") or "",
        "reason": decision.get("reason") or "",
        "checked_at": now_text,
        "run": bool(decision.get("run")),
    }
    for key in ("cooldown_until", "user_action", "blocked_scopes"):
        if decision.get(key) is not None:
            payload[key] = decision[key]
    return payload


async def record_gate_state(service: str, decision: dict[str, Any]) -> bool:
    """앱별 게이트 상태를 기록한다. 같은 상태는 30분에 한 번만 쓴다. 실패해도 수집을 막지 않는다."""
    import json

    state = str(decision.get("state") or "")
    reason = str(decision.get("reason") or "")
    previous = _last_written.get(service)
    mono = time.monotonic()
    if previous and previous[0] == state and previous[1] == reason and mono - previous[2] < _GATE_REWRITE_SECONDS:
        return False
    try:
        from app.core.db_pool import get_pool

        await get_pool().execute(
            """
            INSERT INTO system_memory (category, key, value, updated_by)
            VALUES ($1, $2, jsonb_build_object($3::text, $4::jsonb), 'delivery_gate')
            ON CONFLICT (category, key) DO UPDATE
               SET value = jsonb_set(COALESCE(system_memory.value, '{}'::jsonb), ARRAY[$3::text], $4::jsonb, true),
                   updated_at = NOW(), updated_by = 'delivery_gate'
            """,
            _GATE_CATEGORY,
            _GATE_KEY,
            service,
            json.dumps(gate_state_payload(service, decision), ensure_ascii=False),
        )
    except Exception:
        return False
    _last_written[service] = (state, reason, mono)
    return True


async def read_gate_states() -> dict[str, Any]:
    import json

    try:
        from app.core.db_pool import get_pool

        raw = await get_pool().fetchval(
            "SELECT value FROM system_memory WHERE category=$1 AND key=$2", _GATE_CATEGORY, _GATE_KEY
        )
    except Exception:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}
