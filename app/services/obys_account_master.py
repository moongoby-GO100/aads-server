"""테넌트별 계정과목 조회 및 기존 전표 상수 대조. 전표 생성에는 관여하지 않는다."""
from __future__ import annotations

from typing import Any

LIST_SQL = """
SELECT account_code, account_name, source, source_company_label, is_active
FROM obys_account_master
WHERE tenant_id = $1
  AND ($2 = '' OR strpos(lower(account_code), lower($2)) > 0
       OR strpos(lower(account_name), lower($2)) > 0)
ORDER BY account_code
LIMIT $3 OFFSET $4
"""


async def list_accounts(user: dict[str, Any], query: str = '', limit: int = 500, offset: int = 0) -> dict:
    import asyncpg
    from fastapi import HTTPException
    from app.services import obys_upload_service as uploads

    # 활성 멤버십과 JWT 테넌트를 확인한 뒤에만 DB를 연다. tenant_id 입력 파라미터 없음.
    uploads.tenant_session_for_user(user)
    tenant_id = uploads._tenant(user)
    conn = await uploads._connect()
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(LIST_SQL, tenant_id, query.strip(), limit + 1, offset)
        return {'accounts': [dict(row) for row in rows[:limit]], 'has_more': len(rows) > limit}
    except asyncpg.UndefinedTableError:
        raise HTTPException(status_code=503, detail='계정과목 기준정보가 아직 준비되지 않았습니다') from None
    finally:
        await conn.close()
