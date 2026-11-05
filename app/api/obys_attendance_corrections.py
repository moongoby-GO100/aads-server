"""로그인 직원 본인 신청 및 관리자 정정 승인 API."""
from fastapi import APIRouter, Depends
from app.auth import get_current_user
from app.services import obys_attendance_corrections as svc

router = APIRouter(prefix="/workspaces/{business_id}/attendance", tags=["attendance-corrections"])


@router.get("/corrections")
async def list_corrections(business_id: str, user=Depends(get_current_user)):
    return await svc.run(user, business_id, "list")


@router.post("/corrections", status_code=201)
async def create_new_entry(business_id: str, payload: svc.NewEntryIn, user=Depends(get_current_user)):
    """직원 본인의 신규 근무 신청. 신청자는 본문이 아니라 로그인 계정으로만 정해진다."""
    return await svc.run(user, business_id, "create_new", payload=payload)


@router.post("/records/{record_id}/corrections", status_code=201)
async def create_correction(business_id: str, record_id: str, payload: svc.CorrectionIn, user=Depends(get_current_user)):
    return await svc.run(user, business_id, "create", record_id=record_id, payload=payload)


@router.post("/corrections/{correction_id}/approve")
async def approve(business_id: str, correction_id: str, payload: svc.DecisionIn, user=Depends(get_current_user)):
    return await svc.run(user, business_id, "approve", correction_id=correction_id, payload=payload)


@router.post("/corrections/{correction_id}/reject")
async def reject(business_id: str, correction_id: str, payload: svc.DecisionIn, user=Depends(get_current_user)):
    return await svc.run(user, business_id, "reject", correction_id=correction_id, payload=payload)


@router.post("/corrections/{correction_id}/withdraw")
async def withdraw(business_id: str, correction_id: str, payload: svc.DecisionIn, user=Depends(get_current_user)):
    return await svc.run(user, business_id, "withdraw", correction_id=correction_id, payload=payload)
