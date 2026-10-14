"""오비서 '오늘 한눈에'·배달앱 현황 읽기 전용 집계.

이미 DB 에 있는 자료(배달앱 자체수집 yeoljeong_delivery_*, 클로브 수집 obys_clobe_item)를 테넌트 범위 안에서
합산만 한다. 쓰기·확정·전표와 무관하다. 금액·일자는 payload(JSONB) 텍스트에서 SQL 로 안전하게 읽어
깨진 행 하나가 전체 조회를 죽이지 않게 한다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.obys_db import obys_db_url
from app.services import obys_collection_core as core
from app.services.obys_collection_service import CollectionError

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
DEFAULT_DAYS = 30
MAX_SPAN_DAYS = 366
SETTLEMENT_LOOKAHEAD_DAYS = 14
STALE_DAYS = 3
MAX_BUSINESSES = 50

SERVICE_LABELS = {
    "baemin": "배달의민족",
    "coupangeats": "쿠팡이츠",
    "yogiyo": "요기요",
    "ddangyo": "땡겨요",
    "matepos": "메이트포스",
    "card_pg": "카드·PG",
}
CHANNEL_LABELS = {"DELIVERY": "배달앱", "CARD": "카드", "PG": "PG(간편결제)", "MKP": "오픈마켓", "UNKNOWN": "기타 채널"}
# 쿠팡이츠는 사장님 사이트 로그인이 막히는 일이 잦아 정산 CSV 를 직접 올리는 경로가 따로 있다.
CSV_UPLOAD_SERVICES = {"coupangeats"}
BROKEN_STATUSES = frozenset({"failed", "action_required", "error", "stale"})
_PAID_WORDS = ("완료", "입금", "지급", "paid", "done", "complete", "settled")
_SCHEDULED_WORDS = ("예정", "대기", "미", "pending", "scheduled", "wait")

_DAY_OK = "left(payload->>'occurred_on', 10) ~ '^20[0-9]{2}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$'"
_DAY = "left(payload->>'occurred_on', 10)"


def _num(key: str) -> str:
    text = f"(payload->>'{key}')"
    return f"(CASE WHEN {text} ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN {text}::numeric ELSE 0 END)"


def service_label(service: Any) -> str:
    key = str(service or "").strip().lower()
    if key in SERVICE_LABELS:
        return SERVICE_LABELS[key]
    return str(service) if any("가" <= ch <= "힣" for ch in str(service or "")) else "기타 배달앱"


def settlement_bucket(status: Any) -> str:
    text = str(status or "").strip().lower()
    if not text:
        return "unknown"
    if any(word in text for word in _SCHEDULED_WORDS):
        return "scheduled"
    if any(word in text for word in _PAID_WORDS):
        return "paid"
    return "unknown"


def _today() -> date:
    return datetime.now(KST).date()


def resolve_period(date_from: Any, date_to: Any) -> tuple[date, date]:
    end = date_to or _today()
    start = date_from or (end - timedelta(days=DEFAULT_DAYS - 1))
    if start > end or (end - start).days >= MAX_SPAN_DAYS:
        raise CollectionError("invalid_period", 400)
    return start, end


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _money(value: Any) -> str:
    return str(_dec(value).quantize(Decimal("1")))


def _stale(last_day: str | None, today: date) -> tuple[bool, int | None]:
    if not last_day:
        return True, None
    try:
        age = (today - date.fromisoformat(last_day)).days
    except ValueError:
        return True, None
    return age > STALE_DAYS, max(age, 0)


async def _connect():
    import asyncpg

    return await asyncpg.connect(obys_db_url(), timeout=5)


async def _tenant_businesses(conn: Any, tenant_id: Any, business_id: str | None) -> list[dict[str, str]]:
    try:
        tenant = UUID(str(tenant_id))
    except ValueError:
        raise CollectionError("tenant_required", 403) from None
    if not core.tenant_in_scope(tenant):
        raise CollectionError("tenant_not_in_collection_scope", 403)
    rows = await conn.fetch(
        "SELECT id, name FROM yeoljeong_businesses WHERE tenant_id=$1 AND deleted_at IS NULL "
        "AND ($2::text IS NULL OR id=$2) ORDER BY name, id LIMIT $3", tenant, business_id or None, MAX_BUSINESSES)
    blocked = core.blocked_businesses()
    found = [{"business_id": r["id"], "name": r["name"] or ""} for r in rows if r["id"] not in blocked]
    if business_id and not found:
        raise CollectionError("business_not_found", 404)
    return found


async def _sales_rows(conn: Any, ids: list[str], start: date, end: date) -> list[Any]:
    return await conn.fetch(
        f"SELECT business_id, branch, payload->>'service' AS service, {_DAY} AS day, count(*) AS orders, "
        f" sum({_num('gross_amount')}) AS gross, sum({_num('discount_amount')}) AS discount "
        f"FROM yeoljeong_delivery_sales WHERE business_id = ANY($1::text[]) AND deleted_at IS NULL AND {_DAY_OK} "
        f" AND {_DAY} BETWEEN $2 AND $3 GROUP BY 1, 2, 3, 4",
        ids, start.isoformat(), end.isoformat())


async def _settlement_rows(conn: Any, ids: list[str], start: date, end: date) -> list[Any]:
    return await conn.fetch(
        f"SELECT business_id, branch, payload->>'service' AS service, payload->>'settlement_status' AS status, "
        f" count(*) AS n, sum({_num('sales_amount')}) AS sales, sum({_num('fee_amount')}) AS fee, "
        f" sum({_num('settlement_amount')}) AS payout "
        f"FROM yeoljeong_delivery_settlements WHERE business_id = ANY($1::text[]) AND deleted_at IS NULL AND {_DAY_OK} "
        f" AND {_DAY} BETWEEN $2 AND $3 GROUP BY 1, 2, 3, 4",
        ids, start.isoformat(), end.isoformat())


async def _review_rows(conn: Any, ids: list[str], start: date, end: date) -> list[Any]:
    return await conn.fetch(
        f"SELECT business_id, payload->>'service' AS service, count(*) AS n, "
        f" avg(NULLIF({_num('rating')}, 0)) AS rating, "
        f" count(*) FILTER (WHERE COALESCE(payload->>'reply_status', '') = '' "
        f"   OR payload->>'reply_status' ~* '(미|대기|없|wait|pending|none)') AS waiting "
        f"FROM yeoljeong_delivery_reviews WHERE business_id = ANY($1::text[]) AND deleted_at IS NULL AND {_DAY_OK} "
        f" AND {_DAY} BETWEEN $2 AND $3 GROUP BY 1, 2",
        ids, start.isoformat(), end.isoformat())


async def _ad_rows(conn: Any, ids: list[str], start: date, end: date) -> list[Any]:
    return await conn.fetch(
        f"SELECT business_id, payload->>'service' AS service, count(*) AS n, sum({_num('cost_amount')}) AS cost, "
        f" sum({_num('orders')}) AS orders, sum({_num('sales_amount')}) AS sales "
        f"FROM yeoljeong_delivery_ads WHERE business_id = ANY($1::text[]) AND deleted_at IS NULL AND {_DAY_OK} "
        f" AND {_DAY} BETWEEN $2 AND $3 GROUP BY 1, 2",
        ids, start.isoformat(), end.isoformat())


async def _freshness_rows(conn: Any, ids: list[str]) -> list[Any]:
    parts = []
    for kind, table in (("sales", "yeoljeong_delivery_sales"), ("settlements", "yeoljeong_delivery_settlements"),
                        ("reviews", "yeoljeong_delivery_reviews"), ("ads", "yeoljeong_delivery_ads")):
        parts.append(
            f"SELECT '{kind}' AS kind, business_id, payload->>'service' AS service, count(*) AS n, "
            f" max({_DAY}) FILTER (WHERE {_DAY_OK}) AS last_day, max(payload->>'collected_at') AS last_collected "
            f"FROM {table} WHERE business_id = ANY($1::text[]) AND deleted_at IS NULL GROUP BY 2, 3")
    return await conn.fetch(" UNION ALL ".join(parts), ids)


async def _collection_rows(conn: Any, ids: list[str]) -> list[Any]:
    return await conn.fetch(
        "SELECT DISTINCT ON (business_id, branch, payload->>'service') business_id, branch, "
        " payload->>'service' AS service, payload->>'status' AS status, payload->>'error_code' AS error_code, "
        " COALESCE(payload->>'finished_at', payload->>'updated_at', payload->>'queued_at') AS at "
        "FROM yeoljeong_delivery_collection_status WHERE business_id = ANY($1::text[]) AND deleted_at IS NULL "
        "ORDER BY business_id, branch, payload->>'service', updated_at DESC NULLS LAST", ids)


def _group_sales(rows: list[Any]) -> dict[str, Any]:
    by_service: dict[str, dict[str, Any]] = {}
    by_branch: dict[tuple[str, str, str], dict[str, Any]] = {}
    by_day: dict[tuple[str, str], Decimal] = {}
    for r in rows:
        service = r["service"] or ""
        gross, orders = _dec(r["gross"]), int(r["orders"])
        s = by_service.setdefault(service, {"gross": Decimal(0), "orders": 0, "last_date": None})
        s["gross"] += gross
        s["orders"] += orders
        s["last_date"] = max(filter(None, [s["last_date"], r["day"]]), default=None)
        b = by_branch.setdefault((r["business_id"], r["branch"] or "", service), {"gross": Decimal(0), "orders": 0})
        b["gross"] += gross
        b["orders"] += orders
        by_day[(r["day"], service)] = by_day.get((r["day"], service), Decimal(0)) + gross
    return {"by_service": by_service, "by_branch": by_branch, "by_day": by_day}


def _group_settlements(rows: list[Any]) -> dict[str, Any]:
    by_service: dict[str, dict[str, Any]] = {}
    by_branch: dict[tuple[str, str, str], dict[str, Decimal]] = {}
    buckets = {name: {"amount": Decimal(0), "count": 0} for name in ("paid", "scheduled", "unknown")}
    for r in rows:
        service = r["service"] or ""
        bucket = settlement_bucket(r["status"])
        payout, fee, sales, n = _dec(r["payout"]), _dec(r["fee"]), _dec(r["sales"]), int(r["n"])
        s = by_service.setdefault(service, {"sales": Decimal(0), "fee": Decimal(0), "payout": Decimal(0), "count": 0,
                                            "paid": Decimal(0), "scheduled": Decimal(0), "unknown": Decimal(0)})
        s["sales"] += sales
        s["fee"] += fee
        s["payout"] += payout
        s["count"] += n
        s[bucket] += payout
        b = by_branch.setdefault((r["business_id"], r["branch"] or "", service),
                                 {"sales": Decimal(0), "fee": Decimal(0), "payout": Decimal(0)})
        b["sales"] += sales
        b["fee"] += fee
        b["payout"] += payout
        buckets[bucket]["amount"] += payout
        buckets[bucket]["count"] += n
    return {"by_service": by_service, "by_branch": by_branch, "buckets": buckets}


async def delivery_overview(tenant_id: Any, business_id: str | None, *, date_from: Any = None, date_to: Any = None) -> dict[str, Any]:
    """배달앱 자체수집 매출·정산·수수료·리뷰·광고를 앱별·매장별·일별로 합산하고 수집 신선도를 함께 돌려준다."""
    start, end = resolve_period(date_from, date_to)
    conn = await _connect()
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            businesses = await _tenant_businesses(conn, tenant_id, business_id)
            ids = [b["business_id"] for b in businesses]
            if not ids:
                sales_rows, settle_rows, review_rows, ad_rows, fresh_rows, coll_rows = [], [], [], [], [], []
            else:
                sales_rows = await _sales_rows(conn, ids, start, end)
                settle_rows = await _settlement_rows(conn, ids, start, end)
                review_rows = await _review_rows(conn, ids, start, end)
                ad_rows = await _ad_rows(conn, ids, start, end)
                fresh_rows = await _freshness_rows(conn, ids)
                coll_rows = await _collection_rows(conn, ids)
    finally:
        await conn.close()

    names = {b["business_id"]: b["name"] for b in businesses}
    sales, settle = _group_sales(sales_rows), _group_settlements(settle_rows)
    today = _today()

    fresh: dict[str, dict[str, Any]] = {}
    for r in fresh_rows:
        f = fresh.setdefault(r["service"] or "", {"rows": 0, "last_day": None, "last_collected": None})
        f["rows"] += int(r["n"])
        if r["last_day"]:
            f["last_day"] = max(filter(None, [f["last_day"], r["last_day"]]))
        if r["last_collected"]:
            f["last_collected"] = max(filter(None, [f["last_collected"], r["last_collected"]]))
    collection: dict[str, list[dict[str, Any]]] = {}
    for r in coll_rows:
        collection.setdefault(r["service"] or "", []).append({
            "branch": r["branch"] or "", "status": r["status"] or "", "error_code": r["error_code"] or "",
            "at": r["at"]})

    reviews: dict[str, dict[str, Any]] = {}
    for r in review_rows:
        rv = reviews.setdefault(r["service"] or "", {"count": 0, "waiting": 0, "rating_sum": Decimal(0), "rating_n": 0})
        rv["count"] += int(r["n"])
        rv["waiting"] += int(r["waiting"])
        if r["rating"] is not None:
            rv["rating_sum"] += _dec(r["rating"]) * int(r["n"])
            rv["rating_n"] += int(r["n"])
    ads: dict[str, dict[str, Any]] = {}
    for r in ad_rows:
        ad = ads.setdefault(r["service"] or "", {"count": 0, "cost": Decimal(0), "orders": Decimal(0), "sales": Decimal(0)})
        ad["count"] += int(r["n"])
        ad["cost"] += _dec(r["cost"])
        ad["orders"] += _dec(r["orders"])
        ad["sales"] += _dec(r["sales"])

    services = sorted(set(sales["by_service"]) | set(settle["by_service"]) | set(reviews) | set(ads) | set(fresh) | set(collection))
    service_rows = []
    for service in services:
        s = sales["by_service"].get(service, {"gross": Decimal(0), "orders": 0, "last_date": None})
        st = settle["by_service"].get(service, {})
        rv = reviews.get(service)
        ad = ads.get(service)
        f = fresh.get(service, {})
        stale, age = _stale(f.get("last_day"), today)
        latest = sorted(collection.get(service, []), key=lambda c: c["at"] or "", reverse=True)
        broken = [c for c in latest if c["status"] in BROKEN_STATUSES]
        service_rows.append({
            "service": service, "label": service_label(service),
            "sales": {"gross": _money(s["gross"]), "orders": s["orders"], "last_date": s["last_date"]},
            "settlements": {
                "sales": _money(st.get("sales")), "fee": _money(st.get("fee")), "payout": _money(st.get("payout")),
                "count": st.get("count", 0), "paid": _money(st.get("paid")), "scheduled": _money(st.get("scheduled")),
                "unknown": _money(st.get("unknown")),
                "fee_rate": round(float(st["fee"] / st["sales"] * 100), 1) if st and st.get("sales") else None},
            "reviews": {"count": rv["count"], "waiting": rv["waiting"],
                        "rating": round(float(rv["rating_sum"] / rv["rating_n"]), 2) if rv["rating_n"] else None} if rv else None,
            "ads": {"count": ad["count"], "cost": _money(ad["cost"]), "orders": _money(ad["orders"]), "sales": _money(ad["sales"])} if ad else None,
            "last_data_date": f.get("last_day"), "last_collected_at": f.get("last_collected"),
            "stale": stale, "stale_days": age,
            "collection_failed": bool(broken),
            "collection_error_code": broken[0]["error_code"] if broken else "",
            "collection_status": latest[0]["status"] if latest else "",
            "csv_upload_available": service in CSV_UPLOAD_SERVICES,
        })

    branch_keys = sorted(set(sales["by_branch"]) | set(settle["by_branch"]))
    branch_rows = []
    for key in branch_keys:
        business, branch, service = key
        s = sales["by_branch"].get(key, {"gross": Decimal(0), "orders": 0})
        st = settle["by_branch"].get(key, {})
        branch_rows.append({
            "business_name": names.get(business, ""), "branch": branch, "service": service, "label": service_label(service),
            "gross": _money(s["gross"]), "orders": s["orders"], "fee": _money(st.get("fee")), "payout": _money(st.get("payout"))})

    days: dict[str, dict[str, str]] = {}
    for (day, service), gross in sales["by_day"].items():
        days.setdefault(day, {})[service] = _money(gross)
    return {
        "period": {"date_from": start.isoformat(), "date_to": end.isoformat()},
        "businesses": businesses,
        "services": service_rows,
        "branches": branch_rows,
        "trend": [{"date": day, "by_service": by} for day, by in sorted(days.items())],
        "totals": {
            "gross": _money(sum((v["gross"] for v in sales["by_service"].values()), Decimal(0))),
            "orders": sum(v["orders"] for v in sales["by_service"].values()),
            "fee": _money(sum((v["fee"] for v in settle["by_service"].values()), Decimal(0))),
            "payout": _money(sum((v["payout"] for v in settle["by_service"].values()), Decimal(0))),
        },
        "has_data": bool(sales_rows or settle_rows or review_rows or ad_rows or fresh_rows),
    }


_CLOBE_LIVE_STAGES = ["review_box", "reviewed", "confirmed"]
_CHANNEL_LATEST_MULTI = (
    "WITH latest AS (SELECT DISTINCT ON (source_key) direction AS channel, amount AS gross, "
    " NULLIF(payload->>'date_basis', '') AS date_basis, occurred_on FROM obys_clobe_item "
    " WHERE tenant_id=$1 AND business_id = ANY($2::text[]) AND data_kind=$3 AND stage <> 'superseded' "
    " ORDER BY source_key, first_seen_at DESC)")


def _primary_basis(bases: list[str]) -> str | None:
    if not bases:
        return None
    for name in bases:
        if name.upper().startswith("SALE"):
            return name
    return sorted(bases)[0]


async def today_overview(tenant_id: Any, business_id: str | None, *, date_from: Any = None, date_to: Any = None) -> dict[str, Any]:
    """첫 화면용 요약: 최근 기간 매출(배달앱 앱별·카드·현금영수증), 정산 예정/입금, 검토 대기 건수."""
    start, end = resolve_period(date_from, date_to)
    settle_end = end + timedelta(days=SETTLEMENT_LOOKAHEAD_DAYS)
    conn = await _connect()
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            businesses = await _tenant_businesses(conn, tenant_id, business_id)
            ids = [b["business_id"] for b in businesses]
            tenant = UUID(str(tenant_id))
            if not ids:
                sales_rows, settle_rows, fresh_rows, item_rows, channel_rows, stage_rows = [], [], [], [], [], []
            else:
                sales_rows = await _sales_rows(conn, ids, start, end)
                settle_rows = await _settlement_rows(conn, ids, start, settle_end)
                fresh_rows = await _freshness_rows(conn, ids)
                item_rows = await conn.fetch(
                    "SELECT data_kind, direction, count(*) AS n, COALESCE(sum(amount), 0) AS amount FROM obys_clobe_item "
                    "WHERE tenant_id=$1 AND business_id = ANY($2::text[]) AND data_kind = ANY($3::text[]) "
                    " AND stage = ANY($4::text[]) AND occurred_on BETWEEN $5 AND $6 GROUP BY 1, 2",
                    tenant, ids, [core.KIND_CASH, core.KIND_CARD], _CLOBE_LIVE_STAGES, start, end)
                channel_rows = await conn.fetch(
                    f"{_CHANNEL_LATEST_MULTI} SELECT channel, date_basis, count(*) AS n, COALESCE(sum(gross), 0) AS gross "
                    f"FROM latest WHERE occurred_on BETWEEN $4 AND $5 GROUP BY 1, 2 ORDER BY 1, 2",
                    tenant, ids, core.KIND_CHANNEL, start, end)
                stage_rows = await conn.fetch(
                    "SELECT stage, count(*) AS n FROM obys_clobe_item WHERE tenant_id=$1 AND business_id = ANY($2::text[]) "
                    " AND data_kind = ANY($3::text[]) AND stage IN ('review_box', 'reviewed') GROUP BY 1",
                    tenant, ids, list(core.DATA_KINDS))
    finally:
        await conn.close()

    today = _today()
    sales = _group_sales(sales_rows)
    settle = _group_settlements(settle_rows)
    fresh: dict[str, str | None] = {}
    for r in fresh_rows:
        if r["kind"] == "sales" and r["last_day"]:
            service = r["service"] or ""
            fresh[service] = max(filter(None, [fresh.get(service), r["last_day"]]))

    delivery_services = []
    for service, v in sorted(sales["by_service"].items(), key=lambda kv: -kv[1]["gross"]):
        stale, age = _stale(fresh.get(service), today)
        delivery_services.append({"service": service, "label": service_label(service), "gross": _money(v["gross"]),
                                  "orders": v["orders"], "last_date": fresh.get(service), "stale": stale, "stale_days": age})

    cash_in = {"amount": Decimal(0), "count": 0}
    card_out = {"amount": Decimal(0), "count": 0}
    for r in item_rows:
        if r["data_kind"] == core.KIND_CASH and str(r["direction"]).upper() == "SALES":
            cash_in["amount"] += _dec(r["amount"])
            cash_in["count"] += int(r["n"])
        elif r["data_kind"] == core.KIND_CARD:
            card_out["amount"] += _dec(r["amount"])
            card_out["count"] += int(r["n"])

    channels: dict[str, dict[str, Any]] = {}
    for r in channel_rows:
        c = channels.setdefault(r["channel"] or "UNKNOWN", {"bases": {}})
        c["bases"][r["date_basis"] or ""] = (_dec(r["gross"]), int(r["n"]))
    channel_view = []
    for code, c in sorted(channels.items()):
        basis = _primary_basis([b for b in c["bases"] if b])
        gross, n = c["bases"].get(basis if basis is not None else "", next(iter(c["bases"].values())))
        channel_view.append({"channel": code, "label": CHANNEL_LABELS.get(code, code), "gross": _money(gross), "rows": n,
                             "date_basis": basis})
    card_sales = next((c for c in channel_view if c["channel"] == "CARD"), None)

    waiting = {r["stage"]: int(r["n"]) for r in stage_rows}
    return {
        "period": {"date_from": start.isoformat(), "date_to": end.isoformat(), "settlement_to": settle_end.isoformat()},
        "businesses": businesses,
        "delivery": {
            "services": delivery_services,
            "gross": _money(sum((v["gross"] for v in sales["by_service"].values()), Decimal(0))),
            "orders": sum(v["orders"] for v in sales["by_service"].values()),
        },
        "card_sales": card_sales,
        "channel_revenue": channel_view,
        "cash_receipt_sales": {"amount": _money(cash_in["amount"]), "count": cash_in["count"]},
        "card_spend": {"amount": _money(card_out["amount"]), "count": card_out["count"]},
        "settlement": {name: {"amount": _money(v["amount"]), "count": v["count"]} for name, v in settle["buckets"].items()},
        "review_waiting": {"review_box": waiting.get("review_box", 0), "reviewed": waiting.get("reviewed", 0),
                           "total": waiting.get("review_box", 0) + waiting.get("reviewed", 0)},
    }
