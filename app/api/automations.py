"""업무자동화 API — 홈 배지용 승인 대기 건수."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.auth import TenantRole, require_tenant_role
from app.services.work_recipe.approval_notifier import pending_count

router = APIRouter(prefix="/automations", tags=["automations"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)


@router.get("/pending-count")
async def get_pending_count(
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    """현재 테넌트의 승인 대기 건수와 가장 오래된 대기 시간(시간 단위)."""
    return await pending_count(tenant_id=str(context["tenant"]["id"]))
