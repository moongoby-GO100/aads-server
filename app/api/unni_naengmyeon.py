"""Public inquiry endpoint for the Unni Naengmyeon brand site."""

from __future__ import annotations

import re
import time
import uuid
from collections import defaultdict, deque

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.db_pool import get_pool


logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/unni-naengmyeon", tags=["unni-naengmyeon"])

_RATE_LIMIT = 5
_RATE_WINDOW_SECONDS = 600.0
_request_times: dict[str, deque[float]] = defaultdict(deque)
_contact_pattern = re.compile(r"^[0-9A-Za-z가-힣@._+()\-\s]{5,100}$")


class InquiryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    contact: str = Field(min_length=5, max_length=100)
    subject: str = Field(default="일반 문의", min_length=1, max_length=100)
    message: str = Field(min_length=10, max_length=2000)
    privacy_consent: bool
    website: str = Field(default="", max_length=200)


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request) -> None:
    key = _client_key(request)
    now = time.monotonic()
    bucket = _request_times[key]
    while bucket and bucket[0] <= now - _RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="문의가 연속으로 접수되었습니다. 잠시 후 다시 시도해 주세요.",
        )
    bucket.append(now)


def _clean(value: str) -> str:
    return " ".join(value.strip().split())


@router.post("/inquiries", status_code=status.HTTP_201_CREATED)
async def create_inquiry(body: InquiryCreate, request: Request):
    """Store a private customer inquiry; no public listing is exposed."""
    _check_rate_limit(request)

    # Hidden honeypot field: pretend success without persisting bot submissions.
    if body.website.strip():
        return {"status": "received", "reference": ""}

    name = _clean(body.name)
    contact = _clean(body.contact)
    subject = _clean(body.subject)
    message = body.message.strip()

    if not body.privacy_consent:
        raise HTTPException(status_code=422, detail="개인정보 수집 동의가 필요합니다.")
    if not _contact_pattern.fullmatch(contact):
        raise HTTPException(status_code=422, detail="연락처 형식을 확인해 주세요.")

    reference = uuid.uuid4()
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO unni_naengmyeon_inquiries
                    (reference, name, contact, subject, message, privacy_consent)
                VALUES ($1, $2, $3, $4, $5, TRUE)
                """,
                reference,
                name,
                contact,
                subject,
                message,
            )
    except Exception as exc:
        logger.error("unni_inquiry_store_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail="문의 접수가 지연되고 있습니다. 잠시 후 다시 시도해 주세요.") from exc

    logger.info("unni_inquiry_created", reference=str(reference))
    return {
        "status": "received",
        "reference": str(reference),
        "message": "문의가 접수되었습니다.",
    }
