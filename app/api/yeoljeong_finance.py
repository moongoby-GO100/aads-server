"""API routes for the Yeoljeong store assistant app."""
from __future__ import annotations

import logging
import threading
from functools import partial
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services import yeoljeong_finance_service as svc

router = APIRouter(prefix="/yeoljeong-finance", tags=["yeoljeong-finance"])
logger = logging.getLogger(__name__)


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
    address: str = ""
    birth_date: str = ""
    nationality: str = "대한민국"
    memo: str = ""


class ReviewPayload(BaseModel):
    action: str = Field(default="approved")
    memo: str = ""


class EmployeeRoleUpdate(BaseModel):
    role: str
    memo: str = ""


class DocumentReviewPayload(BaseModel):
    status: str = "approved"
    memo: str = ""


class ContractSignPayload(BaseModel):
    model_config = {"extra": "forbid"}
    token: str
    signer_name: str = Field(min_length=1, max_length=100)
    consent: bool
    consent_version: str = Field(default="yeoljeong-contract-sign-v1", max_length=80)
    signature_data_uri: str = Field(min_length=100, max_length=350_000)


class AccountUpsertPayload(BaseModel):
    model_config = {"extra": "forbid"}
    account_id: str = ""
    server_account_id: str = ""
    service: str
    username: str = ""
    password: str = Field(
        default="",
        repr=False,
        json_schema_extra={"writeOnly": True},
    )
    api_key: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    client_secret: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    certificate_password: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    account_no: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    account_password: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    business_registration_no: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    label: str = ""
    login_url: str = ""
    business_id: str = "biz-mia"
    branch: str = "열정국밥_미아점"
    institution_code: str = ""
    account_no_masked: str = ""
    business_registration_no_masked: str = ""
    merchant_no: str = ""
    settlement_cycle: str = ""
    collection_mode: str = "browser-automation"
    category: str = ""
    data_scope: str = ""
    required_proof: str = ""
    auth_owner: str = ""
    mfa_method: str = ""
    credential_expires_at: str = ""
    fallback_auth: str = ""
    sync_scope: str = ""
    permission_scope: str = ""
    failure_fallback: str = ""
    memo: str = ""
    auto_sync: bool = False


class SyncPayload(BaseModel):
    services: list[str] = Field(default_factory=list)
    account_id: str = ""
    server_account_id: str = ""
    business_id: str = "biz-mia"
    branch: str = "열정국밥_미아점"
    date_from: str = ""
    date_to: str = ""
    all_businesses: bool = False
    mode: str = ""
    collection_mode: str = ""
    max_orders: int = Field(default=300, ge=1, le=300)
    max_reviews: int = Field(default=300, ge=1, le=300)
    window_days: int = Field(default=1, ge=1, le=7)
    max_backfill_runs: int = Field(default=1, ge=1, le=4)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    browser_session_id: str = ""
    browser_agent_id: str = ""
    browser_preferred_port: int | None = None
    storage_state_path: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    background: bool = False
    sync_job_id: str = ""
    queued_run_ids: dict[str, str] = Field(default_factory=dict)
    captcha_value: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    captcha_values: dict[str, str] = Field(default_factory=dict, repr=False, json_schema_extra={"writeOnly": True})
    operator_approved: bool = False
    approved_input: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    force_recreate_portal_sessions: bool = False
    force_recreate_bank_browser: bool = False
    force_recreate_browser: bool = False
    close_portal_browser_on_complete: bool = True
    keep_browser_open: bool = False
    require_pc_agent: bool = False
    allow_server_headless_fallback: bool = False
    allowServerHeadlessFallback: bool = False
    auto_open_bank_browser: bool = True
    skip_financial_accounts: bool = False


def _run_delivery_sync_background(payload: dict[str, Any], current_user: dict[str, Any]) -> None:
    try:
        svc.sync_delivery(payload, current_user)
    except Exception as exc:
        logger.exception("yeoljeong_delivery_background_sync_failed: %s", exc)


def _start_delivery_sync_background(payload: dict[str, Any], current_user: dict[str, Any]) -> None:
    worker = threading.Thread(
        target=_run_delivery_sync_background,
        args=(dict(payload), dict(current_user)),
        name=f"yeoljeong-delivery-sync-{str(payload.get('sync_job_id') or 'job')[:24]}",
        daemon=True,
    )
    worker.start()


class CsvImportPayload(BaseModel):
    service: str
    csv_text: str = ""
    filename: str = "settlement.csv"
    business_id: str = "biz-mia"
    branch: str = "열정국밥_미아점"


class DeliveryPortalImportPayload(BaseModel):
    service: str = "baemin"
    record_type: str = "settlements"
    source_text: str
    filename: str = "pc-browser-copy.html"
    business_id: str = "biz-mia"
    branch: str = "열정국밥_미아점"
    date_from: str = ""
    date_to: str = ""


class TransactionCsvImportPayload(BaseModel):
    service: str
    csv_text: str = ""
    filename: str = "transactions.csv"
    business_id: str = "biz-mia"
    branch: str = "열정국밥_미아점"
    source_account_id: str = ""


class BankAccountCreatePayload(BaseModel):
    model_config = {"extra": "forbid"}
    business_id: str
    branch_id: str = ""
    bank_code: str = ""
    bank_name: str = ""
    # 원본 계좌번호는 저장하지 않는다. 마스킹 처리 후 폐기한다.
    account_number: str = Field(default="", repr=False, json_schema_extra={"writeOnly": True})
    account_number_masked: str = ""
    account_holder: str = ""
    account_alias: str = ""
    connection_type: str = "mock"
    connector_type: str = ""
    status: str = "needs_auth"
    institution_code: str = ""
    memo: str = ""
    auto_sync: bool = False
    last_synced_at: str = ""


class BankAccountUpdatePayload(BaseModel):
    model_config = {"extra": "forbid"}
    branch_id: str | None = None
    bank_code: str | None = None
    bank_name: str | None = None
    account_number: str | None = Field(default=None, repr=False, json_schema_extra={"writeOnly": True})
    account_number_masked: str | None = None
    account_holder: str | None = None
    account_alias: str | None = None
    connection_type: str | None = None
    connector_type: str | None = None
    status: str | None = None
    institution_code: str | None = None
    memo: str | None = None
    auto_sync: bool | None = None
    last_synced_at: str | None = None


class BankTransactionEntry(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = ""
    occurred_at: str
    posted_at: str = ""
    direction: str
    amount: float | int | str = 0
    balance: float | int | str | None = None
    counterparty: str = ""
    memo: str = ""
    raw_memo: str = ""
    category: str = ""
    platform_match: str = ""
    settlement_match: str = ""
    source: str = ""
    source_hash: str = ""


class BankTransactionImportPayload(BaseModel):
    model_config = {"extra": "forbid"}
    business_id: str = "biz-mia"
    branch_id: str = ""
    bank_account_id: str
    source: str = "manual"
    transactions: list[BankTransactionEntry] = Field(default_factory=list)


class BankTransactionCsvImportPayload(BaseModel):
    model_config = {"extra": "forbid"}
    business_id: str = "biz-mia"
    branch_id: str = ""
    bank_account_id: str
    source: str = "csv"
    filename: str = "bank-transactions.csv"
    csv_text: str = ""


class BankAccountCollectPayload(BaseModel):
    model_config = {"extra": "forbid"}
    business_id: str = "biz-mia"
    branch_id: str = ""
    date_from: str = ""
    date_to: str = ""
    source: str = "manual"
    transactions: list[BankTransactionEntry] = Field(default_factory=list)
    browser_session_id: str = Field(default="", repr=False)
    browser_work_key: str = ""
    auto_open_browser: bool = False
    browser_agent_id: str = ""
    browser_preferred_port: int | None = None
    force_recreate_browser: bool = False
    browser_timeout_seconds: float = Field(default=120, ge=5, le=300)


@router.get("/session")
async def get_session(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await run_in_threadpool(svc.session_for_user, current_user)


@router.get("/onboarding/document-types")
async def list_document_types(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"document_types": await run_in_threadpool(svc.list_document_types)}


@router.get("/employees/invites")
async def list_employee_invites(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"invites": await run_in_threadpool(svc.list_invites, current_user)}


@router.post("/employees/invites")
async def create_employee_invite(payload: InviteCreate, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"invite": await run_in_threadpool(svc.create_invite, payload.model_dump(), current_user)}


@router.get("/employees/invites/resolve")
async def resolve_employee_invite(token: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"invite": await run_in_threadpool(svc.resolve_invite, token)}


@router.post("/employees/invites/accept")
async def accept_employee_invite(payload: InviteAccept, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"request": await run_in_threadpool(svc.accept_invite, payload.model_dump(), current_user)}


@router.get("/employees/join-requests")
async def list_employee_join_requests(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"requests": await run_in_threadpool(svc.list_join_requests, current_user)}


@router.post("/employees/join-requests")
async def create_employee_join_request(payload: JoinRequestCreate, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"request": await run_in_threadpool(svc.upsert_join_request, payload.model_dump(), current_user)}


@router.patch("/employees/join-requests/{request_id}")
async def review_employee_join_request(request_id: str, payload: ReviewPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"request": await run_in_threadpool(svc.review_join_request, request_id, payload.action, payload.memo, current_user)}


@router.get("/employees/approved")
async def list_approved_employees(
    business_id: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"employees": await run_in_threadpool(svc.list_approved_employees, current_user, business_id)}


@router.patch("/employees/approved/{request_id}/role")
async def update_approved_employee_role(
    request_id: str,
    payload: EmployeeRoleUpdate,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {
        "employee": await run_in_threadpool(
            svc.update_approved_employee_role,
            request_id,
            payload.role,
            payload.memo,
            current_user,
        )
    }


@router.get("/onboarding/documents")
async def list_onboarding_documents(
    business_id: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"documents": await run_in_threadpool(svc.list_onboarding_documents, current_user, business_id)}


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
    document, path = await run_in_threadpool(svc.get_onboarding_document, document_id, current_user)
    return FileResponse(path, media_type=document.get("content_type") or "application/octet-stream", filename=document.get("original_filename") or path.name)


@router.patch("/onboarding/documents/{document_id}/review")
async def review_onboarding_document(document_id: str, payload: DocumentReviewPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"document": await run_in_threadpool(svc.review_onboarding_document, document_id, payload.status, payload.memo, current_user)}


@router.delete("/onboarding/documents/{document_id}")
async def delete_onboarding_document(document_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, bool]:
    await run_in_threadpool(svc.delete_onboarding_document, document_id, current_user)
    return {"ok": True}


@router.get("/contracts")
async def list_contracts(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contracts": await run_in_threadpool(svc.list_contracts, current_user)}


@router.post("/contracts")
async def save_contract(payload: GenericPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contract": await run_in_threadpool(svc.save_contract, payload.model_dump(), current_user)}


@router.post("/contracts/{contract_id}/request-signature")
async def request_contract_signature(contract_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contract": await run_in_threadpool(svc.request_contract_signature, contract_id, current_user)}


@router.get("/contracts/signing/{token}")
async def get_contract_signing(token: str, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"contract": await run_in_threadpool(svc.get_contract_by_token, token, current_user)}


@router.post("/contracts/signing")
async def sign_contract(
    payload: ContractSignPayload,
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    forwarded_for = str(request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    client_ip = forwarded_for or (request.client.host if request.client else "")
    sign_payload = {
        **payload.model_dump(),
        "audit_ip": client_ip[:64],
        "audit_user_agent": str(request.headers.get("user-agent") or "")[:512],
    }
    return {"contract": await run_in_threadpool(svc.sign_contract, sign_payload, current_user)}


@router.delete("/contracts/{contract_id}")
async def delete_contract(contract_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, bool]:
    await run_in_threadpool(svc.delete_contract, contract_id, current_user)
    return {"ok": True}


@router.get("/payroll")
async def list_payroll(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"statements": await run_in_threadpool(svc.list_payroll, current_user)}


@router.post("/payroll")
async def save_payroll(payload: GenericPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"statement": await run_in_threadpool(svc.save_payroll, payload.model_dump(), current_user)}


@router.delete("/payroll/{statement_id}")
async def delete_payroll(statement_id: str, current_user: dict = Depends(get_current_user)) -> dict[str, bool]:
    await run_in_threadpool(svc.delete_payroll, statement_id, current_user)
    return {"ok": True}


@router.get("/accounts")
async def list_accounts(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"accounts": await run_in_threadpool(svc.list_accounts, current_user, business_id)}


@router.get("/settings")
async def get_settings(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await svc.get_settings_persisted(current_user)


@router.put("/settings")
async def save_settings(payload: GenericPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await svc.save_settings_persisted(payload.model_dump(), current_user)


@router.get("/storage-status")
async def get_storage_status(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await svc.get_storage_status(current_user)


@router.post("/accounts")
async def upsert_account(
    payload: AccountUpsertPayload,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    data = payload.model_dump()
    account = await run_in_threadpool(svc.upsert_account, data, current_user)
    response: dict[str, Any] = {"account": account}
    service = str(data.get("service") or "")
    if data.get("auto_sync") and service in svc.PLATFORM_LABELS:
        sync_payload = {
            "services": [service],
            "account_id": account.get("id") or data.get("account_id") or data.get("server_account_id") or "",
            "business_id": account.get("business_id") or data.get("business_id") or "biz-mia",
            "branch": account.get("branch") or data.get("branch") or "열정국밥_미아점",
        }
        queued = await run_in_threadpool(svc.queue_delivery_sync, sync_payload, current_user)
        _start_delivery_sync_background(
            {
                **sync_payload,
                "sync_job_id": queued.get("job_id") or "",
                "queued_run_ids": queued.get("queued_run_ids") or {},
            },
            current_user,
        )
        response["sync"] = queued
    elif data.get("auto_sync") and service in svc.FINANCIAL_TRANSACTION_SERVICES:
        response["sync"] = await run_in_threadpool(
            svc.sync_financial_transactions,
            {
                "services": [service],
                "account_id": account.get("id") or data.get("account_id") or data.get("server_account_id") or "",
                "business_id": data.get("business_id") or account.get("business_id") or "biz-mia",
                "branch": data.get("branch") or account.get("branch") or "열정국밥_미아점",
            },
            current_user,
        )
    return response


@router.get("/settlements")
async def list_settlements(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"settlements": await run_in_threadpool(svc.list_settlements, current_user, business_id)}


@router.get("/integration-evidence")
async def list_integration_evidence(
    business_id: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"evidence": await run_in_threadpool(svc.list_integration_evidence, current_user, business_id)}


@router.post("/integration-evidence")
async def upload_integration_evidence(
    service: str = Form(...),
    business_id: str = Form("biz-mia"),
    branch: str = Form(""),
    document_kind: str = Form("other"),
    vendor: str = Form(""),
    amount: int = Form(0),
    memo: str = Form(""),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return await svc.save_integration_evidence(
        service=service,
        business_id=business_id,
        branch=branch,
        document_kind=document_kind,
        vendor=vendor,
        amount=amount,
        memo=memo,
        upload=file,
        user=current_user,
    )


@router.get("/integration-evidence/{evidence_id}/download")
async def download_integration_evidence(evidence_id: str, current_user: dict = Depends(get_current_user)) -> FileResponse:
    document, path = await run_in_threadpool(svc.get_integration_evidence, evidence_id, current_user)
    return FileResponse(path, media_type=document.get("content_type") or "application/octet-stream", filename=document.get("original_filename") or path.name)


@router.get("/sales")
async def list_sales(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"sales": await run_in_threadpool(svc.list_sales, current_user, business_id)}


@router.get("/reviews")
async def list_reviews(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"reviews": await run_in_threadpool(svc.list_reviews, current_user, business_id)}


@router.get("/ads")
async def list_ads(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"ads": await run_in_threadpool(svc.list_ads, current_user, business_id)}


@router.get("/collection-status")
async def list_collection_status(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"statuses": await run_in_threadpool(svc.list_collection_status, current_user, business_id)}


@router.get("/completion-matrix")
async def completion_matrix(business_id: str | None = None, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"matrix": await run_in_threadpool(svc.delivery_completion_matrix, current_user, business_id)}


@router.get("/automation")
async def get_automation_status(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await run_in_threadpool(svc.automation_status, current_user)


@router.post("/sync")
async def sync_delivery(
    payload: SyncPayload,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    data = payload.model_dump()
    if data.get("background"):
        queued = await run_in_threadpool(svc.queue_delivery_sync, data, current_user)
        _start_delivery_sync_background(
            {
                **data,
                "background": False,
                "sync_job_id": queued.get("job_id") or "",
                "queued_run_ids": queued.get("queued_run_ids") or {},
            },
            current_user,
        )
        return queued
    return await run_in_threadpool(svc.sync_delivery, data, current_user)


@router.get("/transactions")
async def list_transactions(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"transactions": await run_in_threadpool(svc.list_transactions_for_user, current_user)}


@router.post("/transactions/import")
async def import_transactions(payload: TransactionCsvImportPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await run_in_threadpool(svc.import_transaction_csv, payload.model_dump(), current_user)


@router.post("/transactions/sync")
async def sync_financial_transactions(payload: SyncPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await run_in_threadpool(svc.sync_financial_transactions, payload.model_dump(), current_user)


@router.post("/settlements/import")
async def import_settlements(payload: CsvImportPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await run_in_threadpool(
        partial(
            svc.import_settlement_csv,
            payload.csv_text,
            current_user,
            service=payload.service,
            business_id=payload.business_id,
            branch=payload.branch,
            filename=payload.filename,
        )
    )


@router.post("/delivery/import")
async def import_delivery_portal(payload: DeliveryPortalImportPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await run_in_threadpool(svc.import_delivery_portal_text, payload.model_dump(), current_user)


@router.get("/bank-accounts")
async def list_bank_accounts(
    business_id: str | None = None,
    branch_id: str | None = None,
    status: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    accounts = await run_in_threadpool(
        partial(svc.list_bank_accounts, current_user, business_id, branch_id=branch_id, status=status)
    )
    return {"bank_accounts": accounts}


@router.post("/bank-accounts")
async def create_bank_account(payload: BankAccountCreatePayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"bank_account": await run_in_threadpool(svc.create_bank_account, payload.model_dump(), current_user)}


@router.patch("/bank-accounts/{account_id}")
async def update_bank_account(
    account_id: str,
    payload: BankAccountUpdatePayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    account = await run_in_threadpool(
        svc.update_bank_account,
        account_id,
        payload.model_dump(exclude_unset=True),
        current_user,
    )
    return {"bank_account": account}


@router.post("/bank-accounts/{account_id}/collect")
async def collect_bank_account_transactions(
    account_id: str,
    payload: BankAccountCollectPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return await run_in_threadpool(svc.collect_bank_account_transactions, account_id, payload.model_dump(), current_user)


@router.get("/bank-transactions")
async def list_bank_transactions(
    business_id: str | None = None,
    branch_id: str | None = None,
    bank_account_id: str | None = None,
    direction: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    transactions = await run_in_threadpool(
        partial(
            svc.list_bank_transactions,
            current_user,
            business_id=business_id,
            branch_id=branch_id,
            bank_account_id=bank_account_id,
            direction=direction,
            date_from=date_from,
            date_to=date_to,
        )
    )
    return {"bank_transactions": transactions}


@router.post("/bank-transactions")
async def import_bank_transactions(payload: BankTransactionImportPayload, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await run_in_threadpool(svc.record_bank_transactions, payload.model_dump(), current_user)


@router.post("/bank-transactions/import")
async def import_bank_transaction_csv(
    payload: BankTransactionCsvImportPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return await run_in_threadpool(svc.import_bank_transaction_csv, payload.model_dump(), current_user)


@router.get("/bank-summary")
async def get_bank_summary(
    business_id: str | None = None,
    branch_id: str | None = None,
    bank_account_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return await run_in_threadpool(
        partial(
            svc.bank_summary,
            current_user,
            business_id=business_id,
            branch_id=branch_id,
            bank_account_id=bank_account_id,
            date_from=date_from,
            date_to=date_to,
        )
    )
