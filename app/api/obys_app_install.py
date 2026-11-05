"""직원 앱 설치·접속 기록(본인 ping)과 관리자 현황 조회."""
from fastapi import APIRouter, Depends, Request
from app.auth import get_current_user
from app.services import obys_app_install as svc

router = APIRouter(prefix="/obys/{business_id}/app-install", tags=["obys-app-install"])


@router.post("/ping")
async def ping(business_id: str, payload: svc.PingIn, request: Request, user=Depends(get_current_user)):
    return await svc.ping(user, business_id, payload, request.headers.get("user-agent"))


@router.get("/status")
async def status(business_id: str, user=Depends(get_current_user)):
    return await svc.status(user, business_id)
