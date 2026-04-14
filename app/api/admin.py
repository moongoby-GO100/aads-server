"""어드민 API — 시스템 프롬프트 관리 (Phase 2/3 기획안 구현)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class WorkspacePromptUpdate(BaseModel):
    system_prompt: str


class PromptPreviewRequest(BaseModel):
    workspace_key: str = "CEO"
    intent: str = ""
    base_system_prompt: str = ""


# ─── 프롬프트 섹션 조회 ──────────────────────────────────────────────────────

@router.get("/admin/prompts/sections")
async def list_prompt_sections():
    """코드 기반 시스템 프롬프트 섹션 목록 + 토큰 프로파일."""
    from app.core.prompts.token_profiler import profile_sections
    sections = profile_sections()
    return {"sections": sections, "count": len(sections)}


@router.get("/admin/prompts/intent-groups")
async def list_intent_groups():
    """인텐트 그룹별 skip 섹션 현황 (Phase 2 Adaptive Prompt)."""
    from app.core.prompts.system_prompt_v2 import (
        _INTENT_SECTIONS, _LITE_PROMPT_INTENTS, _NO_TOOLS_INTENTS,
    )
    groups = {}
    for name, cfg in _INTENT_SECTIONS.items():
        groups[name] = {
            "intents": sorted(cfg["intents"]),
            "skip": sorted(cfg["skip"]),
        }
    return {
        "groups": groups,
        "lite_intents": sorted(_LITE_PROMPT_INTENTS),
        "no_tools_intents": sorted(_NO_TOOLS_INTENTS),
    }


# ─── 프롬프트 미리보기 ──────────────────────────────────────────────────────

@router.post("/admin/prompts/preview")
async def preview_prompt(req: PromptPreviewRequest):
    """워크스페이스 x 인텐트 조합의 최종 시스템 프롬프트 미리보기."""
    from app.core.prompts.system_prompt_v2 import build_layer1, build_layer4, WS_ROLES
    from app.core.prompts.token_profiler import estimate_tokens

    if req.workspace_key not in WS_ROLES:
        raise HTTPException(400, f"Unknown workspace_key: {req.workspace_key}")

    layer1 = build_layer1(req.workspace_key, req.base_system_prompt, intent=req.intent)
    layer4 = build_layer4()
    full_prompt = layer1 + "\n\n" + layer4

    return {
        "prompt": full_prompt,
        "layer1_chars": len(layer1),
        "layer1_tokens": estimate_tokens(layer1),
        "layer4_chars": len(layer4),
        "layer4_tokens": estimate_tokens(layer4),
        "total_chars": len(full_prompt),
        "total_tokens": estimate_tokens(full_prompt),
    }


# ─── 워크스페이스 DB 프롬프트 관리 ───────────────────────────────────────────

@router.get("/admin/prompts/workspaces")
async def list_workspace_prompts():
    """DB chat_workspaces의 system_prompt 목록."""
    from app.core.db_pool import get_pool
    from app.core.prompts.token_profiler import estimate_tokens

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, system_prompt, color, icon, updated_at "
            "FROM chat_workspaces ORDER BY name"
        )
    items = []
    for r in rows:
        sp = r["system_prompt"] or ""
        items.append({
            "id": str(r["id"]),
            "name": r["name"],
            "system_prompt": sp,
            "chars": len(sp),
            "est_tokens": estimate_tokens(sp),
            "color": r["color"],
            "icon": r["icon"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return {"workspaces": items, "count": len(items)}


@router.put("/admin/prompts/workspace/{workspace_id}")
async def update_workspace_prompt(workspace_id: str, req: WorkspacePromptUpdate):
    """DB chat_workspaces.system_prompt 수정 + 버전 백업."""
    from app.core.db_pool import get_pool
    import uuid

    pool = get_pool()
    async with pool.acquire() as conn:
        # 기존 내용 백업
        old = await conn.fetchrow(
            "SELECT name, system_prompt FROM chat_workspaces WHERE id = $1",
            uuid.UUID(workspace_id),
        )
        if not old:
            raise HTTPException(404, "Workspace not found")

        # 버전 히스토리 저장
        await conn.execute(
            "INSERT INTO prompt_versions (section_name, content, changed_by) "
            "VALUES ($1, $2, $3)",
            f"ws_db_{old['name']}", old["system_prompt"] or "", "CEO",
        )

        # 업데이트
        await conn.execute(
            "UPDATE chat_workspaces SET system_prompt = $1, updated_at = $2 WHERE id = $3",
            req.system_prompt,
            datetime.now(ZoneInfo("Asia/Seoul")),
            uuid.UUID(workspace_id),
        )

    # Layer1 캐시 무효화
    from app.services.context_builder import _layer1_cache
    _layer1_cache.clear()

    logger.info("admin.prompt_updated workspace=%s", old["name"])
    return {"ok": True, "workspace": old["name"], "new_chars": len(req.system_prompt)}


# ─── 버전 히스토리 ────────────────────────────────────────────────────────────

@router.get("/admin/prompts/versions")
async def list_prompt_versions(
    section: Optional[str] = Query(None),
    limit: int = Query(20, le=100),
):
    """프롬프트 수정 이력 조회."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        if section:
            rows = await conn.fetch(
                "SELECT id, section_name, length(content) as content_len, changed_by, created_at "
                "FROM prompt_versions WHERE section_name = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                section, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, section_name, length(content) as content_len, changed_by, created_at "
                "FROM prompt_versions ORDER BY created_at DESC LIMIT $1",
                limit,
            )
    return {
        "versions": [
            {
                "id": r["id"],
                "section_name": r["section_name"],
                "content_len": r["content_len"],
                "changed_by": r["changed_by"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/admin/prompts/versions/{version_id}")
async def get_prompt_version(version_id: int):
    """특정 버전의 전체 내용 조회 (롤백용)."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM prompt_versions WHERE id = $1", version_id,
        )
    if not row:
        raise HTTPException(404, "Version not found")
    return {
        "id": row["id"],
        "section_name": row["section_name"],
        "content": row["content"],
        "changed_by": row["changed_by"],
        "created_at": row["created_at"].isoformat(),
    }


# ─── 토큰 프로파일 ────────────────────────────────────────────────────────────

@router.get("/admin/prompts/token-profile")
async def get_token_profile():
    """전체 워크스페이스 x 인텐트 토큰 프로파일 (히트맵용)."""
    from app.core.prompts.token_profiler import profile_all_workspaces, profile_sections
    return {
        "sections": profile_sections(),
        "workspaces": profile_all_workspaces(),
    }
