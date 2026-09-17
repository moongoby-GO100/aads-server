"""OHVIS app notification endpoints."""
from __future__ import annotations

from typing import Any, Literal

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


class SessionRelayNotification(BaseModel):
    """외부 시스템이 담당 세션에 넣는 알림.

    2026-09-17 CEO 지시로 텔레그램을 알림에서 뺀다. 빼기만 하면 알림이
    사라지므로 받을 곳을 먼저 만든다 — 담당 세션이 그 자리다. 사람이 읽고
    다시 전달하는 단계가 없어지고, 알림을 받은 담당이 바로 조치한다.
    """

    target_role: str = Field(..., min_length=1, max_length=80,
                             description="담당 role_key (DataEngineOwner 등). 한글 별칭·세션 제목도 받는다.")
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field("", max_length=20000)
    project: str = Field("", max_length=32, description="chat_workspaces.project_key (GO100 등). 비우면 테넌트 전체에서 찾는다.")
    severity: Literal["info", "warn", "critical"] = "info"
    dedup_key: str = Field("", max_length=200,
                           description="비우면 역할·제목·본문·프로젝트 해시로 서버가 만든다.")
    source: str = Field("", max_length=120, description="보낸 주체(예: go100-cron@contabo14). 로그·본문에 남는다.")


@router.post("/relay")
async def relay_notification_to_session(
    body: SessionRelayNotification,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """알림을 담당 세션 대기열에 넣는다.

    인증은 기존 테넌트 인증 그대로다(`get_current_user`). cron 처럼 사람이
    없는 호출은 이미 있는 서비스 경로를 쓴다 — `x-monitor-key: $AADS_MONITOR_KEY`
    를 헤더에 넣으면 internal 테넌트로 인증된다. **새 시크릿을 만들지 않는다**
    (R-KEY).

    즉시 배달하지 않는다. 대상이 응답 중일 때 밀어 넣으면 그 응답이 통째로
    버려진다 — `queued` 로 넣고 배달기가 대상이 한가해질 때 전달한다.
    """
    from app.services import session_relay

    tenant_id = current_user.get("tenant_id")
    result = await session_relay.notify(
        body.target_role,
        body.title,
        body.body,
        tenant_id=str(tenant_id) if tenant_id else "",
        project=body.project,
        severity=body.severity,
        dedup_key=body.dedup_key,
        source=body.source or str(current_user.get("user_id") or ""),
    )

    if result.get("queued"):
        return {"ok": True, **result}

    error = result.get("error") or "unknown"
    # 중복은 실패가 아니다. 5분마다 도는 cron 에 4xx 를 주면 그 cron 은
    # 알림이 안 갔다고 판단해 재시도하거나 자기 로그를 오류로 채운다.
    if error == "duplicate":
        return {"ok": True, **result}
    if error == "target_not_found":
        raise HTTPException(status_code=404, detail=result.get("message") or "담당 세션을 찾지 못했습니다.")
    raise HTTPException(status_code=400, detail=result.get("message") or error)
