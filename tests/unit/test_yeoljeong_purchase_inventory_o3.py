"""FOOD-O3-PURCHASE-INVENTORY: 오비서 매입·재고 회사 경계와 발주→입고→실사 흐름.

실제 Postgres 없이 서비스의 SQL 을 그대로 태우기 위해 asyncpg 커넥션을 흉내 내는
인메모리 스토어를 쓴다. 회사 경계가 WHERE 에서 빠지면 이 테스트가 깨진다 —
그게 이 파일이 지키는 것이다.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-for-obys-inventory")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import obys_inventory as api  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.services import yeoljeong_inventory_service as service  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "scripts/migrations/20260918_yeoljeong_purchase_inventory_o3.sql"

_EQUALITY = re.compile(r"(\w+) = \$(\d+)")
_SCOPED_WHERE = re.compile(r"WHERE id = \$(\d+) AND company_id = \$(\d+)")


# ---------------------------------------------------------------------------
# asyncpg 흉내 (서비스가 실제로 만드는 SQL 만 이해한다)
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.movements: list[dict[str, Any]] = []
        self.balances: list[dict[str, Any]] = []
        self._seq = 0

    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def rows_for(self, query: str) -> list[dict[str, Any]]:
        if "yeoljeong_inventory_items" in query:
            return self.items
        if "yeoljeong_purchase_orders" in query:
            return self.orders
        if "yeoljeong_stock_movements" in query:
            return self.movements
        if "yeoljeong_stock_balances" in query:
            return self.balances
        raise AssertionError(f"알 수 없는 테이블: {query}")


class FakeConn:
    """WHERE 의 등호 조건을 실제로 적용한다 — company_id 가 빠지면 격리가 깨진다."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.closed = False
        self.queries: list[str] = []

    async def close(self) -> None:
        self.closed = True

    @asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self):
        return self._tx()

    # -- 조회 -------------------------------------------------------------
    def _filters(self, query: str, params: tuple) -> dict[str, Any]:
        where = query.split("WHERE", 1)[1] if "WHERE" in query else ""
        where = where.split("ORDER BY", 1)[0].split("RETURNING", 1)[0]
        found: dict[str, Any] = {}
        for column, index in _EQUALITY.findall(where):
            if column in {"min_stock", "current_stock"}:
                continue
            found[column] = params[int(index) - 1]
        return found

    def _select(self, query: str, params: tuple) -> list[dict[str, Any]]:
        rows = self.store.rows_for(query)
        filters = self._filters(query, params)
        matched = [
            row for row in rows
            if all(str(row.get(key)) == str(value) for key, value in filters.items())
        ]
        if "current_stock < min_stock" in query:
            matched = [
                row for row in matched
                if Decimal(str(row["min_stock"])) > 0
                and Decimal(str(row["current_stock"])) < Decimal(str(row["min_stock"]))
            ]
        return matched

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        return [dict(row) for row in self._select(query, params)]

    async def fetchrow(self, query: str, *params: Any) -> Optional[dict[str, Any]]:
        self.queries.append(query)
        if query.lstrip().startswith("INSERT"):
            return self._insert(query, params)
        if query.lstrip().startswith("UPDATE"):
            return self._update(query, params)
        rows = self._select(query, params)
        return dict(rows[0]) if rows else None

    async def fetchval(self, query: str, *params: Any) -> Any:
        row = await self.fetchrow(query, *params)
        if row is None:
            return None
        column = query.split("SELECT", 1)[1].split("FROM", 1)[0].strip()
        return row.get(column, next(iter(row.values())))

    async def execute(self, query: str, *params: Any) -> str:
        self.queries.append(query)
        stripped = query.lstrip()
        if stripped.startswith("INSERT"):
            self._insert(query, params)
            return "INSERT 0 1"
        if stripped.startswith("UPDATE"):
            return "UPDATE 1" if self._update(query, params) else "UPDATE 0"
        if stripped.startswith("DELETE"):
            rows = self.store.rows_for(query)
            doomed = self._select(query, params)
            for row in doomed:
                rows.remove(row)
            return f"DELETE {len(doomed)}"
        raise AssertionError(f"지원하지 않는 구문: {query}")

    # -- 변경 -------------------------------------------------------------
    def _insert(self, query: str, params: tuple) -> dict[str, Any]:
        if "INSERT INTO yeoljeong_inventory_items" in query:
            row = {
                "id": self.store.next_id("inv"),
                "company_id": params[0], "business_id": params[1], "branch_id": params[2],
                "name": params[3], "category": params[4], "unit": params[5],
                "current_stock": params[6], "min_stock": params[7], "unit_cost": params[8],
                "supplier": params[9], "supplier_code": params[10], "memo": params[11],
                "last_ordered_at": None,
            }
            self.store.items.append(row)
        elif "INSERT INTO yeoljeong_purchase_orders" in query:
            row = {
                "id": self.store.next_id("po"),
                "company_id": params[0], "business_id": params[1], "branch_id": params[2],
                "order_date": params[3] or "2026-09-18", "supplier": params[4],
                "supplier_type": params[5], "status": params[6], "total_amount": params[7],
                "items": params[8], "memo": params[9],
                "received_at": None, "received_by": "", "received_items": "[]",
                "invoice_number": "",
            }
            self.store.orders.append(row)
        elif "INSERT INTO yeoljeong_stock_movements" in query:
            if "'in'" in query:
                movement_type, rest = "in", params[3:]
            elif "'stocktake'" in query:
                movement_type, rest = "stocktake", params[3:]
            else:
                movement_type, rest = params[3], params[4:]
            row = {
                "id": self.store.next_id("sm"),
                "item_id": params[0], "company_id": params[1], "business_id": params[2],
                "movement_type": movement_type, "quantity": rest[0],
                "reference_id": rest[1], "memo": rest[2], "created_by": rest[3],
            }
            self.store.movements.append(row)
        elif "INSERT INTO yeoljeong_stock_balances" in query:
            row = {
                "id": self.store.next_id("sb"),
                "company_id": params[0], "business_id": params[1], "branch_id": params[2],
                "item_id": params[3], "item_name": params[4], "unit": params[5],
                "system_quantity": params[6], "counted_quantity": params[7],
                "difference": params[8], "counted_by": params[9], "memo": params[10],
                "counted_at": "2026-09-18T10:00:00+09:00",
            }
            self.store.balances.append(row)
        else:
            raise AssertionError(f"알 수 없는 INSERT: {query}")
        return dict(row)

    def _update(self, query: str, params: tuple) -> Optional[dict[str, Any]]:
        scope = _SCOPED_WHERE.search(query)
        assert scope, f"회사 범위 없는 UPDATE: {query}"
        row_id = params[int(scope.group(1)) - 1]
        company_id = params[int(scope.group(2)) - 1]
        rows = self.store.rows_for(query)
        target = next(
            (row for row in rows
             if row["id"] == row_id and str(row["company_id"]) == str(company_id)),
            None,
        )
        if target is None:
            return None

        if "current_stock = current_stock + $1" in query:
            target["current_stock"] = Decimal(str(target["current_stock"])) + params[0]
        elif "SET current_stock = $1" in query:
            target["current_stock"] = params[0]
        elif "SET status = 'received'" in query:
            target.update({
                "status": "received",
                "received_at": "2026-09-18T10:00:00+09:00",
                "received_by": params[0] or target["received_by"],
                "received_items": params[1],
                "invoice_number": params[2] or target["invoice_number"],
                "memo": params[3] or target["memo"],
            })
        else:
            set_part = query.split("SET", 1)[1].split("WHERE", 1)[0]
            for column, index in _EQUALITY.findall(set_part):
                target[column] = params[int(index) - 1]
        return dict(target)


@pytest.fixture()
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()

    async def _get_conn():
        return FakeConn(fake)

    monkeypatch.setattr(service, "_get_conn", _get_conn)
    return fake


def _client(user: dict[str, Any]) -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


COMPANY_A = {"tenant_id": "company-a", "email": "owner@a.test", "user_id": "u-a"}
COMPANY_B = {"tenant_id": "company-b", "email": "owner@b.test", "user_id": "u-b"}


async def _seed_item(company: str, name: str = "국내산 사골", stock: str = "10") -> dict[str, Any]:
    return await service.create_item(
        company,
        {"name": name, "unit": "kg", "current_stock": Decimal(stock), "min_stock": Decimal("5")},
    )


# ---------------------------------------------------------------------------
# 1) 요청이 보낸 company_id 는 무시되고 인증 회사로 강제된다
# ---------------------------------------------------------------------------

def test_request_body_company_id_is_ignored_on_create(store) -> None:
    client = _client(COMPANY_A)

    response = client.post(
        "/api/v1/yeoljeong-inventory/items",
        json={"name": "위조 시도 품목", "company_id": "company-b", "companyId": "company-b"},
    )

    assert response.status_code == 200
    assert response.json()["item"]["company_id"] == "company-a"
    assert [row["company_id"] for row in store.items] == ["company-a"]


def test_listing_never_leaks_another_company(store) -> None:
    client_a, client_b = _client(COMPANY_A), _client(COMPANY_B)
    client_a.post("/api/v1/yeoljeong-inventory/items", json={"name": "A사 사골"})
    client_b.post("/api/v1/yeoljeong-inventory/items", json={"name": "B사 대파"})

    names_a = [item["name"] for item in client_a.get("/api/v1/yeoljeong-inventory/items").json()["items"]]
    names_b = [item["name"] for item in client_b.get("/api/v1/yeoljeong-inventory/items").json()["items"]]

    assert names_a == ["A사 사골"]
    assert names_b == ["B사 대파"]


def test_unowned_business_scope_is_forbidden(store) -> None:
    client = _client(COMPANY_A)

    response = client.get(
        "/api/v1/yeoljeong-inventory/items?business_id=company-b"
    )

    assert response.status_code == 403
    assert "사업자 범위" in response.json()["detail"]


def test_other_company_item_is_not_reachable_by_id(store) -> None:
    client_a, client_b = _client(COMPANY_A), _client(COMPANY_B)
    item_id = client_a.post("/api/v1/yeoljeong-inventory/items", json={"name": "A사 사골"}).json()["item"]["id"]

    assert client_b.get("/api/v1/yeoljeong-inventory/items").json()["count"] == 0
    assert client_b.patch(
        f"/api/v1/yeoljeong-inventory/items/{item_id}", json={"name": "탈취"}
    ).status_code == 404
    assert client_b.delete(f"/api/v1/yeoljeong-inventory/items/{item_id}").status_code == 404
    assert store.items[0]["name"] == "A사 사골"


def test_account_without_company_is_forbidden(store) -> None:
    client = _client({"tenant_id": "", "email": "nobody@test"})

    response = client.get("/api/v1/yeoljeong-inventory/items")

    assert response.status_code == 403
    assert "매입·재고" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 2) 발주 → 입고 → 재고 차감
# ---------------------------------------------------------------------------

async def test_order_receive_adds_stock_then_usage_subtracts(store) -> None:
    item = await _seed_item("company-a", stock="10")

    order = await service.create_order(
        "company-a",
        {
            "supplier": "마켓봄",
            "status": "ordered",
            "total_amount": Decimal("120000"),
            "items": [{"item_id": item["id"], "quantity": "20"}],
        },
    )
    assert order["company_id"] == "company-a"

    received, reason = await service.receive_order(
        "company-a", order["id"], invoice_number="INV-1", actor="owner@a.test"
    )
    assert reason == "received"
    assert received["status"] == "received"
    assert received["received_by"] == "owner@a.test"
    assert json.loads(received["received_items"]) == [{"item_id": item["id"], "quantity": "20"}]

    after_receive = await service.get_item("company-a", item["id"])
    assert Decimal(str(after_receive["current_stock"])) == Decimal("30")

    used = await service.adjust_stock(
        "company-a", item["id"], movement_type="out", quantity="12", actor="owner@a.test"
    )
    assert Decimal(str(used["current_stock"])) == Decimal("18")

    kinds = [(row["movement_type"], str(row["quantity"]), row["company_id"]) for row in store.movements]
    assert kinds == [("in", "20", "company-a"), ("out", "12", "company-a")]


async def test_receive_skips_lines_owned_by_another_company(store) -> None:
    mine = await _seed_item("company-a", name="A사 사골", stock="0")
    theirs = await _seed_item("company-b", name="B사 사골", stock="0")

    order = await service.create_order(
        "company-a",
        {"items": [
            {"item_id": mine["id"], "quantity": "5"},
            {"item_id": theirs["id"], "quantity": "999"},
        ]},
    )
    received, reason = await service.receive_order("company-a", order["id"])

    assert reason == "received"
    assert json.loads(received["received_items"]) == [{"item_id": mine["id"], "quantity": "5"}]
    assert Decimal(str((await service.get_item("company-a", mine["id"]))["current_stock"])) == Decimal("5")
    assert Decimal(str((await service.get_item("company-b", theirs["id"]))["current_stock"])) == Decimal("0")


async def test_receive_of_another_companys_order_is_not_found(store) -> None:
    item = await _seed_item("company-a")
    order = await service.create_order(
        "company-a", {"items": [{"item_id": item["id"], "quantity": "3"}]}
    )

    assert await service.receive_order("company-b", order["id"]) == (None, "not_found")
    assert Decimal(str((await service.get_item("company-a", item["id"]))["current_stock"])) == Decimal("10")


# ---------------------------------------------------------------------------
# 3) 재고 실사
# ---------------------------------------------------------------------------

async def test_stocktake_records_counted_quantity_and_actor(store) -> None:
    item = await _seed_item("company-a", stock="10")

    result, reason = await service.record_stocktake(
        "company-a", item["id"], counted_quantity="7.5",
        counted_by="manager@a.test", memo="주간 실사",
    )

    assert reason == "recorded"
    balance = result["balance"]
    assert balance["company_id"] == "company-a"
    assert Decimal(str(balance["system_quantity"])) == Decimal("10")
    assert Decimal(str(balance["counted_quantity"])) == Decimal("7.5")
    assert Decimal(str(balance["difference"])) == Decimal("-2.5")
    assert balance["counted_by"] == "manager@a.test"
    assert balance["memo"] == "주간 실사"

    # 장부 수량이 실사 수량과 일치한다
    assert Decimal(str(result["item"]["current_stock"])) == Decimal("7.5")
    assert Decimal(str((await service.get_item("company-a", item["id"]))["current_stock"])) == Decimal("7.5")

    # 차이는 수불로도 남아 추적된다
    adjustments = [row for row in store.movements if row["movement_type"] == "stocktake"]
    assert len(adjustments) == 1
    assert Decimal(str(adjustments[0]["quantity"])) == Decimal("-2.5")
    assert adjustments[0]["created_by"] == "manager@a.test"
    assert adjustments[0]["company_id"] == "company-a"


async def test_matching_stocktake_leaves_no_adjustment_movement(store) -> None:
    item = await _seed_item("company-a", stock="10")

    result, _ = await service.record_stocktake("company-a", item["id"], counted_quantity="10")

    assert Decimal(str(result["balance"]["difference"])) == Decimal("0")
    assert [row for row in store.movements if row["movement_type"] == "stocktake"] == []


async def test_negative_stocktake_is_rejected(store) -> None:
    item = await _seed_item("company-a", stock="10")

    with pytest.raises(ValueError):
        await service.record_stocktake("company-a", item["id"], counted_quantity="-1")

    assert store.balances == []
    assert Decimal(str((await service.get_item("company-a", item["id"]))["current_stock"])) == Decimal("10")


async def test_stocktake_on_another_companys_item_is_not_found(store) -> None:
    item = await _seed_item("company-a")

    assert await service.record_stocktake("company-b", item["id"], counted_quantity="1") == (None, "not_found")
    assert store.balances == []


def test_stocktake_api_rejects_negative_and_records_actor(store) -> None:
    client = _client(COMPANY_A)
    item_id = client.post(
        "/api/v1/yeoljeong-inventory/items",
        json={"name": "사골", "current_stock": 10},
    ).json()["item"]["id"]

    rejected = client.post(
        f"/api/v1/yeoljeong-inventory/items/{item_id}/stocktake",
        json={"counted_quantity": -1},
    )
    assert rejected.status_code == 400
    assert store.balances == []

    accepted = client.post(
        f"/api/v1/yeoljeong-inventory/items/{item_id}/stocktake",
        json={"counted_quantity": "8", "memo": "야간 실사", "company_id": "company-b"},
    )
    assert accepted.status_code == 200
    balance = accepted.json()["balance"]
    assert balance["counted_by"] == "owner@a.test"
    assert balance["company_id"] == "company-a"
    assert Decimal(str(balance["counted_quantity"])) == Decimal("8")

    listed = client.get("/api/v1/yeoljeong-inventory/stock-balances").json()
    assert listed["count"] == 1
    assert listed["stock_balances"][0]["counted_by"] == "owner@a.test"


def test_stock_balances_are_scoped_to_the_authenticated_company(store) -> None:
    client_a, client_b = _client(COMPANY_A), _client(COMPANY_B)
    item_id = client_a.post("/api/v1/yeoljeong-inventory/items", json={"name": "사골"}).json()["item"]["id"]
    client_a.post(
        f"/api/v1/yeoljeong-inventory/items/{item_id}/stocktake", json={"counted_quantity": 3}
    )

    assert client_b.get("/api/v1/yeoljeong-inventory/stock-balances").json()["count"] == 0
    assert client_a.get("/api/v1/yeoljeong-inventory/stock-balances").json()["count"] == 1


# ---------------------------------------------------------------------------
# 4) 수량 파싱 / 계약
# ---------------------------------------------------------------------------

def test_adjust_rejects_zero_and_non_numeric_quantity(store) -> None:
    client = _client(COMPANY_A)
    item_id = client.post("/api/v1/yeoljeong-inventory/items", json={"name": "사골"}).json()["item"]["id"]
    path = f"/api/v1/yeoljeong-inventory/items/{item_id}/adjust"

    assert client.post(path, json={"movement_type": "out", "quantity": 0}).status_code == 400
    assert client.post(path, json={"movement_type": "out", "quantity": -3}).status_code == 400
    assert client.post(path, json={"movement_type": "out", "quantity": "열개"}).status_code == 400
    assert client.post(path, json={"movement_type": "out"}).status_code == 400
    assert store.movements == []


def test_decimal_quantities_keep_exact_values(store) -> None:
    assert service.to_decimal(0.1) == Decimal("0.1")
    assert service.to_decimal("2.05") == Decimal("2.05")
    with pytest.raises(ValueError):
        service.to_decimal("열개")


def test_public_service_functions_take_company_id_first() -> None:
    import inspect

    scoped = [
        "get_item", "create_item", "update_item", "delete_item", "low_stock_items",
        "list_items", "get_order", "create_order", "update_order", "delete_order",
        "list_orders", "adjust_stock", "receive_order", "list_movements",
        "record_stocktake", "list_stock_balances",
    ]
    for name in scoped:
        function = getattr(service, name)
        first = list(inspect.signature(function).parameters)[0]
        assert first == "company_id", f"{name} 의 첫 인자가 company_id 가 아니다: {first}"


def test_inventory_router_is_served_by_the_obys_app() -> None:
    from app import yeoljeong_main

    paths = {getattr(route, "path", "") for route in yeoljeong_main.app.routes}

    assert "/api/v1/yeoljeong-inventory/items" in paths
    assert "/api/v1/yeoljeong-inventory/stock-balances" in paths
    assert "/api/v1/yeoljeong-inventory/items/{item_id}/stocktake" in paths


def test_migration_extends_existing_tables_and_adds_balances() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for table in ("yeoljeong_inventory_items", "yeoljeong_purchase_orders", "yeoljeong_stock_movements"):
        assert f"ALTER TABLE {table}\n    ADD COLUMN IF NOT EXISTS company_id" in sql
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in sql, f"{table} 은 신설이 아니라 확장이어야 한다"
    assert "CREATE TABLE IF NOT EXISTS yeoljeong_stock_balances" in sql
    assert "counted_by" in sql and "counted_quantity" in sql and "difference" in sql


def test_obys_screen_uses_purchase_inventory_wording() -> None:
    html = (REPO_ROOT / "app/static/apps/obys/index.html").read_text(encoding="utf-8")
    js = (REPO_ROOT / "app/static/apps/obys/modules/store-assistant-v2.js").read_text(encoding="utf-8")
    css = (REPO_ROOT / "app/static/apps/obys/modules/store-assistant-v2.css").read_text(encoding="utf-8")

    assert '<button class="tab" data-view="inventory">매입·재고</button>' in html
    assert "매장비서" not in html and "매장비서" not in js
    assert "오비서" in html
    # 모바일 한 손 조작: 주요 버튼 52px, 작성 중 발주 보관, 세션 만료 복구
    assert "min-height: 52px" in css
    assert "recoverExpiredSession" in js
    assert "obys_inventory_order_draft" in js
    assert 'id="stockBalanceRows"' in html
    assert 'id="inventoryStocktakeForm"' in html
    assert "loadInventoryData" in html
    assert "inventoryApi(\"/orders\"" in html
    assert "data-receive-order" in html
    assert "inventoryDraftKey" in html
    assert "salesDataState" in html
