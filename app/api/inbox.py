"""오비스 알림 모아보기 — 통합 알림 API (`/api/v1/inbox`).

정본 PRD: document_key=ohvis-notification-inbox-prd (docs/prd/20261009_AADS_오비스_알림_모아보기_PRD.md).

흩어진 알림 출처 다섯 곳을 읽어 하나의 목록으로 돌려준다. 알림을 만드는 쪽은 건드리지 않는다.

| 출처(source)          | 테이블                          | 탭                  | 확인 저장                                  |
|-----------------------|---------------------------------|---------------------|--------------------------------------------|
| alert                 | alert_history                   | alert (+action)     | acknowledged / acknowledged_at             |
| approval              | agent_permission_requests       | action              | ohvis_inbox_read_marks (결정은 그대로 pending) |
| notify                | agent_permission_requests       | change              | decision='acknowledged' (기존 의미)        |
| ohvis_notification    | ohvis_notifications             | change              | read_at                                    |
| runner                | pipeline_runner_events          | runner (+action)    | ohvis_inbox_read_marks                     |

출처마다 병렬로 읽고 2초가 넘거나 실패하면 그 출처만 `degraded_sources` 로 돌려준다.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.core.db_pool import get_pool

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/inbox", tags=["inbox"])
ADMIN = Depends(require_tenant_role(TenantRole.ADMIN))

KST = timezone(timedelta(hours=9))
TABS = ("action", "alert", "change", "runner")
SOURCE_TIMEOUT_SEC = 2.0

ALERT_GROUP_WINDOW = timedelta(hours=24)
ALERT_WINDOW_DAYS = 7
NOTIFY_WINDOW_DAYS = 30
APPROVAL_WINDOW_DAYS = 30
RUNNER_WINDOW_DAYS = 7
OHVIS_WINDOW_DAYS = 30

ALERT_ROW_CAP = 3000
NOTIFY_ROW_CAP = 1500
APPROVAL_ROW_CAP = 300
OHVIS_ROW_CAP = 300
RUNNER_JOB_CAP = 1500

# model_attempt_* / cli_* 같은 내부 이벤트는 화이트리스트 밖이라 노출되지 않는다.
RUNNER_VISIBLE_EVENTS = (
    "job_started",
    "ai_review_result",
    "approval_requested",
    "approval_decision",
    "job_terminal",
)
RUNNER_SUCCESS_JOB_STATUSES = ("done", "approved", "completed")

SEVERITY_RANK = {"critical": 3, "high": 2, "warning": 1, "info": 0}
SOURCE_NAMES = ("alert", "approval", "notify", "ohvis_notification", "runner")
DEGRADED_LABEL = {
    "alert": "alert_history",
    "approval": "agent_permission_requests",
    "notify": "agent_permission_requests.notify",
    "ohvis_notification": "ohvis_notifications",
    "runner": "pipeline_runner_events",
}

_RUNNER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DIGITS_RE = re.compile(r"^[0-9]{1,18}$")


# ── 공통 모델 ────────────────────────────────────────────────────────────


@dataclass
class Item:
    source: str
    source_id: str
    home_tab: str
    project: str
    severity: str
    title: str
    summary: str
    occurred_at: datetime
    count: int = 1
    read: bool = False
    link: str = ""
    in_action: bool = False

    def tabs(self) -> tuple[str, ...]:
        return (self.home_tab, "action") if self.in_action and self.home_tab != "action" else (self.home_tab,)

    def public(self, tab: str) -> dict[str, Any]:
        actions = ["open"] if self.read else ["open", "read"]
        return {
            "source": self.source,
            "source_id": self.source_id,
            "tab": tab,
            "project": self.project,
            "severity": self.severity,
            "title": self.title,
            "summary": self.summary,
            "occurred_at_kst": self.occurred_at.astimezone(KST).isoformat(timespec="seconds"),
            "count": self.count,
            "read": self.read,
            "link": self.link,
            "actions": actions,
        }


@dataclass
class SourceResult:
    items: list[Item] = field(default_factory=list)
    # 이 출처가 탭에 기여하는 (total, unread) 를 목록 길이 대신 DB 집계로 덮어쓴다.
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass
class Ctx:
    tenant_id: str
    user_id: str
    now: datetime


class ReadRef(BaseModel):
    source: str
    source_id: str = Field(min_length=1, max_length=128)


class ReadBody(BaseModel):
    items: list[ReadRef] = Field(min_length=1, max_length=200)


class ReadAllBody(BaseModel):
    tab: str
    before: datetime


# ── 순수 함수 (DB 없이 테스트) ───────────────────────────────────────────


def normalize_severity(value: Any) -> str:
    sev = str(value or "").strip().lower()
    if sev in SEVERITY_RANK:
        return sev
    if sev in ("medium", "warn", "moderate"):
        return "warning"
    if sev in ("low", "notice", "debug", ""):
        return "info"
    if sev in ("error", "fatal", "emergency"):
        return "critical"
    return "info"


def _first_line(text: Any, limit: int = 80) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _clip(text: Any, limit: int = 160) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=KST)
    return datetime.fromtimestamp(0, tz=timezone.utc)


def group_alerts(rows: list[dict[str, Any]]) -> list[Item]:
    """같은 (project, category, title) 이 24시간 안에 반복되면 1행으로 묶는다.

    묶음의 기준은 그 묶음에서 가장 최근 행이다. 대표 행 id 를 source_id 로 쓰고,
    확인 처리(`_ack_alert_groups`)는 같은 규칙으로 묶음 전체를 확인한다.
    """
    ordered = sorted(rows, key=lambda r: (_as_dt(r["created_at"]), r["id"]), reverse=True)
    clusters: dict[tuple[str, str, str], list[list[dict[str, Any]]]] = {}
    for row in ordered:
        key = (str(row.get("project") or ""), str(row.get("category") or ""), str(row.get("title") or ""))
        bucket = clusters.setdefault(key, [])
        if bucket and _as_dt(bucket[-1][0]["created_at"]) - _as_dt(row["created_at"]) < ALERT_GROUP_WINDOW:
            bucket[-1].append(row)
        else:
            bucket.append([row])

    items: list[Item] = []
    for (project, _category, title), groups in clusters.items():
        for members in groups:
            head = members[0]
            severity = max((normalize_severity(m.get("severity")) for m in members), key=SEVERITY_RANK.__getitem__)
            read = all(bool(m.get("acknowledged")) for m in members)
            items.append(Item(
                source="alert",
                source_id=str(head["id"]),
                home_tab="alert",
                project=project,
                severity=severity,
                title=_first_line(title, 120),
                summary=_clip(head.get("message")),
                occurred_at=_as_dt(head["created_at"]),
                count=len(members),
                read=read,
                link=f"/decisions?alert={head['id']}",
                in_action=(severity == "critical" and not read),
            ))
    return items


def _task_label(row: dict[str, Any]) -> str:
    return _first_line(row.get("task_title"), 100) or "러너 작업"


def _runner_state(row: dict[str, Any]) -> tuple[str, str, bool]:
    """(상태 라벨, 심각도, 처리 필요 후보 여부)."""
    et, status = row.get("event_type"), str(row.get("status") or "")
    if et == "job_started":
        return "실행 중", "info", False
    if et == "ai_review_result":
        verdict = str(row.get("verdict") or "")
        return f"AI 리뷰 {verdict}".strip(), ("warning" if verdict and verdict != "APPROVE" else "info"), False
    if et == "approval_requested":
        return "승인 대기", "warning", True
    if et == "approval_decision":
        return ("반려됨" if status == "rejected" else "승인됨"), "info", False
    if et == "job_terminal":
        if status == "error":
            return "실패", "high", True
        if status == "review_hold":
            return "리뷰 보류", "warning", False
        return {"done": "완료", "rejected_done": "반려 종결", "cancelled": "취소됨"}.get(status, status or "종료"), "info", False
    return str(et or ""), "info", False


def is_superseded(job: dict[str, Any], successes: list[dict[str, Any]]) -> bool:
    """실패한 job 뒤에 같은 일을 성공시킨 job 이 있는가 (재작업 지시이거나 같은 지시문 해시·TASK_ID)."""
    start = _as_dt(job.get("job_created_at") or job.get("observed_at"))
    for ok in successes:
        if ok["job_id"] == job["job_id"] or ok.get("project") != job.get("project"):
            continue
        if _as_dt(ok.get("created_at")) <= start:
            continue
        if ok.get("rework_of") == job["job_id"]:
            return True
        if job.get("instruction_hash") and ok.get("instruction_hash") == job["instruction_hash"]:
            return True
        if job.get("task_id") and ok.get("task_id") == job["task_id"]:
            return True
    return False


def build_runner_cards(
    events: list[dict[str, Any]],
    successes: list[dict[str, Any]],
    marks: dict[str, datetime],
) -> list[Item]:
    """이벤트 행을 job 하나당 카드 1개(최신 상태)로 줄인다."""
    latest: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("event_type") not in RUNNER_VISIBLE_EVENTS:
            continue
        cur = latest.get(ev["job_id"])
        if cur is None or (_as_dt(ev["observed_at"]), ev.get("id", 0)) > (_as_dt(cur["observed_at"]), cur.get("id", 0)):
            latest[ev["job_id"]] = ev

    cards: list[Item] = []
    for job_id, ev in latest.items():
        label, severity, candidate = _runner_state(ev)
        in_action = False
        if candidate:
            if ev["event_type"] == "approval_requested":
                job_status = ev.get("job_status")
                in_action = job_status is None or job_status == "awaiting_approval"
            else:
                in_action = not is_superseded(ev, successes)
        occurred = _as_dt(ev["observed_at"])
        mark = marks.get(job_id)
        session = ev.get("session_id")
        link = f"/chat?session={session}&job={job_id}" if session else f"/chat?job={job_id}"
        cards.append(Item(
            source="runner",
            source_id=job_id,
            home_tab="runner",
            project=str(ev.get("project") or ""),
            severity=severity,
            title=_task_label(ev),
            summary=_clip(f"{label} · {ev.get('phase') or ''}".rstrip(" ·")),
            occurred_at=occurred,
            read=bool(mark and mark >= occurred),
            link=link,
            in_action=in_action,
        ))
    return cards


def _item_sort_key(item: Item) -> tuple[datetime, str, str]:
    return (item.occurred_at, item.source, item.source_id)


def encode_cursor(item: Item) -> str:
    raw = json.dumps([item.occurred_at.isoformat(), item.source, item.source_id])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at, source, source_id = json.loads(raw)
        return datetime.fromisoformat(at), str(source), str(source_id)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="cursor 형식이 올바르지 않습니다") from exc


def select_tab(
    items: list[Item],
    tab: str,
    *,
    project: str = "",
    severity: str = "",
    unread_only: bool = False,
    cursor: str = "",
    limit: int = 30,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    rows = [i for i in items if tab in i.tabs()]
    if project:
        rows = [i for i in rows if i.project.lower() == project.lower()]
    if severity:
        rows = [i for i in rows if i.severity == severity.lower()]
    if unread_only:
        rows = [i for i in rows if not i.read]
    oldest_first = tab == "action"
    rows.sort(key=_item_sort_key, reverse=not oldest_first)
    if cursor:
        pos = decode_cursor(cursor)
        rows = [i for i in rows if (_item_sort_key(i) > pos if oldest_first else _item_sort_key(i) < pos)]
    page = rows[:limit]
    nxt = encode_cursor(page[-1]) if len(rows) > limit and page else None
    return [i.public(tab) for i in page], nxt


# ── DB 접근 ──────────────────────────────────────────────────────────────


async def _read_marks(pool: Any, ctx: Ctx, source: str, ids: list[str]) -> dict[str, datetime]:
    if not ids:
        return {}
    try:
        rows = await pool.fetch(
            "SELECT source_id, read_at FROM ohvis_inbox_read_marks "
            "WHERE tenant_id=$1::uuid AND user_id=$2 AND source=$3 AND source_id = ANY($4::text[])",
            ctx.tenant_id, ctx.user_id, source, ids,
        )
    except Exception as exc:
        # 마이그레이션 전에는 표가 없다. 확인 표시만 못 읽을 뿐 알림 자체는 보여 줘야 한다.
        if getattr(exc, "sqlstate", None) == "42P01":
            logger.warning("inbox_read_marks_table_missing")
            return {}
        raise
    return {r["source_id"]: _as_dt(r["read_at"]) for r in rows}


async def load_alerts(pool: Any, ctx: Ctx) -> SourceResult:
    rows = await pool.fetch(
        "SELECT id, severity, category, title, message, project, acknowledged, created_at "
        "FROM alert_history "
        "WHERE acknowledged IS NOT TRUE OR created_at > now() - make_interval(days => $1) "
        "ORDER BY created_at DESC, id DESC LIMIT $2",
        ALERT_WINDOW_DAYS, ALERT_ROW_CAP,
    )
    # 탭 배지는 원본 행 수다: SELECT count(*) FROM alert_history WHERE NOT acknowledged 와 같아야 한다.
    stat = await pool.fetchrow(
        "SELECT count(*) AS total, count(*) FILTER (WHERE acknowledged IS NOT TRUE) AS unread FROM alert_history"
    )
    result = SourceResult(items=group_alerts([dict(r) for r in rows]))
    if stat is not None:
        result.counts["alert"] = (int(stat["total"]), int(stat["unread"]))
    return result


_PERMISSION_SELECT = (
    "SELECT r.id::text AS id, r.action_type, r.action_summary, r.risk_level, r.gate_source, "
    "       r.decision, r.created_at, w.project_key AS project "
    "FROM agent_permission_requests r "
    "LEFT JOIN chat_sessions cs ON cs.id::text = r.requested_by "
    "LEFT JOIN chat_workspaces w ON w.id = cs.workspace_id "
)


async def load_approvals(pool: Any, ctx: Ctx) -> SourceResult:
    rows = await pool.fetch(
        _PERMISSION_SELECT
        + "WHERE r.tenant_id=$1::uuid AND r.tier='approve' AND r.decision='pending' AND r.expires_at > now() "
        "  AND r.created_at > now() - make_interval(days => $2) "
        "ORDER BY r.created_at ASC LIMIT $3",
        ctx.tenant_id, APPROVAL_WINDOW_DAYS, APPROVAL_ROW_CAP,
    )
    marks = await _read_marks(pool, ctx, "approval", [r["id"] for r in rows])
    items = []
    for r in rows:
        created = _as_dt(r["created_at"])
        mark = marks.get(r["id"])
        items.append(Item(
            source="approval",
            source_id=r["id"],
            home_tab="action",
            project=str(r.get("project") or ""),
            severity=normalize_severity(r["risk_level"]),
            title=_first_line(r["action_summary"]) or "승인 요청",
            summary=_clip(f"요청 도구 {r['action_type']} · 위험도 {r['risk_level']}"),
            occurred_at=created,
            read=mark is not None,
            link=f"/approvals?focus={r['id']}",
        ))
    return SourceResult(items=items)


async def load_notifies(pool: Any, ctx: Ctx) -> SourceResult:
    rows = await pool.fetch(
        _PERMISSION_SELECT
        + "WHERE r.tenant_id=$1::uuid AND r.tier='notify' AND r.decision IN ('notified','acknowledged') "
        "  AND (r.decision='notified' OR r.created_at > now() - make_interval(days => $2)) "
        "ORDER BY r.created_at DESC LIMIT $3",
        ctx.tenant_id, NOTIFY_WINDOW_DAYS, NOTIFY_ROW_CAP,
    )
    stat = await pool.fetchrow(
        "SELECT count(*) AS total, count(*) FILTER (WHERE decision='notified') AS unread "
        "FROM agent_permission_requests "
        "WHERE tenant_id=$1::uuid AND tier='notify' AND decision IN ('notified','acknowledged') "
        "  AND (decision='notified' OR created_at > now() - make_interval(days => $2))",
        ctx.tenant_id, NOTIFY_WINDOW_DAYS,
    )
    items = [
        Item(
            source="notify",
            source_id=r["id"],
            home_tab="change",
            project=str(r.get("project") or ""),
            severity=normalize_severity(r["risk_level"]),
            title=_first_line(r["action_summary"]) or "변경 알림",
            summary=_clip(f"{r['gate_source']} · {r['action_type']}"),
            occurred_at=_as_dt(r["created_at"]),
            read=(r["decision"] == "acknowledged"),
            link=f"/approvals?focus={r['id']}",
        )
        for r in rows
    ]
    result = SourceResult(items=items)
    if stat is not None:
        result.counts["change"] = (int(stat["total"]), int(stat["unread"]))
    return result


async def load_ohvis_notifications(pool: Any, ctx: Ctx) -> SourceResult:
    rows = await pool.fetch(
        "SELECT id, project_key, kind, title, body, link, created_at, read_at "
        "FROM ohvis_notifications "
        "WHERE tenant_id=$1::uuid AND recipient_user_id=$2 "
        "  AND (read_at IS NULL OR created_at > now() - make_interval(days => $3)) "
        "ORDER BY created_at DESC LIMIT $4",
        ctx.tenant_id, ctx.user_id, OHVIS_WINDOW_DAYS, OHVIS_ROW_CAP,
    )
    return SourceResult(items=[
        Item(
            source="ohvis_notification",
            source_id=str(r["id"]),
            home_tab="change",
            project=str(r.get("project_key") or ""),
            severity="info",
            title=_first_line(r["title"]) or "시안 검토 알림",
            summary=_clip(r["body"]),
            occurred_at=_as_dt(r["created_at"]),
            read=r["read_at"] is not None,
            link=str(r.get("link") or ""),
        )
        for r in rows
    ])


async def load_runner(pool: Any, ctx: Ctx) -> SourceResult:
    events = await pool.fetch(
        r"""
        WITH latest AS (
            SELECT DISTINCT ON (e.job_id) e.id, e.job_id, e.project, e.event_type, e.status, e.phase,
                   e.metadata->>'verdict' AS verdict, e.observed_at
            FROM pipeline_runner_events e
            WHERE e.created_at > now() - make_interval(days => $1)
              AND e.event_type = ANY($2::text[])
              AND (e.tenant_id = $3::uuid OR e.tenant_id IS NULL)
            ORDER BY e.job_id, e.observed_at DESC, e.id DESC
        )
        SELECT l.*, pj.chat_session_id::text AS session_id, pj.status AS job_status,
               pj.instruction_hash, pj.created_at AS job_created_at,
               substring(pj.instruction from 'TITLE:[ \t]*([^\n]+)') AS task_title,
               substring(pj.instruction from 'TASK_ID:[ \t]*([^\s]+)') AS task_id
        FROM latest l LEFT JOIN pipeline_jobs pj ON pj.job_id = l.job_id
        ORDER BY l.observed_at DESC LIMIT $4
        """,
        RUNNER_WINDOW_DAYS, list(RUNNER_VISIBLE_EVENTS), ctx.tenant_id, RUNNER_JOB_CAP,
    )
    successes = await pool.fetch(
        r"""
        SELECT job_id, project, instruction_hash, created_at,
               substring(instruction from 'REWORK_OF:[ \t]*(runner-[0-9a-f]+)') AS rework_of,
               substring(instruction from 'TASK_ID:[ \t]*([^\s]+)') AS task_id
        FROM pipeline_jobs
        WHERE created_at > now() - make_interval(days => $1) AND status = ANY($2::text[])
        """,
        RUNNER_WINDOW_DAYS, list(RUNNER_SUCCESS_JOB_STATUSES),
    )
    marks = await _read_marks(pool, ctx, "runner", [r["job_id"] for r in events])
    return SourceResult(items=build_runner_cards([dict(r) for r in events], [dict(r) for r in successes], marks))


def _sources() -> dict[str, Callable[[Any, Ctx], Awaitable[SourceResult]]]:
    return {
        "alert": load_alerts,
        "approval": load_approvals,
        "notify": load_notifies,
        "ohvis_notification": load_ohvis_notifications,
        "runner": load_runner,
    }


async def _guarded(name: str, call: Callable[[], Awaitable[Any]]) -> tuple[str, Any, bool]:
    try:
        return name, await asyncio.wait_for(call(), timeout=SOURCE_TIMEOUT_SEC), False
    except asyncio.TimeoutError:
        logger.warning("inbox_source_timeout", source=name)
    except Exception as exc:
        logger.warning("inbox_source_failed", source=name, error=str(exc))
    return name, None, True


def _ctx(context: dict) -> Ctx:
    user = context.get("user") or {}
    uid = str(user.get("user_id") or user.get("id") or "")
    if not uid:
        raise HTTPException(status_code=403, detail="user_identity_required")
    return Ctx(tenant_id=str(context["tenant"]["id"]), user_id=uid, now=datetime.now(timezone.utc))


def _pool() -> Any:
    try:
        return get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="DB 풀이 준비되지 않았습니다") from exc


async def _load_all(ctx: Ctx) -> tuple[dict[str, SourceResult], list[str]]:
    pool = _pool()
    outcomes = await asyncio.gather(*[
        _guarded(name, lambda fn=fn: fn(pool, ctx)) for name, fn in _sources().items()
    ])
    results = {name: res for name, res, failed in outcomes if not failed}
    degraded = [DEGRADED_LABEL[name] for name, _res, failed in outcomes if failed]
    return results, degraded


def _all_items(results: dict[str, SourceResult]) -> list[Item]:
    return [i for res in results.values() for i in res.items]


# ── 조회 ────────────────────────────────────────────────────────────────


@router.get("/summary")
async def inbox_summary(context: dict = ADMIN):
    ctx = _ctx(context)
    results, degraded = await _load_all(ctx)
    tabs = {tab: {"total": 0, "unread": 0} for tab in TABS}
    for res in results.values():
        for tab in TABS:
            if tab in res.counts:
                total, unread = res.counts[tab]
            else:
                members = [i for i in res.items if tab in i.tabs()]
                total, unread = len(members), sum(1 for i in members if not i.read)
            tabs[tab]["total"] += total
            tabs[tab]["unread"] += unread
    return {"tabs": tabs, "degraded_sources": degraded}


@router.get("")
async def inbox_list(
    tab: str = Query("action", pattern="^(action|alert|change|runner)$"),
    project: str = Query("", max_length=40),
    severity: str = Query("", pattern="^(|critical|high|warning|info|CRITICAL|HIGH|WARNING|INFO)$"),
    unread_only: bool = Query(False),
    cursor: str = Query("", max_length=300),
    limit: int = Query(30, ge=1, le=100),
    context: dict = ADMIN,
):
    ctx = _ctx(context)
    results, degraded = await _load_all(ctx)
    items, next_cursor = select_tab(
        _all_items(results), tab,
        project=project, severity=severity, unread_only=unread_only, cursor=cursor, limit=limit,
    )
    return {"tab": tab, "items": items, "next_cursor": next_cursor, "degraded_sources": degraded}


# ── 확인 처리 ───────────────────────────────────────────────────────────


def _affected(status: Any) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _validate_ref(ref: ReadRef) -> None:
    ok = {
        "alert": _DIGITS_RE,
        "ohvis_notification": _DIGITS_RE,
        "approval": _UUID_RE,
        "notify": _UUID_RE,
        "runner": _RUNNER_ID_RE,
    }.get(ref.source)
    if ok is None:
        raise HTTPException(status_code=422, detail=f"알 수 없는 source: {ref.source}")
    if not ok.match(ref.source_id):
        raise HTTPException(status_code=422, detail=f"{ref.source} 의 source_id 형식이 올바르지 않습니다")


async def _upsert_marks(pool: Any, ctx: Ctx, source: str, ids: list[str]) -> int:
    await pool.execute(
        "INSERT INTO ohvis_inbox_read_marks(tenant_id, user_id, source, source_id, read_at) "
        "SELECT $1::uuid, $2, $3, x, now() FROM unnest($4::text[]) AS x "
        "ON CONFLICT (tenant_id, user_id, source, source_id) DO UPDATE SET read_at = EXCLUDED.read_at",
        ctx.tenant_id, ctx.user_id, source, ids,
    )
    return len(ids)


async def _ack_alert_groups(pool: Any, ids: list[str]) -> int:
    # group_alerts 와 같은 규칙: 대표 행과 같은 (project, category, title), 대표 시각 기준 24시간 안.
    await pool.execute(
        "UPDATE alert_history a SET acknowledged = TRUE, acknowledged_at = COALESCE(a.acknowledged_at, now()) "
        "FROM alert_history rep "
        "WHERE rep.id = ANY($1::int[]) AND a.acknowledged IS NOT TRUE "
        "  AND a.category = rep.category AND a.title = rep.title "
        "  AND a.project IS NOT DISTINCT FROM rep.project "
        "  AND a.created_at <= rep.created_at AND a.created_at > rep.created_at - interval '24 hours'",
        [int(i) for i in ids],
    )
    return len(ids)


async def _ack_notifies(pool: Any, ctx: Ctx, ids: list[str]) -> int:
    # /approvals/{id}/acknowledge 와 같은 의미 — 이미 확인했거나 없는 id 는 조용히 건너뛴다(멱등).
    await pool.execute(
        "UPDATE agent_permission_requests SET decision='acknowledged', decided_by=$2, decided_at=now(), "
        "updated_at=now() WHERE id = ANY($1::uuid[]) AND tenant_id=$3::uuid AND tier='notify' AND decision='notified'",
        ids, ctx.user_id, ctx.tenant_id,
    )
    return len(ids)


async def _read_ohvis(pool: Any, ctx: Ctx, ids: list[str]) -> int:
    await pool.execute(
        "UPDATE ohvis_notifications SET read_at = COALESCE(read_at, now()) "
        "WHERE id = ANY($1::bigint[]) AND tenant_id=$2::uuid AND recipient_user_id=$3",
        [int(i) for i in ids], ctx.tenant_id, ctx.user_id,
    )
    return len(ids)


def _mark_ops(pool: Any, ctx: Ctx, source: str, ids: list[str]) -> Callable[[], Awaitable[int]]:
    if source == "alert":
        return lambda: _ack_alert_groups(pool, ids)
    if source == "notify":
        return lambda: _ack_notifies(pool, ctx, ids)
    if source == "ohvis_notification":
        return lambda: _read_ohvis(pool, ctx, ids)
    return lambda: _upsert_marks(pool, ctx, source, ids)


@router.post("/read")
async def inbox_read(body: ReadBody, context: dict = ADMIN):
    ctx = _ctx(context)
    by_source: dict[str, list[str]] = {}
    for ref in body.items:
        _validate_ref(ref)
        bucket = by_source.setdefault(ref.source, [])
        if ref.source_id not in bucket:
            bucket.append(ref.source_id)
    pool = _pool()
    outcomes = await asyncio.gather(*[
        _guarded(src, _mark_ops(pool, ctx, src, ids)) for src, ids in by_source.items()
    ])
    failed = [DEGRADED_LABEL[n] for n, _r, bad in outcomes if bad]
    marked = {n: r for n, r, bad in outcomes if not bad}
    if failed and not marked:
        raise HTTPException(status_code=503, detail="확인 처리를 저장하지 못했습니다")
    return {"ok": not failed, "marked": marked, "failed_sources": failed}


def _before_utc(value: datetime, now: datetime) -> datetime:
    dt = value if value.tzinfo else value.replace(tzinfo=KST)
    return min(dt.astimezone(timezone.utc), now)


async def _read_all_marks(pool: Any, ctx: Ctx, loader: Any, source: str, before: datetime, only_action: bool) -> int:
    res: SourceResult = await loader(pool, ctx)
    ids = [
        i.source_id for i in res.items
        if i.source == source and not i.read and i.occurred_at <= before and (not only_action or i.in_action)
    ]
    return await _upsert_marks(pool, ctx, source, ids) if ids else 0


@router.post("/read-all")
async def inbox_read_all(body: ReadAllBody, context: dict = ADMIN):
    """탭의 `before` 이전 미확인 항목을 확인 처리한다. 사용자가 확인창을 거쳐 직접 누를 때만 쓴다."""
    if body.tab not in TABS:
        raise HTTPException(status_code=422, detail="tab 은 action/alert/change/runner 중 하나여야 합니다")
    ctx = _ctx(context)
    before = _before_utc(body.before, ctx.now)
    pool = _pool()
    srcs = _sources()
    ops: dict[str, Callable[[], Awaitable[int]]] = {}

    async def ack_alerts(critical_only: bool) -> int:
        status = await pool.execute(
            "UPDATE alert_history SET acknowledged = TRUE, acknowledged_at = COALESCE(acknowledged_at, now()) "
            "WHERE acknowledged IS NOT TRUE AND created_at <= $1 "
            "  AND ($2::boolean IS FALSE OR lower(severity) = 'critical')",
            before, critical_only,
        )
        return _affected(status)

    async def ack_changes() -> int:
        s1 = await pool.execute(
            "UPDATE agent_permission_requests SET decision='acknowledged', decided_by=$1, decided_at=now(), "
            "updated_at=now() WHERE tenant_id=$2::uuid AND tier='notify' AND decision='notified' AND created_at <= $3",
            ctx.user_id, ctx.tenant_id, before,
        )
        return _affected(s1)

    async def read_ohvis() -> int:
        s2 = await pool.execute(
            "UPDATE ohvis_notifications SET read_at = now() "
            "WHERE tenant_id=$1::uuid AND recipient_user_id=$2 AND read_at IS NULL AND created_at <= $3",
            ctx.tenant_id, ctx.user_id, before,
        )
        return _affected(s2)

    if body.tab == "alert":
        ops["alert"] = lambda: ack_alerts(False)
    elif body.tab == "change":
        ops["notify"] = ack_changes
        ops["ohvis_notification"] = read_ohvis
    elif body.tab == "runner":
        ops["runner"] = lambda: _read_all_marks(pool, ctx, srcs["runner"], "runner", before, False)
    else:
        ops["approval"] = lambda: _read_all_marks(pool, ctx, srcs["approval"], "approval", before, False)
        ops["runner"] = lambda: _read_all_marks(pool, ctx, srcs["runner"], "runner", before, True)
        ops["alert"] = lambda: ack_alerts(True)

    outcomes = await asyncio.gather(*[_guarded(n, fn) for n, fn in ops.items()])
    failed = [DEGRADED_LABEL[n] for n, _r, bad in outcomes if bad]
    marked = {n: r for n, r, bad in outcomes if not bad}
    if failed and not marked:
        raise HTTPException(status_code=503, detail="확인 처리를 저장하지 못했습니다")
    return {"ok": not failed, "tab": body.tab, "marked": marked, "failed_sources": failed}
