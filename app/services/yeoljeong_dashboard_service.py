"""Dashboard aggregation service for the Yeoljeong store assistant."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
SALES_PERIODS = {"daily", "weekly", "monthly"}


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://aads:aads@localhost:5432/aads")


def _today_kst() -> date:
    return datetime.now(KST).date()


def _run_async(coro: Any) -> Any:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=30)
    return asyncio.run(coro)


async def _query(sql: str, *args: Any) -> list[dict[str, Any]]:
    import asyncpg
    pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            return [dict(r) for r in rows]
    finally:
        await pool.close()


async def _query_one(sql: str, *args: Any) -> dict[str, Any]:
    import asyncpg
    pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *args)
            return dict(row) if row else {}
    finally:
        await pool.close()


def _parse_payload(val: Any) -> dict[str, Any]:
    if isinstance(val, str):
        return json.loads(val)
    return val if isinstance(val, dict) else {}


def _business_filter(business_id: str, parameter_index: int = 1) -> tuple[str, tuple[str, ...]]:
    """Return a parameterized optional business filter."""
    if not business_id:
        return "", ()
    return f"AND business_id = ${parameter_index}", (business_id,)


def _sales_bucket(sale_date: str, period: str) -> str:
    parsed = date.fromisoformat(sale_date)
    if period == "weekly":
        parsed -= timedelta(days=parsed.weekday())
    elif period == "monthly":
        parsed = parsed.replace(day=1)
    return parsed.isoformat()


def _aggregate_sales_rows(rows: list[Any], period: str) -> list[dict[str, Any]]:
    """Aggregate daily query rows without changing the response's date contract."""
    if period not in SALES_PERIODS:
        raise ValueError(f"unsupported sales period: {period}")

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        sale_date = str(row["sale_date"] or "")
        if not sale_date:
            continue
        bucket = _sales_bucket(sale_date, period)
        item = buckets.setdefault(
            bucket,
            {
                "date": bucket,
                "period": period,
                "total": 0,
                "baemin": 0,
                "coupangeats": 0,
                "yogiyo": 0,
                "ddangyo": 0,
                "orders": 0,
            },
        )
        service = str(row["service"] or "").lower()
        amount = int(row["total"] or 0)
        item["total"] += amount
        item["orders"] += int(row["orders"] or 0)
        if service in item:
            item[service] += amount
    return [buckets[key] for key in sorted(buckets)]


def get_kpis(business_id: str = "") -> dict[str, Any]:
    today = _today_kst()
    today_str = today.isoformat()
    first_of_week = (today - timedelta(days=today.weekday())).isoformat()
    first_of_month = today.replace(day=1).isoformat()

    async def _run() -> dict[str, Any]:
        import asyncpg
        pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=3)
        try:
            async with pool.acquire() as conn:
                biz, biz_args = _business_filter(business_id, 4)

                sales = await conn.fetchrow(
                    f"SELECT "
                    f"COALESCE(SUM(COALESCE(NULLIF(payload->>'gross_amount','')::numeric, 0)) "
                    f"FILTER (WHERE payload->>'occurred_on' = $1), 0) AS today_total, "
                    f"COALESCE(SUM(COALESCE(NULLIF(payload->>'gross_amount','')::numeric, 0)) "
                    f"FILTER (WHERE payload->>'occurred_on' >= $2), 0) AS week_total, "
                    f"COALESCE(SUM(COALESCE(NULLIF(payload->>'gross_amount','')::numeric, 0)) "
                    f"FILTER (WHERE payload->>'occurred_on' >= $3), 0) AS month_total "
                    f"FROM yeoljeong_delivery_sales "
                    f"WHERE payload->>'occurred_on' <= $1 AND deleted_at IS NULL {biz}",
                    today_str,
                    first_of_week,
                    first_of_month,
                    *biz_args,
                )
                settlement_biz, settlement_args = _business_filter(business_id)
                pending_stl = await conn.fetchrow(
                    f"SELECT COALESCE(SUM(COALESCE(NULLIF(payload->>'settlement_amount','')::numeric, 0)), 0) AS total, "
                    f"COUNT(*) AS cnt "
                    f"FROM yeoljeong_delivery_settlements "
                    f"WHERE COALESCE(payload->>'settlement_status','') "
                    f"NOT IN ('deposited','confirmed','입금완료') "
                    f"AND deleted_at IS NULL {settlement_biz}",
                    *settlement_args,
                )
                employee_biz, employee_args = _business_filter(business_id)
                emp_cnt = await conn.fetchrow(
                    f"SELECT COUNT(*) AS cnt FROM yeoljeong_employee_join_requests "
                    f"WHERE status = 'approved' AND deleted_at IS NULL {employee_biz}",
                    *employee_args,
                )
                p_joins = await conn.fetchrow(
                    f"SELECT COUNT(*) AS cnt FROM yeoljeong_employee_join_requests "
                    f"WHERE status = 'pending' AND deleted_at IS NULL {employee_biz}",
                    *employee_args,
                )
                p_docs = await conn.fetchrow(
                    f"SELECT COUNT(*) AS cnt FROM yeoljeong_onboarding_documents "
                    f"WHERE COALESCE(status, '') NOT IN ('approved','rejected') "
                    f"AND deleted_at IS NULL {employee_biz}",
                    *employee_args,
                )
                p_contracts = await conn.fetchrow(
                    f"SELECT COUNT(*) AS cnt FROM yeoljeong_contracts "
                    f"WHERE COALESCE(status, '') NOT IN ('signed','completed','cancelled','rejected') "
                    f"AND deleted_at IS NULL {employee_biz}",
                    *employee_args,
                )

                pj = int(p_joins["cnt"]) if p_joins else 0
                pd = int(p_docs["cnt"]) if p_docs else 0
                pc = int(p_contracts["cnt"]) if p_contracts else 0

                return {
                    "today_sales": int(sales["today_total"]) if sales else 0,
                    "week_sales": int(sales["week_total"]) if sales else 0,
                    "month_sales": int(sales["month_total"]) if sales else 0,
                    "pending_settlement_amount": int(pending_stl["total"]) if pending_stl else 0,
                    "pending_settlement_count": int(pending_stl["cnt"]) if pending_stl else 0,
                    "employee_count": int(emp_cnt["cnt"]) if emp_cnt else 0,
                    "pending_tasks": pj + pd + pc,
                    "pending_joins": pj,
                    "pending_documents": pd,
                    "pending_contracts": pc,
                    "date": today_str,
                }
        finally:
            await pool.close()

    return _run_async(_run())


def get_sales_trend(period: str = "daily", days: int = 30, business_id: str = "") -> list[dict[str, Any]]:
    if period not in SALES_PERIODS:
        raise ValueError(f"unsupported sales period: {period}")
    days = max(1, min(int(days), 365))
    today = _today_kst()
    start_date = (today - timedelta(days=days - 1)).isoformat()
    end_date = today.isoformat()
    biz, biz_args = _business_filter(business_id, 3)

    async def _run() -> list[dict[str, Any]]:
        import asyncpg
        pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=2)
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT payload->>'occurred_on' AS sale_date, "
                    f"payload->>'service' AS service, "
                    f"COALESCE(SUM(COALESCE(NULLIF(payload->>'gross_amount','')::numeric, 0)), 0) AS total, "
                    f"COALESCE(SUM(NULLIF(payload->>'order_count','')::int), 0) AS orders "
                    f"FROM yeoljeong_delivery_sales "
                    f"WHERE payload->>'occurred_on' >= $1 AND payload->>'occurred_on' <= $2 "
                    f"AND deleted_at IS NULL {biz} "
                    f"GROUP BY payload->>'occurred_on', payload->>'service' "
                    f"ORDER BY 1",
                    start_date, end_date, *biz_args,
                )
                return _aggregate_sales_rows(rows, period)
        finally:
            await pool.close()

    return _run_async(_run())


def get_tasks(business_id: str = "") -> list[dict[str, Any]]:
    biz, biz_args = _business_filter(business_id)

    async def _run() -> list[dict[str, Any]]:
        import asyncpg
        pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=2)
        try:
            async with pool.acquire() as conn:
                tasks: list[dict[str, Any]] = []

                for row in await conn.fetch(
                    f"SELECT id, employee_name FROM yeoljeong_employee_join_requests "
                    f"WHERE status = 'pending' AND deleted_at IS NULL {biz} "
                    f"ORDER BY created_at DESC LIMIT 20",
                    *biz_args,
                ):
                    tasks.append({"type": "join_request", "title": f"{row['employee_name'] or '직원'} 가입 신청 검토", "priority": "high", "reference_id": row["id"]})

                for row in await conn.fetch(
                    f"SELECT id, employee_name, document_label, document_type "
                    f"FROM yeoljeong_onboarding_documents "
                    f"WHERE COALESCE(status, '') NOT IN ('approved','rejected') "
                    f"AND deleted_at IS NULL {biz} "
                    f"ORDER BY created_at DESC LIMIT 20",
                    *biz_args,
                ):
                    label = row["document_label"] or row["document_type"] or "서류"
                    tasks.append({"type": "document_review", "title": f"{row['employee_name'] or ''} {label} 검토".strip(), "priority": "normal", "reference_id": row["id"]})

                for row in await conn.fetch(
                    f"SELECT id, employee_name FROM yeoljeong_contracts "
                    f"WHERE COALESCE(status, '') NOT IN ('signed','completed','cancelled','rejected') "
                    f"AND deleted_at IS NULL {biz} "
                    f"ORDER BY created_at DESC LIMIT 20",
                    *biz_args,
                ):
                    tasks.append({"type": "contract_sign", "title": f"{row['employee_name'] or ''} 계약서 서명 대기".strip(), "priority": "normal", "reference_id": row["id"]})

                return tasks
        finally:
            await pool.close()

    return _run_async(_run())


def get_settlement_summary(business_id: str = "") -> list[dict[str, Any]]:
    biz, biz_args = _business_filter(business_id)

    async def _run() -> list[dict[str, Any]]:
        import asyncpg
        pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=2)
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT payload->>'service' AS service, "
                    f"COUNT(*) FILTER (WHERE COALESCE(payload->>'settlement_status','') NOT IN ('deposited','confirmed','입금완료')) AS pending_cnt, "
                    f"COALESCE(SUM(COALESCE(NULLIF(payload->>'settlement_amount','')::numeric, 0)) FILTER (WHERE COALESCE(payload->>'settlement_status','') NOT IN ('deposited','confirmed','입금완료')), 0) AS pending_amount, "
                    f"COUNT(*) FILTER (WHERE payload->>'settlement_status' IN ('deposited','confirmed','입금완료')) AS done_cnt, "
                    f"COALESCE(SUM(COALESCE(NULLIF(payload->>'settlement_amount','')::numeric, 0)) FILTER (WHERE payload->>'settlement_status' IN ('deposited','confirmed','입금완료')), 0) AS done_amount "
                    f"FROM yeoljeong_delivery_settlements WHERE deleted_at IS NULL {biz} "
                    f"GROUP BY payload->>'service' ORDER BY 1",
                    *biz_args,
                )
                return [{"service": r["service"], "pending_count": int(r["pending_cnt"]), "pending_amount": int(r["pending_amount"]),
                         "done_count": int(r["done_cnt"]), "done_amount": int(r["done_amount"])} for r in rows]
        finally:
            await pool.close()

    return _run_async(_run())


def get_expense_summary(business_id: str = "", date_from: str = "", date_to: str = "") -> list[dict[str, Any]]:
    today = _today_kst()
    if not date_from:
        date_from = today.replace(day=1).isoformat()
    if not date_to:
        date_to = today.isoformat()
    biz, biz_args = _business_filter(business_id, 3)

    async def _run() -> list[dict[str, Any]]:
        import asyncpg
        pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=2)
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT COALESCE(NULLIF(category,''), '미분류') AS cat, "
                    f"SUM(amount) AS total, COUNT(*) AS cnt "
                    f"FROM yeoljeong_bank_transactions "
                    f"WHERE direction = 'out' AND occurred_date >= $1::date AND occurred_date <= $2::date {biz} "
                    f"GROUP BY 1 ORDER BY total DESC",
                    date_from, date_to, *biz_args,
                )
                return [{"category": r["cat"], "total": int(r["total"] or 0), "count": int(r["cnt"])} for r in rows]
        finally:
            await pool.close()

    return _run_async(_run())
