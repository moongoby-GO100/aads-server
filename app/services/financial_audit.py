"""금융 자동화 실행 감사 기록.

`authenticated_collector_audit_log` 에 설정 등록(site_profile.upsert) 5건만
있고 **수집 실행 기록이 하나도 없었다**(2026-09-13 확인). 무엇을, 언제, 어느
계정으로 조회했는지 남지 않으면 금융 자동화로 성립하지 않는다. 사고가 나도
무엇이 돌았는지 재구성할 수 없고, 오늘처럼 "러너가 왜 못 했나"를 물었을 때
로그를 뒤질 근거가 없다.

기록 원칙 두 가지.

**자격증명은 절대 남기지 않는다.** 계정 식별은 마스킹된 형태로만 남긴다
(`moo***@naver.com`). 감사 로그는 사고 조사 때 여러 사람이 보는 자료다.

**실패도 남긴다.** 성공만 기록하면 오늘 본 조용한 실패들이 감사에서도
사라진다. 차단·중단·재시도 금지 판정까지 같은 테이블에 남긴다.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from app.core.db_pool import get_pool

_TABLE = "authenticated_collector_audit_log"


def mask_account(value: Any) -> str:
    """계정 식별자를 마스킹한다. 감사에 필요한 만큼만 남긴다."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" in text:
        local, _, domain = text.partition("@")
        head = local[:3] if len(local) > 3 else local[:1]
        return f"{head}***@{domain}"
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


async def record_financial_audit(
    *,
    tenant_id: str,
    actor_user_id: Optional[str],
    action: str,
    site_key: str,
    resource_id: str = "",
    account: Any = None,
    details: Optional[dict] = None,
) -> bool:
    """감사 한 줄을 남긴다. 실패해도 호출부를 막지 않는다.

    감사 기록 실패가 수집 자체를 중단시키면 안 된다. 다만 조용히 넘기지 않고
    False 를 돌려 호출부가 알 수 있게 한다.

    action 예: collection.start / collection.complete / collection.blocked /
              collection.retry_denied / session.expired
    """
    payload = dict(details or {})
    payload["site_key"] = site_key
    masked = mask_account(account)
    if masked:
        payload["account"] = masked
    # 혹시라도 섞여 들어온 비밀 값을 막는다. 감사 로그는 여러 사람이 본다.
    for key in ("password", "secret", "token", "credential", "pw"):
        payload.pop(key, None)

    try:
        pool = get_pool()
        exists = await pool.fetchval(f"SELECT to_regclass('public.{_TABLE}')")
        if not exists:
            return False
        await pool.execute(
            f"""
            INSERT INTO {_TABLE}
                (tenant_id, actor_user_id, action, resource_type, resource_id, details)
            VALUES ($1, $2, $3, 'financial_collection', $4, $5::jsonb)
            """,
            uuid.UUID(str(tenant_id)),
            str(actor_user_id or "system"),
            str(action)[:120],
            str(resource_id or site_key)[:200],
            json.dumps(payload, ensure_ascii=False, default=str),
        )
        return True
    except Exception:
        return False


async def recent_financial_audit(*, tenant_id: str, limit: int = 20) -> list[dict]:
    """최근 실행 기록. 운영 점검과 사고 조사용."""
    try:
        pool = get_pool()
        rows = await pool.fetch(
            f"""
            SELECT action, resource_id, details, created_at
            FROM {_TABLE}
            WHERE tenant_id = $1 AND resource_type = 'financial_collection'
            ORDER BY created_at DESC
            LIMIT $2
            """,
            uuid.UUID(str(tenant_id)),
            int(limit),
        )
    except Exception:
        return []
    out = []
    for r in rows:
        item = dict(r)
        if isinstance(item.get("details"), str):
            try:
                item["details"] = json.loads(item["details"])
            except Exception:
                pass
        item["created_at"] = str(item.get("created_at"))
        out.append(item)
    return out
