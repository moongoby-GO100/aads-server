"""열정국밥 재고·발주 API 라우터."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_current_user
from app.services import yeoljeong_inventory_service as service

router = APIRouter(prefix="/yeoljeong-inventory", tags=["yeoljeong-inventory"])


# ---------------------------------------------------------------------------
# Inventory items
# ---------------------------------------------------------------------------

@router.get("/items")
async def get_items(
    business_id: str = Query("biz-mia"),
    category: Optional[str] = Query(None),
    low_stock: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    items = await service.list_items(
        business_id=business_id, category=category, low_stock=low_stock
    )
    return {"items": items, "count": len(items)}


@router.post("/items")
async def post_item(
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    if not payload.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    item = await service.create_item(payload)
    return {"item": item}


@router.patch("/items/{item_id}")
async def patch_item(
    item_id: str,
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    existing = await service.get_item(item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="item not found")
    item = await service.update_item(item_id, payload)
    return {"item": item}


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    user: dict = Depends(get_current_user),
):
    existing = await service.get_item(item_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="item not found")
    ok = await service.delete_item(item_id)
    return {"deleted": ok}


@router.post("/items/{item_id}/adjust")
async def post_item_adjust(
    item_id: str,
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    movement_type = payload.get("movement_type")
    quantity = payload.get("quantity")
    if movement_type not in ("in", "out", "waste"):
        raise HTTPException(
            status_code=400, detail="movement_type must be one of in/out/waste"
        )
    if quantity is None:
        raise HTTPException(status_code=400, detail="quantity is required")

    item = await service.adjust_stock(
        item_id,
        movement_type=movement_type,
        quantity=quantity,
        memo=payload.get("memo", ""),
        reference_id=payload.get("reference_id", ""),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return {"item": item}


@router.get("/low-stock")
async def get_low_stock(
    business_id: str = Query("biz-mia"),
    user: dict = Depends(get_current_user),
):
    items = await service.low_stock_items(business_id=business_id)
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------

@router.get("/orders")
async def get_orders(
    business_id: str = Query("biz-mia"),
    status: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    orders = await service.list_orders(
        business_id=business_id, status=status, date_from=date_from, date_to=date_to
    )
    return {"orders": orders, "count": len(orders)}


@router.post("/orders")
async def post_order(
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    order = await service.create_order(payload)
    return {"order": order}


@router.patch("/orders/{order_id}")
async def patch_order(
    order_id: str,
    payload: dict[str, Any],
    user: dict = Depends(get_current_user),
):
    existing = await service.get_order(order_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="order not found")
    order = await service.update_order(order_id, payload)
    return {"order": order}


@router.post("/orders/{order_id}/receive")
async def post_order_receive(
    order_id: str,
    payload: dict[str, Any] = None,
    user: dict = Depends(get_current_user),
):
    payload = payload or {}
    order, reason = await service.receive_order(
        order_id,
        invoice_number=payload.get("invoice_number", ""),
        memo=payload.get("memo", ""),
    )
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order": order, "status": reason}


@router.delete("/orders/{order_id}")
async def delete_order(
    order_id: str,
    user: dict = Depends(get_current_user),
):
    ok, reason = await service.delete_order(order_id)
    if not ok:
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="order not found")
        raise HTTPException(
            status_code=400, detail="only draft orders can be deleted"
        )
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Stock movements
# ---------------------------------------------------------------------------

@router.get("/movements")
async def get_movements(
    item_id: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    movements = await service.list_movements(
        item_id=item_id, date_from=date_from, date_to=date_to
    )
    return {"movements": movements, "count": len(movements)}
