"""OHVIS app notification endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services import push_notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


class PushSubscriptionRequest(BaseModel):
    endpoint: str
    expirationTime: int | None = None
    keys: dict[str, str] = Field(default_factory=dict)


class PushSubscriptionBody(BaseModel):
    subscription: PushSubscriptionRequest


@router.get("/vapid-public-key")
async def get_vapid_public_key() -> dict[str, Any]:
    public_key = push_notifications.vapid_public_key()
    return {
        "configured": bool(public_key and push_notifications.vapid_private_key()),
        "public_key": public_key,
    }


@router.post("/push-subscriptions", status_code=201)
async def save_push_subscription(
    body: PushSubscriptionBody,
    user_agent: str = Header("", alias="User-Agent"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        saved = await push_notifications.upsert_subscription(
            current_user=current_user,
            subscription=body.subscription.model_dump(by_alias=True),
            user_agent=user_agent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "subscription": saved}


@router.delete("/push-subscriptions")
async def delete_push_subscription(
    body: PushSubscriptionBody,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    endpoint = body.subscription.endpoint
    disabled = await push_notifications.disable_subscription(
        current_user=current_user,
        endpoint=endpoint,
    )
    return {"ok": True, "disabled": disabled}


@router.post("/push-test")
async def send_push_test(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    result = await push_notifications.send_web_push_to_user(
        tenant_id=current_user.get("tenant_id"),
        user_id=str(current_user.get("user_id")),
        payload={
            "title": "오비스",
            "body": "앱 알림이 연결되었습니다.",
            "url": "/chat",
            "tag": "ohvis-push-test",
            "data": {"event": "push_test"},
        },
    )
    return {"ok": True, "result": result}
