"""클로브 MCP 다회사 읽기 전용 수집 — OBYS DB 쪽 계약(연결·임대·수집·검토함·확정 원장·상태).

단계는 분리돼 있다. 수집은 검토함(review_box)까지만 쓰고, 회계 검토(reviewed)와 원장 확정(confirmed)은
각각 별도 호출이다. 조회가 원장을 자동 확정하는 경로는 이 모듈에 없다.
쓰기는 모두 같은 트랜잭션 안에서 임대(lease)를 FOR UPDATE 로 잠가 owner/epoch 를 대조한다.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from app.core.obys_db import obys_db_url
from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_core as core

logger = logging.getLogger(__name__)

LEASE_SECONDS = 300
ABSENT_ERROR = "absent_from_context"
# get_my_context 가 연속 이만큼 한 회사를 빼먹어야 absent 로 표시한다. 한 번의 부분 응답으로는 정상 연결을 흔들지 않는다.
ABSENT_AFTER_MISSES = 2
DEFAULT_INSTANCE = "aads-api"


class CollectionError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status


class LeaseLost(CollectionError):
    def __init__(self) -> None:
        super().__init__("lease_lost", 409)


async def _connect():
    import asyncpg

    return await asyncpg.connect(obys_db_url(), timeout=5)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def mask_reg_no(value: Any) -> str:
    d = core.digits(value)
    return f"***-**-*{d[-4:]}" if len(d) == 10 else ""


def _link_view(row: Any) -> dict[str, Any]:
    r = dict(row)
    return {
        "clobe_company_id": r["clobe_company_id"],
        "company_name": r["company_name"],
        "reg_no_masked": mask_reg_no(r["reg_no"]),
        "link_status": r["link_status"],
        "link_basis": r["link_basis"],
        "tenant_id": str(r["tenant_id"]) if r.get("tenant_id") else None,
        "business_id": r.get("business_id"),
        "candidate_business_ids": list(r.get("candidate_business_ids") or []),
        "permission_ok": r.get("permission_ok"),
        "permission_checked_at": r["permission_checked_at"].isoformat() if r.get("permission_checked_at") else None,
        "permission_error": r.get("permission_error"),
        "approved_by": r.get("approved_by"),
    }


# ── 회사 발견 · 권한 확인 · 연결 ─────────────────────────

async def _all_businesses(conn: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, tenant_id, name, registration_no FROM yeoljeong_businesses WHERE deleted_at IS NULL"
    )
    return [dict(r) for r in rows]


def _bump(summary: dict[str, Any], key: str) -> None:
    summary[key] = int(summary.get(key, 0)) + 1


async def discover_companies(*, session_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """get_my_context 의 실제 회사 목록으로 연결 테이블을 갱신한다. 앱 수정 없이 새 회사가 review 로 들어온다."""
    factory = session_factory or clobe.ReadSession
    async with factory() as session:
        context = await session.call("get_my_context")
    companies = clobe.parse_companies(context)
    conn = await _connect()
    try:
        businesses = await _all_businesses(conn)
        summary = {"seen": len(companies), "new": 0, "linked": 0, "review": 0, "blocked": 0, "absent": 0}
        async with conn.transaction():
            for company in companies:
                existing = await conn.fetchrow(
                    "SELECT link_status, link_basis FROM obys_clobe_company_link WHERE clobe_company_id=$1 FOR UPDATE",
                    company["company_id"],
                )
                decision = core.decide_link(company, businesses)
                if existing is None:
                    summary["new"] += 1
                # linked 는 판정이 바뀌어도 되돌리지 않는다. blocked 는 사유(admin_blocked·excluded_scope 등)와 무관하게
                # fail-closed 로 고정한다 — 차단이 재판정으로 review/linked 로 풀리는 경로는 없고, 해제는 운영자 조치다.
                kept = existing is not None and existing["link_status"] in ("linked", "blocked")
                if kept:
                    await conn.execute(
                        "UPDATE obys_clobe_company_link SET company_name=$2, reg_no=$3, clobe_role=$4, last_seen_at=now(), "
                        " missing_streak=0, "
                        " permission_ok=CASE WHEN permission_error=$5 THEN NULL ELSE permission_ok END, "
                        " permission_error=CASE WHEN permission_error=$5 THEN NULL ELSE permission_error END "
                        "WHERE clobe_company_id=$1",
                        company["company_id"], company["name"], company["reg_no"], company["role"], ABSENT_ERROR,
                    )
                    _bump(summary, existing["link_status"])
                    continue
                biz = decision["business"]
                status = decision["status"]
                await conn.execute(
                    "INSERT INTO obys_clobe_company_link (clobe_company_id, company_name, reg_no, clobe_role, link_status, "
                    " link_basis, tenant_id, business_id, candidate_business_ids) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                    "ON CONFLICT (clobe_company_id) DO UPDATE SET company_name=EXCLUDED.company_name, reg_no=EXCLUDED.reg_no, "
                    " clobe_role=EXCLUDED.clobe_role, link_status=EXCLUDED.link_status, link_basis=EXCLUDED.link_basis, "
                    " tenant_id=EXCLUDED.tenant_id, business_id=EXCLUDED.business_id, "
                    " candidate_business_ids=EXCLUDED.candidate_business_ids, last_seen_at=now(), missing_streak=0, "
                    " permission_ok=CASE WHEN obys_clobe_company_link.permission_error=$10 THEN NULL "
                    "  ELSE obys_clobe_company_link.permission_ok END, "
                    " permission_error=CASE WHEN obys_clobe_company_link.permission_error=$10 THEN NULL "
                    "  ELSE obys_clobe_company_link.permission_error END",
                    company["company_id"], company["name"], company["reg_no"], company["role"], status,
                    decision["basis"], biz["tenant_id"] if biz else None, biz["id"] if biz else None,
                    decision["candidates"], ABSENT_ERROR,
                )
                _bump(summary, status)
            seen_ids = [c["company_id"] for c in companies]
            if seen_ids:
                # 안 보인 회사는 연속 미등장 횟수만 올리고, ABSENT_AFTER_MISSES 에 닿았을 때 비로소 absent 로 표시한다.
                # 실제 권한 오류(permission_denied 등)가 이미 적힌 행은 absent 로 덮어쓰지 않는다.
                gone = await conn.fetch(
                    "UPDATE obys_clobe_company_link SET missing_streak = missing_streak + 1, "
                    " permission_ok = CASE WHEN missing_streak + 1 >= $3 AND COALESCE(permission_error,$2) = $2 "
                    "  THEN FALSE ELSE permission_ok END, "
                    " permission_error = CASE WHEN missing_streak + 1 >= $3 AND COALESCE(permission_error,$2) = $2 "
                    "  THEN $2 ELSE permission_error END, "
                    " permission_checked_at = CASE WHEN missing_streak + 1 = $3 AND COALESCE(permission_error,$2) = $2 "
                    "  THEN now() ELSE permission_checked_at END "
                    "WHERE NOT (clobe_company_id = ANY($1::text[])) "
                    "RETURNING missing_streak, permission_error",
                    seen_ids, ABSENT_ERROR, ABSENT_AFTER_MISSES,
                )
                summary["missing"] = len(gone)
                summary["absent"] = sum(1 for g in gone if g["missing_streak"] == ABSENT_AFTER_MISSES and g["permission_error"] == ABSENT_ERROR)
            else:
                # 빈 목록은 "모든 회사가 사라졌다"가 아니라 일시 장애일 가능성이 더 높다 — 미등장 횟수도 올리지 않는다.
                summary["absent_skipped"] = "empty_context"
        return summary
    finally:
        await conn.close()


async def _get_link(conn: Any, company_id: str) -> Any:
    row = await conn.fetchrow("SELECT * FROM obys_clobe_company_link WHERE clobe_company_id=$1", company_id)
    if row is None:
        raise CollectionError("company_not_discovered", 404)
    return row


async def check_company_permission(
    company_id: str, *, session_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """회사별 접근 권한을 읽기 도구 1회로 확인한다. 일시 장애는 상태를 바꾸지 않고 그대로 던진다."""
    factory = session_factory or clobe.ReadSession
    conn = await _connect()
    try:
        await _get_link(conn, company_id)
        if await _is_blocked(conn, company_id):
            raise CollectionError("company_blocked", 403)
        ok, error = True, None
        try:
            async with factory() as session:
                await session.call("get_scraping_status", {"companyId": company_id})
        except (clobe.ClobeReauthRequired, clobe.ClobeTransientError):
            raise
        except clobe.ClobeError as exc:
            ok, error = False, f"permission_denied:{str(exc)[:60]}"
        await conn.execute(
            "UPDATE obys_clobe_company_link SET permission_ok=$2, permission_error=$3, permission_checked_at=now() "
            "WHERE clobe_company_id=$1",
            company_id, ok, error,
        )
        return {"clobe_company_id": company_id, "permission_ok": ok, "error": error}
    finally:
        await conn.close()


async def require_company_tenant(company_id: str, tenant_id: Any) -> None:
    """연결된 회사의 테넌트가 호출자 테넌트일 때만 통과한다(미연결·차단 회사는 존재 자체를 숨긴다)."""
    conn = await _connect()
    try:
        link = await conn.fetchrow(
            "SELECT link_status, tenant_id FROM obys_clobe_company_link WHERE clobe_company_id=$1", company_id)
        if link is None or link["link_status"] != "linked" or str(link["tenant_id"]) != str(tenant_id):
            raise CollectionError("company_not_found", 404)
    finally:
        await conn.close()


async def require_runnable_company(company_id: str) -> None:
    """내부 관리자 경로용: 테넌트 대조는 없어도 미발견·미연결·차단·범위 밖 회사는 수집을 시작하지 못한다."""
    conn = await _connect()
    try:
        _scoped_link_or_raise(await _get_link(conn, company_id))
    finally:
        await conn.close()


async def _is_blocked(conn: Any, company_id: str) -> bool:
    row = await _get_link(conn, company_id)
    if row["link_status"] == "blocked":
        return True
    t, b = row.get("tenant_id"), row.get("business_id")
    return bool((t and str(t) in core.blocked_tenants()) or (b and b in core.blocked_businesses()))


async def approve_link(
    company_id: str, business_id: str, *, actor: str, name_evidence: bool = False,
) -> dict[str, Any]:
    conn = await _connect()
    try:
        async with conn.transaction():
            link = await conn.fetchrow(
                "SELECT * FROM obys_clobe_company_link WHERE clobe_company_id=$1 FOR UPDATE", company_id)
            if link is None:
                raise CollectionError("company_not_discovered", 404)
            if link["link_status"] == "blocked":
                raise CollectionError("company_blocked", 403)
            biz = await conn.fetchrow(
                "SELECT id, tenant_id, name, registration_no FROM yeoljeong_businesses WHERE id=$1 AND deleted_at IS NULL",
                business_id,
            )
            if biz is None:
                raise CollectionError("business_not_found", 404)
            ok, basis = core.check_admin_approval(
                {"name": link["company_name"], "reg_no": link["reg_no"]}, dict(biz), name_evidence=name_evidence)
            if not ok:
                raise CollectionError(basis, 409)
            taken = await conn.fetchval(
                "SELECT clobe_company_id FROM obys_clobe_company_link WHERE link_status='linked' "
                "AND tenant_id=$1 AND business_id=$2 AND clobe_company_id<>$3",
                biz["tenant_id"], business_id, company_id,
            )
            if taken:
                raise CollectionError("business_already_linked", 409)
            await conn.execute(
                "UPDATE obys_clobe_company_link SET link_status='linked', link_basis=$2, tenant_id=$3, business_id=$4, "
                " approved_by=$5, approved_at=now(), candidate_business_ids='{}' WHERE clobe_company_id=$1",
                company_id, basis, biz["tenant_id"], business_id, actor,
            )
            row = await conn.fetchrow("SELECT * FROM obys_clobe_company_link WHERE clobe_company_id=$1", company_id)
        return _link_view(row)
    finally:
        await conn.close()


async def block_company(company_id: str, *, actor: str) -> dict[str, Any]:
    conn = await _connect()
    try:
        await _get_link(conn, company_id)
        await conn.execute(
            "UPDATE obys_clobe_company_link SET link_status='blocked', link_basis='admin_blocked', tenant_id=NULL, "
            " business_id=NULL, approved_by=$2, approved_at=now() WHERE clobe_company_id=$1",
            company_id, actor,
        )
        return _link_view(await _get_link(conn, company_id))
    finally:
        await conn.close()


# ── 임대(lease) 와 펜싱 ──────────────────────────────────

async def acquire_lease(conn: Any, company_id: str, instance: str, seconds: int = LEASE_SECONDS) -> int:
    epoch = await conn.fetchval(
        "INSERT INTO obys_clobe_collection_lease (clobe_company_id, owner_instance, owner_epoch, lease_expires_at) "
        "VALUES ($1, $2, 1, now() + make_interval(secs => $3)) "
        "ON CONFLICT (clobe_company_id) DO UPDATE SET owner_instance = EXCLUDED.owner_instance, "
        " owner_epoch = obys_clobe_collection_lease.owner_epoch + 1, lease_expires_at = EXCLUDED.lease_expires_at "
        "WHERE obys_clobe_collection_lease.lease_expires_at < now() "
        " OR obys_clobe_collection_lease.owner_instance = EXCLUDED.owner_instance "
        "RETURNING owner_epoch",
        company_id, instance, float(seconds),
    )
    if epoch is None:
        raise CollectionError("collection_in_progress", 409)
    return int(epoch)


async def _fence(conn: Any, ctx: "RunCtx") -> None:
    """쓰기 트랜잭션 안에서 호출한다. 임대 행을 잠그므로 이후 다른 인스턴스의 인수는 이 트랜잭션 뒤에 일어난다."""
    row = await conn.fetchrow(
        "SELECT owner_instance, owner_epoch, lease_expires_at > now() AS live "
        "FROM obys_clobe_collection_lease WHERE clobe_company_id=$1 FOR UPDATE",
        ctx.company_id,
    )
    if row is None or row["owner_instance"] != ctx.instance or int(row["owner_epoch"]) != ctx.epoch or not row["live"]:
        raise LeaseLost()
    await conn.execute(
        "UPDATE obys_clobe_collection_lease SET lease_expires_at = now() + make_interval(secs => $2) "
        "WHERE clobe_company_id=$1",
        ctx.company_id, float(ctx.lease_seconds),
    )


async def _release_lease(conn: Any, ctx: "RunCtx") -> None:
    await conn.execute(
        "UPDATE obys_clobe_collection_lease SET lease_expires_at = now() - interval '1 second' "
        "WHERE clobe_company_id=$1 AND owner_instance=$2 AND owner_epoch=$3",
        ctx.company_id, ctx.instance, ctx.epoch,
    )


@dataclass
class RunCtx:
    company_id: str
    tenant_id: UUID
    business_id: str
    instance: str
    epoch: int
    lease_seconds: int
    mode: str
    kind: str = ""
    run_id: UUID | None = None
    chain_id: UUID | None = None


# ── 수집 ─────────────────────────────────────────────────

def _scoped_link_or_raise(link: Any) -> None:
    if link["link_status"] != "linked":
        raise CollectionError("company_not_linked", 409)
    if str(link["tenant_id"]) in core.blocked_tenants() or link["business_id"] in core.blocked_businesses():
        raise CollectionError("company_blocked", 403)
    if not core.tenant_in_scope(link["tenant_id"]):
        raise CollectionError("tenant_not_in_collection_scope", 403)


async def _find_resume(conn: Any, company_id: str, kind: str, start: date, end: date) -> Any:
    """같은 회사·종류·기간의 가장 최근 live 실행이 성공이 아니면 그 체크포인트를 이어받는다."""
    row = await conn.fetchrow(
        "SELECT run_id, chain_id, status, resume_state, attempt FROM obys_clobe_collection_run "
        "WHERE clobe_company_id=$1 AND data_kind=$2 AND period_start=$3 AND period_end=$4 AND mode='live' "
        "ORDER BY started_at DESC LIMIT 1",
        company_id, kind, start, end,
    )
    if row is not None and row["status"] in ("partial", "failed", "lease_lost", "running"):
        return row
    return None


async def _upsert_item(conn: Any, ctx: RunCtx, row: dict[str, Any], counts: dict[str, int]) -> None:
    kind, key, digest = ctx.kind, row["source_key"], row["hash"]
    prior = await conn.fetch(
        "SELECT item_id, item_hash, stage FROM obys_clobe_item "
        "WHERE tenant_id=$1 AND business_id=$2 AND data_kind=$3 AND source_key=$4 ORDER BY first_seen_at DESC",
        ctx.tenant_id, ctx.business_id, kind, key,
    )
    same = next((p for p in prior if p["item_hash"] == digest), None)
    if same is not None:
        await conn.execute(
            "UPDATE obys_clobe_item SET last_run_id=$2, last_seen_at=now() WHERE item_id=$1", same["item_id"], ctx.run_id)
        counts["unchanged"] += 1
        return
    latest = prior[0] if prior else None
    await conn.execute(
        "INSERT INTO obys_clobe_item (item_id, tenant_id, business_id, clobe_company_id, data_kind, source_key, item_hash, "
        " institution, occurred_on, amount, direction, counterparty, source_as_of, payload, supersedes_item_id, "
        " first_run_id, last_run_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15,$16,$16)",
        uuid.uuid4(), ctx.tenant_id, ctx.business_id, ctx.company_id, kind, key, digest, row["institution"],
        row["occurred_on"], row["amount"], row["direction"], row["counterparty"], row.get("source_as_of"),
        core.canonical_json(row["raw"]), latest["item_id"] if latest else None, ctx.run_id,
    )
    if latest is not None:
        counts["revised"] += 1
        if latest["stage"] in ("review_box", "reviewed"):
            await conn.execute("UPDATE obys_clobe_item SET stage='superseded' WHERE item_id=$1", latest["item_id"])
    else:
        counts["inserted"] += 1


async def _shadow_probe(conn: Any, ctx: RunCtx, row: dict[str, Any], counts: dict[str, int]) -> None:
    exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM obys_clobe_item WHERE tenant_id=$1 AND business_id=$2 AND data_kind=$3 "
        " AND source_key=$4 AND item_hash=$5)",
        ctx.tenant_id, ctx.business_id, ctx.kind, row["source_key"], row["hash"],
    )
    counts["unchanged" if exists else "would_insert"] += 1


def _prepare_rows(kind: str, items: list[Any], source_as_of: datetime | None) -> list[dict[str, Any]]:
    rows = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        norm = core.normalize_item(kind, raw)
        norm["raw"] = raw
        rows.append(norm)
    core.assign_source_keys(kind, rows)
    for r in rows:
        r["hash"] = core.item_hash(kind, r["raw"])
        r["source_as_of"] = source_as_of
    return rows


async def _write_page(
    conn: Any, ctx: RunCtx, rows: list[dict[str, Any]], resume_state: dict[str, Any],
    counts: dict[str, int], seen: set[tuple[str, str]],
) -> None:
    fresh = []
    for r in rows:
        ident = (r["source_key"], r["hash"])
        if ident in seen:
            counts["dup_in_run"] += 1
            continue
        seen.add(ident)
        fresh.append(r)
    async with conn.transaction():
        await _fence(conn, ctx)
        for r in fresh:
            if ctx.mode == "live":
                await _upsert_item(conn, ctx, r, counts)
            else:
                await _shadow_probe(conn, ctx, r, counts)
        await conn.execute(
            "UPDATE obys_clobe_collection_run SET resume_state=$2::jsonb, counts=$3::jsonb WHERE run_id=$1",
            ctx.run_id, _json(resume_state), _json(counts),
        )


async def _stream_totals(
    conn: Any, ctx: RunCtx, direction: str | None, shadow: dict[str, dict[str, Any]],
) -> tuple[int, dict[str, Decimal | None]]:
    keys = core.sum_item_keys(ctx.kind)
    if ctx.mode == "shadow":
        rows = [r for r in shadow.values() if not direction or r["direction"] == direction]
        sums: dict[str, Decimal | None] = {}
        for k in keys:
            vals = [r["raw"].get(k) for r in rows]
            sign = [-1 if core.is_cancellation(ctx.kind, r["raw"]) else 1 for r in rows]
            sums[k] = None if any(v is None for v in vals) else sum(
                (sg * (core._dec(v) or Decimal("0")) for sg, v in zip(sign, vals)), Decimal("0"))
        return len(rows), sums
    where = ("tenant_id=$1 AND business_id=$2 AND data_kind=$3 AND stage <> 'superseded' "
             "AND last_run_id IN (SELECT run_id FROM obys_clobe_collection_run WHERE chain_id=$4)")
    args: list[Any] = [ctx.tenant_id, ctx.business_id, ctx.kind, ctx.chain_id]
    if direction:
        where += " AND direction=$5"
        args.append(direction)
    count = await conn.fetchval(f"SELECT count(DISTINCT source_key) FROM obys_clobe_item WHERE {where}", *args)
    sign_sql = core.CANCEL_SIGN_SQL.get(ctx.kind, "1")
    sums = {}
    for k in keys:
        row = await conn.fetchrow(
            f"SELECT count(*) AS n, count(*) FILTER (WHERE payload ? '{k}' AND payload->>'{k}' IS NOT NULL) AS have, "
            f"sum((payload->>'{k}')::numeric * {sign_sql}) AS total FROM (SELECT DISTINCT ON (source_key) payload FROM obys_clobe_item "
            f"WHERE {where} ORDER BY source_key, last_seen_at DESC) t",
            *args,
        )
        sums[k] = None if row["n"] != row["have"] else (Decimal(row["total"]) if row["total"] is not None else Decimal("0"))
    return int(count or 0), sums


def _page_args(kind: str, company_id: str, start: date, end: date, stream: dict[str, str], page: int, cursor: str | None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "companyId": company_id, "startDate": start.isoformat(), "endDate": end.isoformat(), "size": core.PAGE_SIZE,
    }
    args.update(stream)
    if kind == core.KIND_BANK:
        if cursor:
            args["cursor"] = cursor
    else:
        args["page"] = page
        if kind == core.KIND_CARD:
            args["sort"] = "usedAt,desc"
    return args


async def _collect_kind(
    conn: Any, session: Any, ctx: RunCtx, start: date, end: date, scrape: dict[str, Any], resume_state: dict[str, Any],
    counts: dict[str, int],
) -> dict[str, Any]:
    """한 종류를 끝까지(또는 중단 지점까지) 읽는다. 중단은 예외로 올라가고 체크포인트는 이미 저장돼 있다."""
    tool = clobe.COLLECTION_DATA_TOOLS[ctx.kind]
    streams = core.KIND_STREAMS[ctx.kind]
    state = dict(resume_state) if resume_state else {}
    state.setdefault("stream", 0)
    state.setdefault("page", 0)
    state.setdefault("cursor", None)
    state.setdefault("meta", {})
    state.setdefault("done", [])
    seen: set[tuple[str, str]] = set()
    shadow: dict[str, dict[str, Any]] = {}
    reconcile_out: list[dict[str, Any]] = []
    while state["stream"] < len(streams):
        stream = streams[state["stream"]]
        direction = stream.get("type")
        pages = 0
        while True:
            if pages >= core.MAX_PAGES:
                raise CollectionError("page_limit", 409)
            result = await session.call(tool, _page_args(ctx.kind, ctx.company_id, start, end, stream, state["page"], state["cursor"]))
            items = result.get("content")
            if not isinstance(items, list):
                raise clobe.ClobeError("tool_unexpected_shape")
            if not state["meta"]:
                state["meta"] = {k: v for k, v in result.items() if k not in ("content", "nextCursor") and not isinstance(v, (list, dict))}
            rows = _prepare_rows(ctx.kind, items, scrape.get("source_as_of"))
            if ctx.mode == "shadow":
                for r in rows:
                    shadow[f"{r['source_key']}|{r['hash']}"] = r
            has_next = bool(result.get("hasNext"))
            nxt = result.get("nextCursor")
            if ctx.kind == core.KIND_BANK:
                state["cursor"] = nxt if has_next and nxt else None
                if has_next and not nxt:
                    raise clobe.ClobeError("cursor_missing")
            else:
                state["page"] += 1
            counts["pages"] += 1
            counts["received"] += len(rows)
            pages += 1
            await _write_page(conn, ctx, rows, state, counts, seen)
            if not has_next:
                break
        count, sums = await _stream_totals(conn, ctx, direction, shadow)
        rec = core.reconcile(ctx.kind, count, sums, state["meta"])
        rec["stream"] = direction or "all"
        reconcile_out.append(rec)
        state["done"].append(rec)
        state.update({"stream": state["stream"] + 1, "page": 0, "cursor": None, "meta": {}})
        await _checkpoint(conn, ctx, state)
    return {"reconcile": reconcile_out, "state": state}


async def _checkpoint(conn: Any, ctx: RunCtx, state: dict[str, Any]) -> None:
    async with conn.transaction():
        await _fence(conn, ctx)
        await conn.execute("UPDATE obys_clobe_collection_run SET resume_state=$2::jsonb WHERE run_id=$1", ctx.run_id, _json(state))


async def _finish_run(
    conn: Any, ctx: RunCtx, status: str, *, error_code: str | None, counts: dict[str, Any],
    source_as_of: datetime | None, start: date, end: date, resume_state: dict[str, Any] | None,
) -> None:
    """종료 기록. 임대를 잃었으면 상태 갱신은 하지 않고 실행 행만 lease_lost 로 닫는다."""
    try:
        async with conn.transaction():
            await _fence(conn, ctx)
            await conn.execute(
                "UPDATE obys_clobe_collection_run SET status=$2, error_code=$3, counts=$4::jsonb, source_as_of=$5, "
                " resume_state=COALESCE($6::jsonb, resume_state), finished_at=now() WHERE run_id=$1",
                ctx.run_id, status, error_code, _json(counts), source_as_of,
                None if resume_state is None else _json(resume_state),
            )
            if ctx.mode == "live":
                await _update_state(conn, ctx, status, error_code, source_as_of, start, end)
    except LeaseLost:
        await conn.execute(
            "UPDATE obys_clobe_collection_run SET status='lease_lost', error_code='lease_lost', finished_at=now() "
            "WHERE run_id=$1 AND status='running'", ctx.run_id)
        raise


async def _update_state(conn: Any, ctx: RunCtx, status: str, error_code: str | None, source_as_of: datetime | None,
                        start: date, end: date) -> None:
    prev = await conn.fetchrow(
        "SELECT consecutive_failures FROM obys_clobe_collection_state WHERE clobe_company_id=$1 AND data_kind=$2",
        ctx.company_id, ctx.kind)
    failures = int(prev["consecutive_failures"]) if prev else 0
    if status == "succeeded":
        await conn.execute(
            "INSERT INTO obys_clobe_collection_state (clobe_company_id, data_kind, status, last_run_id, last_success_at, "
            " last_success_run_id, consecutive_failures, covered_from, covered_to, source_as_of, last_error_code) "
            "VALUES ($1,$2,'succeeded',$3,now(),$3,0,$4,$5,$6,NULL) "
            "ON CONFLICT (clobe_company_id, data_kind) DO UPDATE SET status='succeeded', last_run_id=$3, last_success_at=now(), "
            " last_success_run_id=$3, consecutive_failures=0, next_retry_at=NULL, covered_from=$4, covered_to=$5, "
            " source_as_of=$6, last_error_code=NULL, updated_at=now()",
            ctx.company_id, ctx.kind, ctx.run_id, start, end, source_as_of,
        )
        return
    failures += 1
    retry = None if error_code == "reauth_required" else datetime.now(timezone.utc) + timedelta(seconds=core.retry_delay_seconds(failures))
    state = "needs_reauth" if error_code == "reauth_required" else status
    await conn.execute(
        "INSERT INTO obys_clobe_collection_state (clobe_company_id, data_kind, status, last_run_id, last_failure_at, "
        " last_error_code, consecutive_failures, next_retry_at) VALUES ($1,$2,$3,$4,now(),$5,$6,$7) "
        "ON CONFLICT (clobe_company_id, data_kind) DO UPDATE SET status=$3, last_run_id=$4, last_failure_at=now(), "
        " last_error_code=$5, consecutive_failures=$6, next_retry_at=$7, updated_at=now()",
        ctx.company_id, ctx.kind, state, ctx.run_id, error_code, failures, retry,
    )


async def run_collection(
    company_id: str, *, kinds: list[str] | None = None, period_start: Any = None, period_end: Any = None,
    mode: str = "live", instance: str = DEFAULT_INSTANCE, session_factory: Callable[[], Any] | None = None,
    lease_seconds: int = LEASE_SECONDS,
) -> dict[str, Any]:
    if mode not in ("live", "shadow"):
        raise CollectionError("invalid_mode", 400)
    wanted = list(kinds or core.DATA_KINDS)
    if any(k not in core.DATA_KINDS for k in wanted):
        raise CollectionError("invalid_kind", 400)
    try:
        start, end = core.resolve_period(period_start, period_end)
    except ValueError as exc:
        raise CollectionError(str(exc), 400) from None

    conn = await _connect()
    factory = session_factory or clobe.ReadSession
    results: list[dict[str, Any]] = []
    epoch: int | None = None
    try:
        link = await _get_link(conn, company_id)
        _scoped_link_or_raise(link)
        epoch = await acquire_lease(conn, company_id, instance, lease_seconds)
        base = RunCtx(company_id, link["tenant_id"], link["business_id"], instance, epoch, lease_seconds, mode)
        try:
            async with factory() as session:
                scrape_payload = await _preflight(conn, session, base)
                for kind in wanted:
                    results.append(await _run_kind(conn, session, base, kind, start, end, scrape_payload))
        except clobe.ClobeReauthRequired:
            return {"company_id": company_id, "status": "needs_reauth", "runs": results, "error_code": "reauth_required"}
        except clobe.ClobeTransientError as exc:
            return {"company_id": company_id, "status": "partial", "runs": results,
                    "error_code": f"transient:{str(exc)[:40]}"}
        except LeaseLost:
            return {"company_id": company_id, "status": "lease_lost", "runs": results, "error_code": "lease_lost"}
        statuses = {r["status"] for r in results}
        overall = "succeeded" if statuses == {"succeeded"} else ("failed" if statuses <= {"failed"} else "partial")
        return {"company_id": company_id, "mode": mode, "status": overall, "runs": results,
                "period": [start.isoformat(), end.isoformat()]}
    finally:
        if epoch is not None:
            try:
                await _release_lease(conn, RunCtx(company_id, link["tenant_id"], link["business_id"], instance, epoch, lease_seconds, mode))
            except Exception:  # noqa: BLE001 - 해제 실패는 임대 만료가 대신한다
                logger.warning("clobe_collection_lease_release_failed")
        await conn.close()


async def _preflight(conn: Any, session: Any, ctx: RunCtx) -> dict[str, Any]:
    """권한 확인과 최신성 조회를 한 번에. 접근이 막히면 데이터 도구를 부르기 전에 중단한다."""
    try:
        payload = await session.call("get_scraping_status", {"companyId": ctx.company_id})
    except (clobe.ClobeReauthRequired, clobe.ClobeTransientError):
        raise
    except clobe.ClobeError as exc:
        await conn.execute(
            "UPDATE obys_clobe_company_link SET permission_ok=FALSE, permission_error=$2, permission_checked_at=now() "
            "WHERE clobe_company_id=$1", ctx.company_id, f"permission_denied:{str(exc)[:60]}")
        raise CollectionError("permission_denied", 403) from None
    await conn.execute(
        "UPDATE obys_clobe_company_link SET permission_ok=TRUE, permission_error=NULL, permission_checked_at=now() "
        "WHERE clobe_company_id=$1", ctx.company_id)
    return payload


async def _run_kind(conn: Any, session: Any, base: RunCtx, kind: str, start: date, end: date,
                    scrape_payload: dict[str, Any]) -> dict[str, Any]:
    ctx = RunCtx(**{**base.__dict__, "kind": kind})
    scrape = core.scrape_summary(scrape_payload, kind)
    prev = await _find_resume(conn, ctx.company_id, kind, start, end) if ctx.mode == "live" else None
    resume_state = _load(prev["resume_state"]) if prev else {}
    ctx.run_id = uuid.uuid4()
    resumed = bool(prev and resume_state)
    ctx.chain_id = prev["chain_id"] if resumed else ctx.run_id
    attempt = int(prev["attempt"]) + 1 if prev else 1
    async with conn.transaction():
        await _fence(conn, ctx)
        await conn.execute(
            "INSERT INTO obys_clobe_collection_run (run_id, clobe_company_id, tenant_id, business_id, data_kind, mode, "
            " period_start, period_end, status, owner_instance, owner_epoch, attempt, chain_id, resume_of, resume_state, source_as_of) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'running',$9,$10,$11,$12,$13,$14::jsonb,$15)",
            ctx.run_id, ctx.company_id, ctx.tenant_id, ctx.business_id, kind, ctx.mode, start, end, ctx.instance,
            ctx.epoch, attempt, ctx.chain_id, prev["run_id"] if prev else None, _json(resume_state), scrape["source_as_of"],
        )
    counts: dict[str, Any] = {"pages": 0, "received": 0, "inserted": 0, "unchanged": 0, "revised": 0,
                              "dup_in_run": 0, "would_insert": 0}
    status, error_code, final_state, reconcile_out = "succeeded", None, None, []
    try:
        out = await _collect_kind(conn, session, ctx, start, end, scrape, resume_state, counts)
        final_state, reconcile_out = out["state"], out["reconcile"]
        bad = [m for r in reconcile_out for m in r["mismatches"]]
        if bad:
            # 누락·합계 불일치는 성공이 아니다. 다음 시도는 처음부터 다시 읽는다(적재는 멱등).
            status, error_code, final_state = "partial", bad[0], {}
        elif scrape["asset_error"]:
            # 클로브 쪽 수집(스크래핑)이 일부 실패한 상태다 — 우리가 읽은 값은 맞아도 최신이라고 보증하지 못한다.
            status, error_code, final_state = "partial", "source_scrape_error", {}
    except clobe.ClobeReauthRequired:
        status, error_code = "failed", "reauth_required"
    except clobe.ClobeTransientError as exc:
        status, error_code = "partial", f"transient:{str(exc)[:40]}"
    except CollectionError as exc:
        if isinstance(exc, LeaseLost):
            await conn.execute(
                "UPDATE obys_clobe_collection_run SET status='lease_lost', error_code='lease_lost', finished_at=now() "
                "WHERE run_id=$1 AND status='running'", ctx.run_id)
            raise
        status, error_code = "partial", exc.code
    except clobe.ClobeError as exc:
        status, error_code = "failed", f"clobe:{str(exc)[:40]}"
    counts["reconcile"] = reconcile_out
    counts["scrape"] = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in scrape.items()}
    await _finish_run(conn, ctx, status, error_code=error_code, counts=counts, source_as_of=scrape["source_as_of"],
                      start=start, end=end, resume_state=final_state)
    if error_code == "reauth_required":
        raise clobe.ClobeReauthRequired("reauth_required")
    return {"run_id": str(ctx.run_id), "data_kind": kind, "status": status, "error_code": error_code,
            "attempt": attempt, "resumed": resumed, "counts": {k: v for k, v in counts.items() if k not in ("reconcile", "scrape")},
            "reconcile": reconcile_out, "source_as_of": scrape["source_as_of"].isoformat() if scrape["source_as_of"] else None}


# ── 검토함 → 회계 검토 → 원장 확정 ───────────────────────

def _scope(tenant_id: Any) -> UUID:
    if not core.tenant_in_scope(tenant_id):
        raise CollectionError("tenant_not_in_collection_scope", 403)
    return UUID(str(tenant_id))


async def _require_business(conn: Any, tenant: UUID, business_id: str) -> None:
    owns = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM yeoljeong_businesses WHERE id=$1 AND tenant_id=$2 AND deleted_at IS NULL)",
        business_id, tenant)
    if not owns or business_id in core.blocked_businesses():
        raise CollectionError("business_not_found", 404)


async def list_items(tenant_id: Any, business_id: str, *, stage: str | None = None, kind: str | None = None,
                     limit: int = 100, offset: int = 0) -> dict[str, Any]:
    tenant = _scope(tenant_id)
    limit = max(1, min(int(limit), 500))
    conn = await _connect()
    try:
        await _require_business(conn, tenant, business_id)
        rows = await conn.fetch(
            "SELECT item_id, data_kind, source_key, institution, occurred_on, amount, direction, counterparty, stage, "
            " source_as_of, first_seen_at, supersedes_item_id FROM obys_clobe_item "
            "WHERE tenant_id=$1 AND business_id=$2 AND ($3::text IS NULL OR stage=$3) AND ($4::text IS NULL OR data_kind=$4) "
            "ORDER BY occurred_on DESC NULLS LAST, item_id LIMIT $5 OFFSET $6",
            tenant, business_id, stage, kind, limit, max(0, int(offset)))
        total = await conn.fetchval(
            "SELECT count(*) FROM obys_clobe_item WHERE tenant_id=$1 AND business_id=$2 "
            "AND ($3::text IS NULL OR stage=$3) AND ($4::text IS NULL OR data_kind=$4)", tenant, business_id, stage, kind)
        items = [{
            "item_id": str(r["item_id"]), "data_kind": r["data_kind"], "source_key": r["source_key"],
            "institution": r["institution"], "occurred_on": r["occurred_on"].isoformat() if r["occurred_on"] else None,
            "amount": str(r["amount"]) if r["amount"] is not None else None, "direction": r["direction"],
            "counterparty": r["counterparty"], "stage": r["stage"],
            "source_as_of": r["source_as_of"].isoformat() if r["source_as_of"] else None,
            "is_revision": r["supersedes_item_id"] is not None,
        } for r in rows]
        return {"total": int(total or 0), "items": items}
    finally:
        await conn.close()


def _parse_item_ids(item_ids: list[str]) -> list[UUID]:
    try:
        return [UUID(str(i)) for i in item_ids]
    except ValueError:
        raise CollectionError("invalid_item_id", 400) from None


async def review_items(tenant_id: Any, business_id: str, item_ids: list[str], decision: str, *, actor: str) -> dict[str, Any]:
    """검토함 → 회계 검토 결과(reviewed | rejected). 원장은 건드리지 않는다."""
    tenant = _scope(tenant_id)
    if decision not in ("approve", "reject"):
        raise CollectionError("invalid_decision", 400)
    target = "reviewed" if decision == "approve" else "rejected"
    allowed_from = ("review_box",) if decision == "approve" else ("review_box", "reviewed")
    ids = _parse_item_ids(item_ids)
    conn = await _connect()
    try:
        await _require_business(conn, tenant, business_id)
        done = await conn.fetch(
            "UPDATE obys_clobe_item SET stage=$5, reviewed_by=$6, reviewed_at=now() "
            "WHERE tenant_id=$1 AND business_id=$2 AND item_id = ANY($3::uuid[]) AND stage = ANY($4::text[]) RETURNING item_id",
            tenant, business_id, ids, list(allowed_from), target, actor)
        return {"updated": len(done), "skipped": len(ids) - len(done), "stage": target}
    finally:
        await conn.close()


async def confirm_items(tenant_id: Any, business_id: str, item_ids: list[str], *, actor: str) -> dict[str, Any]:
    """회계 검토를 마친(reviewed) 항목만 확정 원장에 올린다. 같은 항목을 다시 확정해도 행이 늘지 않는다."""
    tenant = _scope(tenant_id)
    ids = _parse_item_ids(item_ids)
    conn = await _connect()
    try:
        await _require_business(conn, tenant, business_id)
        confirmed = skipped = 0
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT * FROM obys_clobe_item WHERE tenant_id=$1 AND business_id=$2 AND item_id = ANY($3::uuid[]) FOR UPDATE",
                tenant, business_id, ids)
            for r in rows:
                if r["stage"] != "reviewed":
                    skipped += 1
                    continue
                # 같은 원장 키를 동시에 확정하는 두 트랜잭션이 둘 다 "항목 없음"을 보지 못하게 키 단위로 직렬화한다.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"{tenant}|{business_id}|{r['data_kind']}|{r['source_key']}")
                entry = await conn.fetchrow(
                    "SELECT entry_id, item_id, item_hash FROM obys_clobe_ledger_entry "
                    "WHERE tenant_id=$1 AND business_id=$2 AND data_kind=$3 AND source_key=$4 FOR UPDATE",
                    tenant, business_id, r["data_kind"], r["source_key"])
                if entry is None:
                    await conn.execute(
                        "INSERT INTO obys_clobe_ledger_entry (entry_id, tenant_id, business_id, data_kind, source_key, item_id, "
                        " item_hash, occurred_on, amount, direction, confirmed_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
                        "ON CONFLICT (tenant_id, business_id, data_kind, source_key) DO NOTHING",
                        uuid.uuid4(), tenant, business_id, r["data_kind"], r["source_key"], r["item_id"], r["item_hash"],
                        r["occurred_on"], r["amount"], r["direction"], actor)
                elif entry["item_hash"] != r["item_hash"]:
                    await conn.execute(
                        "UPDATE obys_clobe_item SET stage='superseded' WHERE item_id=$1 AND stage='confirmed'", entry["item_id"])
                    await conn.execute(
                        "UPDATE obys_clobe_ledger_entry SET item_id=$2, item_hash=$3, occurred_on=$4, amount=$5, direction=$6, "
                        " revision=revision+1, confirmed_by=$7, confirmed_at=now() WHERE entry_id=$1",
                        entry["entry_id"], r["item_id"], r["item_hash"], r["occurred_on"], r["amount"], r["direction"], actor)
                await conn.execute(
                    "UPDATE obys_clobe_item SET stage='confirmed', confirmed_by=$2, confirmed_at=now() WHERE item_id=$1",
                    r["item_id"], actor)
                confirmed += 1
            skipped += len(ids) - len(rows)
        return {"confirmed": confirmed, "skipped": skipped}
    finally:
        await conn.close()


# ── 상태 계약(UI) ────────────────────────────────────────

def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


async def company_status(tenant_id: Any, *, internal_admin: bool = False) -> dict[str, Any]:
    """회사별 수집 상태. 일반 사용자는 자기 테넌트에 연결된 회사만 본다(미연결·차단 회사는 관리자 전용)."""
    tenant = _scope(tenant_id) if not internal_admin else None
    conn = await _connect()
    try:
        if internal_admin:
            links = await conn.fetch("SELECT * FROM obys_clobe_company_link ORDER BY company_name, clobe_company_id")
        else:
            links = await conn.fetch(
                "SELECT * FROM obys_clobe_company_link WHERE link_status='linked' AND tenant_id=$1 "
                "ORDER BY company_name, clobe_company_id", tenant)
        companies = []
        for link in links:
            cid = link["clobe_company_id"]
            states = await conn.fetch("SELECT * FROM obys_clobe_collection_state WHERE clobe_company_id=$1", cid)
            by_kind = {s["data_kind"]: s for s in states}
            stage_counts: dict[str, int] = {}
            if link["link_status"] == "linked":
                for r in await conn.fetch(
                    "SELECT stage, count(*) AS n FROM obys_clobe_item WHERE tenant_id=$1 AND business_id=$2 GROUP BY stage",
                    link["tenant_id"], link["business_id"]):
                    stage_counts[r["stage"]] = int(r["n"])
            kinds = {}
            for kind in core.DATA_KINDS:
                s = by_kind.get(kind)
                kinds[kind] = {
                    "status": s["status"] if s else "never_run",
                    "last_success_at": _iso(s["last_success_at"]) if s else None,
                    "last_failure_at": _iso(s["last_failure_at"]) if s else None,
                    "last_error_code": s["last_error_code"] if s else None,
                    "consecutive_failures": int(s["consecutive_failures"]) if s else 0,
                    "next_retry_at": _iso(s["next_retry_at"]) if s else None,
                    "covered_from": _iso(s["covered_from"]) if s else None,
                    "covered_to": _iso(s["covered_to"]) if s else None,
                    "source_as_of": _iso(s["source_as_of"]) if s else None,
                }
            companies.append({**_link_view(link), "kinds": kinds, "item_stage_counts": stage_counts})
        return {"companies": companies, "unsupported_sources": list(core.UNSUPPORTED_SOURCES)}
    finally:
        await conn.close()


async def list_runs(tenant_id: Any, company_id: str, *, limit: int = 20, internal_admin: bool = False) -> list[dict[str, Any]]:
    conn = await _connect()
    try:
        link = await _get_link(conn, company_id)
        if not internal_admin:
            tenant = _scope(tenant_id)
            if link["link_status"] != "linked" or link["tenant_id"] != tenant:
                raise CollectionError("company_not_found", 404)
        rows = await conn.fetch(
            "SELECT run_id, data_kind, mode, period_start, period_end, status, attempt, error_code, counts, source_as_of, "
            " started_at, finished_at FROM obys_clobe_collection_run WHERE clobe_company_id=$1 "
            "ORDER BY started_at DESC LIMIT $2", company_id, max(1, min(int(limit), 100)))
        return [{
            "run_id": str(r["run_id"]), "data_kind": r["data_kind"], "mode": r["mode"],
            "period": [r["period_start"].isoformat(), r["period_end"].isoformat()], "status": r["status"],
            "attempt": r["attempt"], "error_code": r["error_code"],
            "counts": {k: v for k, v in _load(r["counts"]).items() if k not in ("scrape",)},
            "source_as_of": _iso(r["source_as_of"]), "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"]),
        } for r in rows]
    finally:
        await conn.close()
