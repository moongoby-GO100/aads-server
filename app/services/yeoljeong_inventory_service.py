"""열정국밥 재고·발주 서비스 로직.

DB 접근은 asyncpg 직접 커넥션을 사용한다 (프로젝트 표준 패턴).

모든 공개 함수는 **첫 인자로 company_id 를 받고** 모든 쿼리의 WHERE 에 반영한다.
company_id 는 인증 사용자의 tenant_id 에서만 나오며(API 계층 `_company_id`),
요청 본문이 보낸 값은 쓰지 않는다 — 다른 회사의 재고를 읽거나 바꾸지 못하게 하는
유일한 경계가 이 인자다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import asyncpg

from app.core.obys_db import obys_db_url

KST = timezone(timedelta(hours=9))


def _db_url() -> str:
    return obys_db_url()


async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_db_url())


def _now_kst() -> datetime:
    return datetime.now(KST)


def _row_to_dict(row: Optional[asyncpg.Record]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def _rows_to_list(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def to_decimal(value: Any) -> Decimal:
    """NUMERIC 컬럼과 오차 없이 맞물리도록 수량·금액을 Decimal 로 맞춘다.

    float 를 그대로 넘기면 0.1 이 0.1000000000000000055 로 저장되어 실사 대조가
    어긋나므로 str() 를 거쳐 변환한다.
    """
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        raise ValueError("quantity is required")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:  # pragma: no cover - 방어
        raise ValueError(f"invalid quantity: {value!r}") from exc


def _scope_company(company_id: str) -> str:
    company = str(company_id or "").strip()
    if not company:
        raise ValueError("company_id is required")
    return company


# ---------------------------------------------------------------------------
# Inventory items
# ---------------------------------------------------------------------------

async def list_items(
    company_id: str,
    category: Optional[str] = None,
    low_stock: bool = False,
    business_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        conditions = ["company_id = $1"]
        params: list[Any] = [company]

        if business_id:
            params.append(business_id)
            conditions.append(f"business_id = ${len(params)}")

        if category:
            params.append(category)
            conditions.append(f"category = ${len(params)}")

        if low_stock:
            conditions.append("current_stock < min_stock AND min_stock > 0")

        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT * FROM yeoljeong_inventory_items
            WHERE {where_clause}
            ORDER BY name ASC
        """
        rows = await conn.fetch(query, *params)
        return _rows_to_list(rows)
    finally:
        await conn.close()


async def get_item(company_id: str, item_id: str) -> Optional[dict[str, Any]]:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM yeoljeong_inventory_items WHERE id = $1 AND company_id = $2",
            item_id,
            company,
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def create_item(company_id: str, data: dict[str, Any]) -> dict[str, Any]:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO yeoljeong_inventory_items (
                company_id, business_id, branch_id, name, category, unit,
                current_stock, min_stock, unit_cost, supplier, supplier_code, memo
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING *
            """,
            company,
            data.get("business_id") or company,
            data.get("branch_id", ""),
            data["name"],
            data.get("category", ""),
            data.get("unit", "ea"),
            to_decimal(data.get("current_stock") or 0),
            to_decimal(data.get("min_stock") or 0),
            to_decimal(data.get("unit_cost") or 0),
            data.get("supplier", ""),
            data.get("supplier_code", ""),
            data.get("memo", ""),
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def update_item(
    company_id: str, item_id: str, data: dict[str, Any]
) -> Optional[dict[str, Any]]:
    company = _scope_company(company_id)
    allowed_fields = {
        "branch_id", "name", "category", "unit", "current_stock",
        "min_stock", "unit_cost", "supplier", "supplier_code", "memo",
    }
    numeric_fields = {"current_stock", "min_stock", "unit_cost"}
    updates = {k: v for k, v in data.items() if k in allowed_fields}
    for key in numeric_fields & updates.keys():
        updates[key] = to_decimal(updates[key])
    if not updates:
        return await get_item(company, item_id)

    conn = await _get_conn()
    try:
        set_clauses = []
        params: list[Any] = []
        for idx, (key, value) in enumerate(updates.items(), start=1):
            set_clauses.append(f"{key} = ${idx}")
            params.append(value)
        params.append(item_id)
        item_placeholder = len(params)
        params.append(company)
        set_clauses.append("updated_at = NOW()")

        query = f"""
            UPDATE yeoljeong_inventory_items
            SET {', '.join(set_clauses)}
            WHERE id = ${item_placeholder} AND company_id = ${len(params)}
            RETURNING *
        """
        row = await conn.fetchrow(query, *params)
        return _row_to_dict(row)
    finally:
        await conn.close()


async def delete_item(company_id: str, item_id: str) -> bool:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        result = await conn.execute(
            "DELETE FROM yeoljeong_inventory_items WHERE id = $1 AND company_id = $2",
            item_id,
            company,
        )
        return result.endswith("1")
    finally:
        await conn.close()


async def adjust_stock(
    company_id: str,
    item_id: str,
    movement_type: str,
    quantity: Any,
    memo: str = "",
    reference_id: str = "",
    actor: str = "",
) -> Optional[dict[str, Any]]:
    """재고 수량 조정 (입고/출고/폐기). movement 기록 + stock 반영을 트랜잭션으로 처리."""
    company = _scope_company(company_id)
    qty = to_decimal(quantity)
    conn = await _get_conn()
    try:
        async with conn.transaction():
            item = await conn.fetchrow(
                """
                SELECT * FROM yeoljeong_inventory_items
                WHERE id = $1 AND company_id = $2
                FOR UPDATE
                """,
                item_id,
                company,
            )
            if item is None:
                return None

            business_id = item["business_id"]

            if movement_type == "in":
                delta = qty
            elif movement_type in ("out", "waste"):
                delta = -abs(qty)
            else:
                delta = qty

            await conn.execute(
                """
                INSERT INTO yeoljeong_stock_movements (
                    item_id, company_id, business_id, movement_type,
                    quantity, reference_id, memo, created_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                item_id, company, business_id, movement_type,
                qty, reference_id, memo, actor,
            )

            updated = await conn.fetchrow(
                """
                UPDATE yeoljeong_inventory_items
                SET current_stock = current_stock + $1, updated_at = NOW()
                WHERE id = $2 AND company_id = $3
                RETURNING *
                """,
                delta, item_id, company,
            )
            return _row_to_dict(updated)
    finally:
        await conn.close()


async def low_stock_items(
    company_id: str, business_id: Optional[str] = None
) -> list[dict[str, Any]]:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        conditions = ["company_id = $1", "min_stock > 0", "current_stock < min_stock"]
        params: list[Any] = [company]
        if business_id:
            params.append(business_id)
            conditions.append(f"business_id = ${len(params)}")

        rows = await conn.fetch(
            f"""
            SELECT * FROM yeoljeong_inventory_items
            WHERE {' AND '.join(conditions)}
            ORDER BY (min_stock - current_stock) DESC
            """,
            *params,
        )
        return _rows_to_list(rows)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

async def list_orders(
    company_id: str,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    business_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        conditions = ["company_id = $1"]
        params: list[Any] = [company]

        if business_id:
            params.append(business_id)
            conditions.append(f"business_id = ${len(params)}")
        if status:
            params.append(status)
            conditions.append(f"status = ${len(params)}")
        if date_from:
            params.append(date_from)
            conditions.append(f"order_date >= ${len(params)}")
        if date_to:
            params.append(date_to)
            conditions.append(f"order_date <= ${len(params)}")

        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT * FROM yeoljeong_purchase_orders
            WHERE {where_clause}
            ORDER BY order_date DESC, created_at DESC
        """
        rows = await conn.fetch(query, *params)
        return _rows_to_list(rows)
    finally:
        await conn.close()


async def get_order(company_id: str, order_id: str) -> Optional[dict[str, Any]]:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM yeoljeong_purchase_orders WHERE id = $1 AND company_id = $2",
            order_id,
            company,
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def create_order(company_id: str, data: dict[str, Any]) -> dict[str, Any]:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO yeoljeong_purchase_orders (
                company_id, business_id, branch_id, order_date, supplier,
                supplier_type, status, total_amount, items, memo
            ) VALUES ($1, $2, $3, COALESCE($4, CURRENT_DATE), $5, $6, $7, $8, $9, $10)
            RETURNING *
            """,
            company,
            data.get("business_id") or company,
            data.get("branch_id", ""),
            data.get("order_date"),
            data.get("supplier", ""),
            data.get("supplier_type", "marketbom"),
            data.get("status", "draft"),
            to_decimal(data.get("total_amount") or 0),
            json.dumps(data.get("items", [])),
            data.get("memo", ""),
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def update_order(
    company_id: str, order_id: str, data: dict[str, Any]
) -> Optional[dict[str, Any]]:
    company = _scope_company(company_id)
    allowed_fields = {
        "branch_id", "order_date", "supplier", "supplier_type",
        "status", "total_amount", "items", "memo", "invoice_number",
    }
    updates = {k: v for k, v in data.items() if k in allowed_fields}
    if not updates:
        return await get_order(company, order_id)

    if "items" in updates:
        updates["items"] = json.dumps(updates["items"])
    if "total_amount" in updates:
        updates["total_amount"] = to_decimal(updates["total_amount"])

    conn = await _get_conn()
    try:
        set_clauses = []
        params: list[Any] = []
        for idx, (key, value) in enumerate(updates.items(), start=1):
            set_clauses.append(f"{key} = ${idx}")
            params.append(value)
        params.append(order_id)
        order_placeholder = len(params)
        params.append(company)
        set_clauses.append("updated_at = NOW()")

        query = f"""
            UPDATE yeoljeong_purchase_orders
            SET {', '.join(set_clauses)}
            WHERE id = ${order_placeholder} AND company_id = ${len(params)}
            RETURNING *
        """
        row = await conn.fetchrow(query, *params)
        return _row_to_dict(row)
    finally:
        await conn.close()


async def delete_order(company_id: str, order_id: str) -> tuple[bool, str]:
    """draft 상태인 발주만 삭제 가능. (성공여부, 사유) 반환."""
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        order = await conn.fetchrow(
            "SELECT status FROM yeoljeong_purchase_orders WHERE id = $1 AND company_id = $2",
            order_id,
            company,
        )
        if order is None:
            return False, "not_found"
        if order["status"] != "draft":
            return False, "not_draft"

        await conn.execute(
            "DELETE FROM yeoljeong_purchase_orders WHERE id = $1 AND company_id = $2",
            order_id,
            company,
        )
        return True, "deleted"
    finally:
        await conn.close()


async def receive_order(
    company_id: str,
    order_id: str,
    invoice_number: str = "",
    memo: str = "",
    actor: str = "",
) -> tuple[Optional[dict[str, Any]], str]:
    """입고 처리: 발주 status='received'로 변경하고 items의 재고를 자동 반영한다.

    입고 검증 — 발주 줄의 item_id 는 **같은 회사의 품목이어야** 반영한다.
    남의 회사 품목 id 가 발주 JSON 에 섞여 있어도 그 줄은 건너뛰고,
    실제로 반영된 줄만 received_items 에 남겨 사후 대조가 가능하게 한다.
    """
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        async with conn.transaction():
            order = await conn.fetchrow(
                """
                SELECT * FROM yeoljeong_purchase_orders
                WHERE id = $1 AND company_id = $2
                FOR UPDATE
                """,
                order_id,
                company,
            )
            if order is None:
                return None, "not_found"
            if order["status"] == "received":
                return _row_to_dict(order), "already_received"

            items_raw = order["items"]
            items = json.loads(items_raw) if isinstance(items_raw, str) else (items_raw or [])
            business_id = order["business_id"]
            applied: list[dict[str, Any]] = []

            for line in items:
                item_id = line.get("item_id")
                raw_qty = line.get("quantity", 0)
                if not item_id or not raw_qty:
                    continue
                qty = to_decimal(raw_qty)
                if qty <= 0:
                    continue

                owned = await conn.fetchval(
                    """
                    SELECT id FROM yeoljeong_inventory_items
                    WHERE id = $1 AND company_id = $2
                    FOR UPDATE
                    """,
                    item_id,
                    company,
                )
                if owned is None:
                    continue

                await conn.execute(
                    """
                    INSERT INTO yeoljeong_stock_movements (
                        item_id, company_id, business_id, movement_type,
                        quantity, reference_id, memo, created_by
                    ) VALUES ($1, $2, $3, 'in', $4, $5, $6, $7)
                    """,
                    item_id, company, business_id, qty, order_id,
                    f"발주 입고: {order_id}", actor,
                )
                await conn.execute(
                    """
                    UPDATE yeoljeong_inventory_items
                    SET current_stock = current_stock + $1,
                        last_ordered_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $2 AND company_id = $3
                    """,
                    qty, item_id, company,
                )
                applied.append({"item_id": item_id, "quantity": str(qty)})

            updated = await conn.fetchrow(
                """
                UPDATE yeoljeong_purchase_orders
                SET status = 'received', received_at = NOW(),
                    received_by = COALESCE(NULLIF($1, ''), received_by),
                    received_items = $2,
                    invoice_number = COALESCE(NULLIF($3, ''), invoice_number),
                    memo = CASE WHEN $4 != '' THEN $4 ELSE memo END,
                    updated_at = NOW()
                WHERE id = $5 AND company_id = $6
                RETURNING *
                """,
                actor, json.dumps(applied), invoice_number, memo, order_id, company,
            )
            return _row_to_dict(updated), "received"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Stock movements
# ---------------------------------------------------------------------------

async def list_movements(
    company_id: str,
    item_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict[str, Any]]:
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        conditions = ["company_id = $1"]
        params: list[Any] = [company]

        if item_id:
            params.append(item_id)
            conditions.append(f"item_id = ${len(params)}")
        if date_from:
            params.append(date_from)
            conditions.append(f"created_at >= ${len(params)}")
        if date_to:
            params.append(date_to)
            conditions.append(f"created_at <= ${len(params)}")

        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT * FROM yeoljeong_stock_movements
            WHERE {where_clause}
            ORDER BY created_at DESC
        """
        rows = await conn.fetch(query, *params)
        return _rows_to_list(rows)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Stock take (재고 실사)
# ---------------------------------------------------------------------------

async def record_stocktake(
    company_id: str,
    item_id: str,
    counted_quantity: Any,
    counted_by: str = "",
    memo: str = "",
) -> tuple[Optional[dict[str, Any]], str]:
    """재고 실사 1건을 기록하고 장부 수량을 실사 수량에 맞춘다.

    - 스냅샷(`yeoljeong_stock_balances`)에 장부/실사/차이와 **실행자**를 남긴다.
    - 차이가 있으면 `stocktake` 수불을 같이 남겨 이후 수불 내역만 봐도
      수량이 왜 움직였는지 추적된다.
    - 실사 수량이 음수면 거부한다(0 은 재고 소진이라 허용).
    """
    company = _scope_company(company_id)
    counted = to_decimal(counted_quantity)
    if counted < 0:
        raise ValueError("counted_quantity must be zero or greater")

    conn = await _get_conn()
    try:
        async with conn.transaction():
            item = await conn.fetchrow(
                """
                SELECT * FROM yeoljeong_inventory_items
                WHERE id = $1 AND company_id = $2
                FOR UPDATE
                """,
                item_id,
                company,
            )
            if item is None:
                return None, "not_found"

            system_qty = to_decimal(item["current_stock"] or 0)
            difference = counted - system_qty

            balance = await conn.fetchrow(
                """
                INSERT INTO yeoljeong_stock_balances (
                    company_id, business_id, branch_id, item_id, item_name, unit,
                    system_quantity, counted_quantity, difference, counted_by, memo
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING *
                """,
                company,
                item["business_id"],
                item["branch_id"],
                item_id,
                item["name"],
                item["unit"],
                system_qty,
                counted,
                difference,
                counted_by,
                memo,
            )

            if difference != 0:
                await conn.execute(
                    """
                    INSERT INTO yeoljeong_stock_movements (
                        item_id, company_id, business_id, movement_type,
                        quantity, reference_id, memo, created_by
                    ) VALUES ($1, $2, $3, 'stocktake', $4, $5, $6, $7)
                    """,
                    item_id,
                    company,
                    item["business_id"],
                    difference,
                    balance["id"],
                    memo or f"재고 실사 조정: {balance['id']}",
                    counted_by,
                )

            updated = await conn.fetchrow(
                """
                UPDATE yeoljeong_inventory_items
                SET current_stock = $1, updated_at = NOW()
                WHERE id = $2 AND company_id = $3
                RETURNING *
                """,
                counted, item_id, company,
            )

            return {
                "balance": _row_to_dict(balance),
                "item": _row_to_dict(updated),
            }, "recorded"
    finally:
        await conn.close()


async def list_stock_balances(
    company_id: str,
    item_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """재고 실사 스냅샷 조회 (최근 순)."""
    company = _scope_company(company_id)
    conn = await _get_conn()
    try:
        conditions = ["company_id = $1"]
        params: list[Any] = [company]

        if item_id:
            params.append(item_id)
            conditions.append(f"item_id = ${len(params)}")
        if date_from:
            params.append(date_from)
            conditions.append(f"counted_at >= ${len(params)}")
        if date_to:
            params.append(date_to)
            conditions.append(f"counted_at <= ${len(params)}")

        params.append(max(1, min(int(limit or 200), 1000)))
        query = f"""
            SELECT * FROM yeoljeong_stock_balances
            WHERE {' AND '.join(conditions)}
            ORDER BY counted_at DESC
            LIMIT ${len(params)}
        """
        rows = await conn.fetch(query, *params)
        return _rows_to_list(rows)
    finally:
        await conn.close()
