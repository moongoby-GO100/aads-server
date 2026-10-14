"""레시피 등록 승인 카드 — 채팅 승인 카드 경로(agent_permission_requests)로 올린다.

browser_login_autosave.propose_save 와 같은 방식이다: 카드 한 장 = agent_permission_requests 한 행.
결정은 `/approvals/{id}/decide` 가 받고, 그 안에서 `decide_registration_as_human` 을 부른다.
에이전트 도구에는 이 카드를 결정하는 수단이 없다(결정 경로는 사람 JWT 만 통과한다).
카드 내용에는 비밀값·입력값 원문이 없다 — 도메인, 이름·버전, 단계 수, 위험 등급, 증거 URL 만.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import structlog

from app.services.work_recipe.registration import (
    registration_summary,
    requires_high_risk_confirmation,
)

logger = structlog.get_logger()

ACTION_TYPE = "approve_work_recipe_registration"
GATE_SOURCE = "work_recipe_registration"
CARD_TTL_DAYS = 14


def build_card(registration: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (action_summary, approval_scope) for one registration. Secret-free by construction."""
    from app.services.work_recipe.audit import mask_secrets

    info = registration_summary(registration)
    high = requires_high_risk_confirmation(info["max_risk"])
    name = mask_secrets(info["name"])[:120]
    domain = mask_secrets(info["domain"])[:200]
    version = info["version"]
    summary = (
        f"레시피 등록 승인 · {domain} · {name} v{version} · "
        f"단계 {info['step_count']}개(검증 {info['verify_count']}개) · 최대 위험 {info['max_risk']}"
    )
    if info["screen_evidence_url"]:
        summary += f" · 화면 증거 {info['screen_evidence_url']}"
    scope = {
        "scope": "single",
        "registration_id": str(registration["id"]),
        "domain": domain,
        "name": name,
        "version": version,
        "step_count": info["step_count"],
        "verify_count": info["verify_count"],
        "max_risk": info["max_risk"],
        "screen_evidence_url": info["screen_evidence_url"],
        "high_risk_confirm_required": high,
    }
    return summary, scope


async def propose_card(
    registration: Mapping[str, Any], *, chat_session_id: str
) -> dict[str, Any]:
    """pending 등록 한 건에 승인 카드를 올린다. 어떤 실패도 등록 자체를 막지 않는다."""
    try:
        if str(registration.get("status") or "") != "pending":
            return {"status": "not_pending"}
        from app.core.db_pool import get_pool

        summary, scope = build_card(registration)
        registration_id = str(registration["id"])
        work_key = f"recipe_registration:{registration_id}"
        risk_level = "high" if scope["high_risk_confirm_required"] else "medium"
        payload = json.dumps(scope, ensure_ascii=False)
        async with get_pool().acquire() as conn:
            request_id = await conn.fetchval(
                "SELECT id::text FROM agent_permission_requests "
                " WHERE tenant_id = $1::uuid AND work_key = $2 AND decision = 'pending' "
                "   AND expires_at > now() ORDER BY created_at DESC LIMIT 1",
                str(registration["tenant_id"]), work_key,
            )
            if request_id:
                await conn.execute(
                    "UPDATE agent_permission_requests "
                    "   SET approval_scope = $2::jsonb, action_summary = $3, risk_level = $4, "
                    "       updated_at = now() WHERE id = $1::uuid",
                    request_id, payload, summary, risk_level,
                )
                return {"status": "refreshed", "request_id": request_id}
            request_id = await conn.fetchval(
                """
                INSERT INTO agent_permission_requests
                    (tenant_id, work_key, origin, action_type, action_summary,
                     risk_level, decision, requested_by, approval_scope,
                     max_executions, expires_at, created_at, gate_source, tier)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, 'pending', $7, $8::jsonb,
                        1, now() + make_interval(days => $9), now(), $10, 'approve')
                RETURNING id::text
                """,
                str(registration["tenant_id"]), work_key, scope["domain"], ACTION_TYPE,
                summary, risk_level, chat_session_id, payload, CARD_TTL_DAYS, GATE_SOURCE,
            )
        logger.info("recipe_registration_card_proposed", request=str(request_id)[:8])
        return {"status": "proposed", "request_id": str(request_id)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("recipe_registration_card_failed", error=type(exc).__name__)
        return {"status": "error", "error": type(exc).__name__}
