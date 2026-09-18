"""열정국밥 재고·발주 API 라우터.

회사 경계: 모든 엔드포인트는 `_company_id(user)` 가 인증 토큰에서 뽑은 회사 id
하나로만 동작한다. 쿼리스트링·요청 본문이 보낸 company_id 는 **읽지 않는다** —
읽는 순간 남의 회사 재고를 조회·수정할 수 있는 위조 경로가 된다.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_current_user
from app.services import yeoljeong_inventory_service as service

router = APIRouter(prefix="/yeoljeong-inventory", tags=["yeoljeong-inventory"])

#: 요청 본문에서 무조건 떼어내는 키 — 회사 귀속은 토큰만이 정한다.
_FORGEABLE_FIELDS = ("company_id", "companyId")


def _company_id(user: dict[str, Any]) -> str:
    """인증 사용자의 tenant_id 를 회사 id 로 쓴다. 없으면 403."""
    company_id = str((user or {}).get("tenant_id") or "").strip()
    if not company_id:
        raise HTTPException(
            status_code=403, detail="회사에 소속되지 않은 계정은 매입·재고를 이용할 수 없습니다."
        )
    return company_id


def _clean_payload(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    """요청 본문에서 위조 가능한 회사 필드를 제거한 사본을 돌려준다."""
    data = dict(payload or {})
    for field in _FORGEABLE_FIELDS:
        data.pop(field, None)
    return data


def _quantity(value: Any, field: str = "quantity", *, allow_zero: bool = False) -> Decimal:
    """수량을 Decimal 로 파싱한다. 형식이 틀리거나 범위를 벗어나면 400."""
    if value is None or value == "":
        raise HTTPException(status_code=400, detail=f"{field} is required")
    try:
        quantity = service.to_decimal(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be a number") from None
    if allow_zero:
        if quantity < 0:
            raise HTTPException(status_code=400, detail=f"{field} must be zero or greater")
    elif quantity <= 0:
        raise HTTPException(status_code=400, detail=f"{field} must be greater than zero")
    return quantity


def _actor(user: dict[str, Any]) -> str:
    return str((user or {}).get("email") or (user or {}).get("user_id") or "")


# ---------------------------------------------------------------------------
# Inventory items
# ---------------------------------------------------------------------------

@router.get("/items")
async def get_items(
    business_id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    low_stock: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    items = await service.list_items(
        _company_id(user),
        category=category,
        low_stock=low_stock,
        business_id=business_id,
    )
    return {"items": items, "count": len(items)}


@router.post("/items")
async def post_item(
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    data = _clean_payload(payload)
    if not data.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    item = await service.create_item(_company_id(user), data)
    return {"item": item}


@router.patch("/items/{item_id}")
async def patch_item(
    item_id: str,
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    company_id = _company_id(user)
    existing = await service.get_item(company_id, item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="item not found")
    item = await service.update_item(company_id, item_id, _clean_payload(payload))
    return {"item": item}


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    user: dict = Depends(get_current_user),
):
    company_id = _company_id(user)
    existing = await service.get_item(company_id, item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="item not found")
    ok = await service.delete_item(company_id, item_id)
    return {"deleted": ok}


@router.post("/items/{item_id}/adjust")
async def post_item_adjust(
    item_id: str,
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    data = _clean_payload(payload)
    movement_type = data.get("movement_type")
    if movement_type not in ("in", "out", "waste"):
        raise HTTPException(
            status_code=400, detail="movement_type must be one of in/out/waste"
        )
    quantity = _quantity(data.get("quantity"))

    item = await service.adjust_stock(
        _company_id(user),
        item_id,
        movement_type=movement_type,
        quantity=quantity,
        memo=data.get("memo", ""),
        reference_id=data.get("reference_id", ""),
        actor=_actor(user),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return {"item": item}


@router.post("/items/{item_id}/stocktake")
async def post_item_stocktake(
    item_id: str,
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    """재고 실사: 센 수량을 그대로 장부에 반영하고 차이를 수불로 남긴다."""
    data = _clean_payload(payload)
    counted = _quantity(
        data.get("counted_quantity", data.get("quantity")),
        "counted_quantity",
        allow_zero=True,
    )

    try:
        result, reason = await service.record_stocktake(
            _company_id(user),
            item_id,
            counted_quantity=counted,
            counted_by=_actor(user),
            memo=data.get("memo", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if result is None:
        raise HTTPException(status_code=404, detail="item not found")
    return {
        "balance": result["balance"],
        "item": result["item"],
        "status": reason,
    }


@router.get("/low-stock")
async def get_low_stock(
    business_id: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    items = await service.low_stock_items(_company_id(user), business_id=business_id)
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

@router.get("/orders")
async def get_orders(
    business_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    orders = await service.list_orders(
        _company_id(user),
        status=status,
        date_from=date_from,
        date_to=date_to,
        business_id=business_id,
    )
    return {"orders": orders, "count": len(orders)}


@router.post("/orders")
async def post_order(
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    data = _clean_payload(payload)
    for index, line in enumerate(data.get("items") or []):
        if isinstance(line, dict) and line.get("quantity") is not None:
            line["quantity"] = str(_quantity(line.get("quantity"), f"items[{index}].quantity"))
    order = await service.create_order(_company_id(user), data)
    return {"order": order}


@router.patch("/orders/{order_id}")
async def patch_order(
    order_id: str,
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    company_id = _company_id(user)
    existing = await service.get_order(company_id, order_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="order not found")
    order = await service.update_order(company_id, order_id, _clean_payload(payload))
    return {"order": order}


@router.post("/orders/{order_id}/receive")
async def post_order_receive(
    order_id: str,
    payload: dict[str, Any] = None,
    user: dict = Depends(get_current_user),
):
    data = _clean_payload(payload)
    order, reason = await service.receive_order(
        _company_id(user),
        order_id,
        invoice_number=data.get("invoice_number", ""),
        memo=data.get("memo", ""),
        actor=_actor(user),
    )
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order": order, "status": reason}


@router.delete("/orders/{order_id}")
async def delete_order(
    order_id: str,
    user: dict = Depends(get_current_user),
):
    ok, reason = await service.delete_order(_company_id(user), order_id)
    if not ok:
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="order not found")
        raise HTTPException(
            status_code=400, detail="only draft orders can be deleted"
        )
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Stock movements / balances
# ---------------------------------------------------------------------------

@router.get("/movements")
async def get_movements(
    item_id: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    movements = await service.list_movements(
        _company_id(user), item_id=item_id, date_from=date_from, date_to=date_to
    )
    return {"movements": movements, "count": len(movements)}


@router.get("/stock-balances")
async def get_stock_balances(
    item_id: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(get_current_user),
):
    """재고 실사 스냅샷 목록 — 실사 수량과 실행자 기록을 그대로 돌려준다."""
    balances = await service.list_stock_balances(
        _company_id(user),
        item_id=item_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    return {"stock_balances": balances, "count": len(balances)}
