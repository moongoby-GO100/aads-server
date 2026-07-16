"""API routes for the Yeoljeong store assistant app."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services import yeoljeong_finance_service as svc

router = APIRouter(prefix="/yeoljeong-finance", tags=["yeoljeong-finance"])


class GenericPayload(BaseModel):
    model_config = {"extra": "allow"}


class InviteCreate(BaseModel):
    phone: str = ""
    name: str = ""
    branch: str = ""
    role: str = "member"
    expires_in_hours: int = 72
    memo: str = ""


class InviteAccept(BaseModel):
    token: str
    name: str = ""
    phone: str = ""
    branch: str = ""
    memo: str = ""


class JoinRequestCreate(BaseModel):
    name: str
    email: str = ""
    branch: str = ""
    phone: str = ""
    memo: str = ""


class ReviewPayload(BaseModel):
    action: str = Field(default="approved")
    memo: str = ""


class DocumentReviewPayload(BaseModel):
    status: str = "approved"
    memo: str = ""


class ContractSignPayload(BaseModel):
    token: str
    signer_name: str = ""
    signer_email: str = ""


class SyncPayload(BaseModel):
    services: list[str] = Field(default_factory=list)


class CsvImportPayload(BaseModel):
    csv_text: str = ""


@router.get("/session")
async def get_session(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return svc.session_for_user(current_user)


@router.get("/onboarding/document-types")
async def list_document_types(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"document_types": svc.list_document_types()}


@router.get("/employees/invites")
async def list_employee_invites(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"invites": svc.list_invites(current_user)}


@router.post("/employees/invites")
async def create_employee_invite(payload: InviteCreate, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"invite": svc.create_invite(payload.model_dump(), current_user)}


@router.get("/employees/invites/resolve")
async def resolve_employee_invite(token: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"invite": svc.resolve_invite(token)}


@router.post("/employees/invites/accept")
async def accept_employee_invite(payload: InviteAccept, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"request": svc.accept_invite(payload.model_dump(), current_user)}


@router.get("/employees/join-requests")
async def list_employee_join_requests(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"requests": svc.list_join_requests(current_user)}


@router.post("/employees/join-requests")
async def create_employee_join_request(payload: JoinRequestCreate, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"request": svc.upsert_join_request(payload.model_dump(), current_user)}


@router.patch("/employees/join-requests/{request_id}")
async def review_employee_join_request(request_id: str, payload: ReviewPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"request": svc.review_join_request(request_id, payload.action, payload.memo, current_user)}


@router.get("/employees/approved")
async def list_approved_employees(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"employees": svc.list_approved_employees(current_user)}


@router.get("/onboarding/documents")
async def list_onboarding_documents(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"documents": svc.list_onboarding_documents(current_user)}


@router.post("/onboarding/documents")
async def upload_onboarding_document(
    employee_name: str = Form(...),
    employee_email: str = Form(...),
    branch: str = Form(""),
    document_type: str = Form(...),
    issue_date: str = Form(""),
    memo: str = Form(""),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    document = await svc.save_onboarding_document(
        employee_name=employee_name,
        employee_email=employee_email,
        branch=branch,
        document_type=document_type,
        issue_date=issue_date,
        memo=memo,
        upload=file,
        user=current_user,
    )
    return {"document": document}


@router.get("/onboarding/documents/{document_id}/download")
async def download_onboarding_document(document_id: str, current_user: dict = Depends(get_current_user)) -> FileResponse:
    document, path = svc.get_onboarding_document(document_id, current_user)
    return FileResponse(path, media_type=document.get("content_type") or "application/octet-stream", filename=document.get("original_filename") or path.name)


@router.patch("/onboarding/documents/{document_id}/review")
async def review_onboarding_document(document_id: str, payload: DocumentReviewPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"document": svc.review_onboarding_document(document_id, payload.status, payload.memo, current_user)}


@router.delete("/onboarding/documents/{document_id}")
async def delete_onboarding_document(document_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, bool]:
    svc.delete_onboarding_document(document_id, current_user)
    return {"ok": True}


@router.get("/contracts")
async def list_contracts(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contracts": svc.list_contracts(current_user)}


@router.post("/contracts")
async def save_contract(payload: GenericPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contract": svc.save_contract(payload.model_dump(), current_user)}


@router.post("/contracts/{contract_id}/request-signature")
async def request_contract_signature(contract_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contract": svc.request_contract_signature(contract_id, current_user)}


@router.get("/contracts/signing/{token}")
async def get_contract_signing(token: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contract": svc.get_contract_by_token(token)}


@router.post("/contracts/signing")
async def sign_contract(payload: ContractSignPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contract": svc.sign_contract(payload.model_dump(), current_user)}


@router.delete("/contracts/{contract_id}")
async def delete_contract(contract_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, bool]:
    svc.delete_contract(contract_id, current_user)
    return {"ok": True}


@router.get("/payroll")
async def list_payroll(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"statements": svc.list_payroll(current_user)}


@router.post("/payroll")
async def save_payroll(payload: GenericPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"statement": svc.save_payroll(payload.model_dump(), current_user)}


@router.delete("/payroll/{statement_id}")
async def delete_payroll(statement_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, bool]:
    svc.delete_payroll(statement_id, current_user)
    return {"ok": True}


@router.get("/accounts")
async def list_accounts(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"accounts": svc.list_accounts(current_user)}


@router.get("/settings")
async def get_settings(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await svc.get_settings_persisted(current_user)


@router.put("/settings")
async def save_settings(payload: GenericPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await svc.save_settings_persisted(payload.model_dump(), current_user)


@router.post("/accounts")
async def upsert_account(payload: GenericPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    account = svc.upsert_account(payload.model_dump(), current_user)
    return {"account": account}


@router.get("/settlements")
async def list_settlements(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"settlements": svc.list_settlements(current_user, business_id)}


@router.get("/sales")
async def list_sales(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"sales": svc.list_sales(current_user, business_id)}


@router.get("/reviews")
async def list_reviews(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"reviews": svc.list_reviews(current_user, business_id)}


@router.get("/collection-status")
async def list_collection_status(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"statuses": svc.list_collection_status(current_user)}


@router.get("/automation")
async def get_automation_status(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return svc.automation_status(current_user)


@router.post("/sync")
async def sync_delivery(payload: SyncPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return svc.sync_delivery(payload.model_dump(), current_user)


@router.post("/settlements/import")
async def import_settlements(payload: CsvImportPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return svc.import_settlement_csv(payload.csv_text, current_user)
