"""
AADS 내부 서비스 토큰 엔드포인트 (AADS-NTV2)
NTV2 등 내부 서비스가 AADS DB의 LLM API 키를 빌려쓰기 위한 인터페이스.
X-Internal-Secret 헤더로 인증 후 llm_api_keys 에서 복호화된 토큰 반환.
"""
import os
import structlog
import asyncpg
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.core.credential_vault import decrypt_value

logger = structlog.get_logger()
router = APIRouter()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads_user:aads_pass@localhost:5432/aads",
)

# service 이름 → DB 조회 조건 매핑
_SERVICE_MAP = {
    "ntv2": {"provider": "anthropic", "label": "라일론"},
}


async def _get_conn():
    return await asyncpg.connect(DATABASE_URL, timeout=10)


class _TokenRequest(BaseModel):
    service: str


@router.post("/internal/service-token", tags=["internal"])
async def get_service_token(
    req: _TokenRequest,
    x_internal_secret: str = Header(..., alias="X-Internal-Secret"),
):
    expected = os.getenv("AADS_INTERNAL_SECRET", "")
    if not expected or x_internal_secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    cfg = _SERVICE_MAP.get(req.service)
    if not cfg:
        raise HTTPException(status_code=400, detail=f"Unknown service: {req.service}")

    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            SELECT encrypted_value FROM llm_api_keys
            WHERE provider=$1 AND label=$2 AND is_active=true
            ORDER BY priority ASC LIMIT 1
            """,
            cfg["provider"],
            cfg["label"],
        )
        if not row:
            row = await conn.fetchrow(
                """
                SELECT encrypted_value FROM llm_api_keys
                WHERE provider=$1 AND is_active=true
                ORDER BY priority ASC LIMIT 1
                """,
                cfg["provider"],
            )
    finally:
        await conn.close()

    if not row:
        raise HTTPException(status_code=503, detail="No active token available")

    token = decrypt_value(row["encrypted_value"])
    return {"token": token}
