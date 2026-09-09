"""열정국밥 재고·발주 서비스 로직.

DB 접근은 asyncpg 직접 커넥션을 사용한다 (프로젝트 표준 패턴).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

KST = timezone(timedelta(hours=9))


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://aads:aads@localhost:5432/aads")


async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_db_url())


def _now_kst() -> datetime:
    return datetime.now(KST)


def _row_to_dict(row: Optional[asyncpg.Record]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def _rows_to_list(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Inventory items
# ---------------------------------------------------------------------------

async def list_items(
    business_id: str = "biz-mia",
    category: Optional[str] = None,
    low_stock: bool = False,
) -> list[dict[str, Any]]:
    conn = await _get_conn()
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]

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


async def get_item(item_id: str) -> Optional[dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM yeoljeong_inventory_items WHERE id = $1", item_id
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def create_item(data: dict[str, Any]) -> dict[str, Any]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO yeoljeong_inventory_items (
                business_id, branch_id, name, category, unit,
                current_stock, min_stock, unit_cost, supplier, supplier_code, memo
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            data.get("business_id", "biz-mia"),
            data.get("branch_id", ""),
            data["name"],
            data.get("category", ""),
            data.get("unit", "ea"),
            data.get("current_stock", 0),
            data.get("min_stock", 0),
            data.get("unit_cost", 0),
            data.get("supplier", ""),
            data.get("supplier_code", ""),
            data.get("memo", ""),
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def update_item(item_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    allowed_fields = {
        "branch_id", "name", "category", "unit", "current_stock",
        "min_stock", "unit_cost", "supplier", "supplier_code", "memo",
    }
    updates = {k: v for k, v in data.items() if k in allowed_fields}
    if not updates:
        return await get_item(item_id)

    conn = await _get_conn()
    try:
        set_clauses = []
        params: list[Any] = []
        for idx, (key, value) in enumerate(updates.items(), start=1):
            set_clauses.append(f"{key} = ${idx}")
            params.append(value)
        params.append(item_id)
        set_clauses.append("updated_at = NOW()")

        query = f"""
            UPDATE yeoljeong_inventory_items
            SET {', '.join(set_clauses)}
            WHERE id = ${len(params)}
            RETURNING *
        """
        row = await conn.fetchrow(query, *params)
        return _row_to_dict(row)
    finally:
        await conn.close()


async def delete_item(item_id: str) -> bool:
    conn = await _get_conn()
    try:
        result = await conn.execute(
            "DELETE FROM yeoljeong_inventory_items WHERE id = $1", item_id
        )
        return result.endswith("1")
    finally:
        await conn.close()


async def adjust_stock(
    item_id: str,
    movement_type: str,
    quantity: float,
    memo: str = "",
    reference_id: str = "",
) -> Optional[dict[str, Any]]:
    """재고 수량 조정 (입고/출고/폐기). movement 기록 + stock 반영을 트랜잭션으로 처리."""
    conn = await _get_conn()
    try:
        async with conn.transaction():
            item = await conn.fetchrow(
                "SELECT * FROM yeoljeong_inventory_items WHERE id = $1 FOR UPDATE",
                item_id,
            )
            if item is None:
                return None

            business_id = item["business_id"]

            if movement_type == "in":
                delta = quantity
            elif movement_type in ("out", "waste"):
                delta = -abs(quantity)
            else:
                delta = quantity

            await conn.execute(
                """
                INSERT INTO yeoljeong_stock_movements (
                    item_id, business_id, movement_type, quantity, reference_id, memo
                ) VALUES ($1, $2, $3, $4, $5, $6)
                """,
                item_id, business_id, movement_type, quantity, reference_id, memo,
            )

            updated = await conn.fetchrow(
                """
                UPDATE yeoljeong_inventory_items
                SET current_stock = current_stock + $1, updated_at = NOW()
                WHERE id = $2
                RETURNING *
                """,
                delta, item_id,
            )
            return _row_to_dict(updated)
    finally:
        await conn.close()


async def low_stock_items(business_id: str = "biz-mia") -> list[dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT * FROM yeoljeong_inventory_items
            WHERE business_id = $1 AND min_stock > 0 AND current_stock < min_stock
            ORDER BY (min_stock - current_stock) DESC
            """,
            business_id,
        )
        return _rows_to_list(rows)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

async def list_orders(
    business_id: str = "biz-mia",
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict[str, Any]]:
    conn = await _get_conn()
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]

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


async def get_order(order_id: str) -> Optional[dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM yeoljeong_purchase_orders WHERE id = $1", order_id
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def create_order(data: dict[str, Any]) -> dict[str, Any]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO yeoljeong_purchase_orders (
                business_id, branch_id, order_date, supplier, supplier_type,
                status, total_amount, items, memo
            ) VALUES ($1, $2, COALESCE($3, CURRENT_DATE), $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            data.get("business_id", "biz-mia"),
            data.get("branch_id", ""),
            data.get("order_date"),
            data.get("supplier", ""),
            data.get("supplier_type", "marketbom"),
            data.get("status", "draft"),
            data.get("total_amount", 0),
            json.dumps(data.get("items", [])),
            data.get("memo", ""),
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def update_order(order_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    allowed_fields = {
        "branch_id", "order_date", "supplier", "supplier_type",
        "status", "total_amount", "items", "memo", "invoice_number",
    }
    updates = {k: v for k, v in data.items() if k in allowed_fields}
    if not updates:
        return await get_order(order_id)

    if "items" in updates:
        updates["items"] = json.dumps(updates["items"])

    conn = await _get_conn()
    try:
        set_clauses = []
        params: list[Any] = []
        for idx, (key, value) in enumerate(updates.items(), start=1):
            set_clauses.append(f"{key} = ${idx}")
            params.append(value)
        params.append(order_id)
        set_clauses.append("updated_at = NOW()")

        query = f"""
            UPDATE yeoljeong_purchase_orders
            SET {', '.join(set_clauses)}
            WHERE id = ${len(params)}
            RETURNING *
        """
        row = await conn.fetchrow(query, *params)
        return _row_to_dict(row)
    finally:
        await conn.close()


async def delete_order(order_id: str) -> tuple[bool, str]:
    """draft 상태인 발주만 삭제 가능. (성공여부, 사유) 반환."""
    conn = await _get_conn()
    try:
        order = await conn.fetchrow(
            "SELECT status FROM yeoljeong_purchase_orders WHERE id = $1", order_id
        )
        if order is None:
            return False, "not_found"
        if order["status"] != "draft":
            return False, "not_draft"

        await conn.execute(
            "DELETE FROM yeoljeong_purchase_orders WHERE id = $1", order_id
        )
        return True, "deleted"
    finally:
        await conn.close()


async def receive_order(
    order_id: str,
    invoice_number: str = "",
    memo: str = "",
) -> tuple[Optional[dict[str, Any]], str]:
    """입고 처리: 발주 status='received'로 변경하고 items의 재고를 자동 반영한다."""
    conn = await _get_conn()
    try:
        async with conn.transaction():
            order = await conn.fetchrow(
                "SELECT * FROM yeoljeong_purchase_orders WHERE id = $1 FOR UPDATE",
                order_id,
            )
            if order is None:
                return None, "not_found"
            if order["status"] == "received":
                return _row_to_dict(order), "already_received"

            items_raw = order["items"]
            items = json.loads(items_raw) if isinstance(items_raw, str) else (items_raw or [])
            business_id = order["business_id"]

            for line in items:
                item_id = line.get("item_id")
                qty = line.get("quantity", 0)
                if not item_id or not qty:
                    continue

                await conn.execute(
                    """
                    INSERT INTO yeoljeong_stock_movements (
                        item_id, business_id, movement_type, quantity, reference_id, memo
                    ) VALUES ($1, $2, 'in', $3, $4, $5)
                    """,
                    item_id, business_id, qty, order_id, f"발주 입고: {order_id}",
                )
                await conn.execute(
                    """
                    UPDATE yeoljeong_inventory_items
                    SET current_stock = current_stock + $1,
                        last_ordered_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $2
                    """,
                    qty, item_id,
                )

            updated = await conn.fetchrow(
                """
                UPDATE yeoljeong_purchase_orders
                SET status = 'received', received_at = NOW(),
                    invoice_number = COALESCE(NULLIF($1, ''), invoice_number),
                    memo = CASE WHEN $2 != '' THEN $2 ELSE memo END,
                    updated_at = NOW()
                WHERE id = $3
                RETURNING *
                """,
                invoice_number, memo, order_id,
            )
            return _row_to_dict(updated), "received"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Stock movements
# ---------------------------------------------------------------------------

async def list_movements(
    item_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict[str, Any]]:
    conn = await _get_conn()
    try:
        conditions = ["1 = 1"]
        params: list[Any] = []

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
