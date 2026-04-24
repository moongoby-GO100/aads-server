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


# ─── 거버넌스 대시보드 ────────────────────────────────────────────────────────

def _governance_make_section(
    key: str,
    name: str,
    description: str,
    text: str,
    source: str,
) -> dict:
    from app.core.prompts.token_profiler import estimate_tokens

    body = text or ""
    return {
        "key": key,
        "name": name,
        "description": description,
        "chars": len(body),
        "est_tokens": estimate_tokens(body),
        "source": source,
    }


def _build_governance_layers() -> list[dict]:
    import json

    from app.core.prompts.system_prompt_v2 import (
        LAYER1_BEHAVIOR,
        LAYER1_CEO_GUIDE,
        LAYER1_RESPONSE_GUIDELINES,
        LAYER1_RULES,
        LAYER1_TOOLS,
        WS_CAPABILITIES,
        WS_ROLES,
        _INTENT_SECTIONS,
        _LITE_PROMPT_INTENTS,
        _NO_TOOLS_INTENTS,
        _CAPABILITIES_FULL,
    )

    tool_group_text = json.dumps(
        {
            name: {
                "intents": sorted(cfg["intents"]),
                "skip": sorted(cfg["skip"]),
            }
            for name, cfg in _INTENT_SECTIONS.items()
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )

    layers = [
        {
            "id": 0,
            "name": "Model Wrapper",
            "description": "모델별 instruction wrapper. 현재 system_prompt_v2.py에는 별도 텍스트 자산이 없습니다.",
            "source": "app/core/prompts/system_prompt_v2.py",
            "implemented": False,
            "sections": [],
        },
        {
            "id": 1,
            "name": "Common Policy",
            "description": "공통 행동 원칙, CEO 해석 가이드, 운영 규칙, 응답 가이드.",
            "source": "app/core/prompts/system_prompt_v2.py",
            "implemented": True,
            "sections": [
                _governance_make_section(
                    "behavior_principles",
                    "Behavior Principles",
                    "행동 원칙과 R-AUTH 등 핵심 규칙",
                    LAYER1_BEHAVIOR,
                    "LAYER1_BEHAVIOR",
                ),
                _governance_make_section(
                    "ceo_communication_guide",
                    "CEO Communication Guide",
                    "CEO 화법을 도구 호출 중심으로 해석하는 규칙",
                    LAYER1_CEO_GUIDE,
                    "LAYER1_CEO_GUIDE",
                ),
                _governance_make_section(
                    "rules",
                    "Operations Rules",
                    "보안 및 운영 규칙",
                    LAYER1_RULES,
                    "LAYER1_RULES",
                ),
                _governance_make_section(
                    "response_guidelines",
                    "Response Guidelines",
                    "응답 형식과 보고 기준",
                    LAYER1_RESPONSE_GUIDELINES,
                    "LAYER1_RESPONSE_GUIDELINES",
                ),
            ],
        },
        {
            "id": 2,
            "name": "Project Profile",
            "description": "프로젝트별 capabilities 자산. 현재는 코드 상수와 fallback full profile로 관리됩니다.",
            "source": "app/core/prompts/system_prompt_v2.py",
            "implemented": True,
            "sections": [
                _governance_make_section(
                    "capabilities_default",
                    "Default Capabilities",
                    "기본 프로젝트 capabilities fallback",
                    _CAPABILITIES_FULL,
                    "LAYER1_CAPABILITIES",
                ),
                *[
                    _governance_make_section(
                        f"capabilities_{workspace.lower()}",
                        f"{workspace} Capabilities",
                        f"{workspace} 전용 프로젝트 컨텍스트",
                        content,
                        f"WS_CAPABILITIES[{workspace!r}]",
                    )
                    for workspace, content in sorted(WS_CAPABILITIES.items())
                ],
            ],
        },
        {
            "id": 3,
            "name": "Role Profile",
            "description": "워크스페이스별 역할 프로필. 현재 WS_ROLES 상수로 유지됩니다.",
            "source": "app/core/prompts/system_prompt_v2.py",
            "implemented": True,
            "sections": [
                _governance_make_section(
                    f"role_{workspace.lower()}",
                    f"{workspace} Role",
                    f"{workspace} 워크스페이스 역할 정의",
                    content,
                    f"WS_ROLES[{workspace!r}]",
                )
                for workspace, content in sorted(WS_ROLES.items())
            ],
        },
        {
            "id": 4,
            "name": "Tool Policy",
            "description": "도구 카탈로그와 인텐트별 prompt 축소 정책.",
            "source": "app/core/prompts/system_prompt_v2.py",
            "implemented": True,
            "sections": [
                _governance_make_section(
                    "tools_available",
                    "Tools Available",
                    "도구 우선순위와 사용 전략",
                    LAYER1_TOOLS,
                    "LAYER1_TOOLS",
                ),
                _governance_make_section(
                    "intent_section_groups",
                    "Intent Section Groups",
                    "인텐트 그룹별 skip 정책",
                    tool_group_text,
                    "_INTENT_SECTIONS",
                ),
                _governance_make_section(
                    "lite_prompt_intents",
                    "Lite Prompt Intents",
                    "경량 프롬프트 사용 인텐트",
                    "\n".join(sorted(_LITE_PROMPT_INTENTS)),
                    "_LITE_PROMPT_INTENTS",
                ),
                _governance_make_section(
                    "no_tools_intents",
                    "No Tools Intents",
                    "도구 섹션을 주입하지 않는 인텐트",
                    "\n".join(sorted(_NO_TOOLS_INTENTS)),
                    "_NO_TOOLS_INTENTS",
                ),
            ],
        },
        {
            "id": 5,
            "name": "Memory Policy",
            "description": "목표 아키텍처상 메모리 정책 레이어. 현재 system_prompt_v2.py에는 아직 분리된 자산이 없습니다.",
            "source": "app/core/prompts/system_prompt_v2.py",
            "implemented": False,
            "sections": [],
        },
        {
            "id": 6,
            "name": "Workspace Override",
            "description": "chat_workspaces.system_prompt 기반 세션 오버라이드. 현재는 DB 런타임 데이터로만 주입됩니다.",
            "source": "chat_workspaces.system_prompt",
            "implemented": True,
            "sections": [],
        },
    ]

    for layer in layers:
        sections = layer["sections"]
        layer["section_count"] = len(sections)
        layer["est_tokens"] = sum(section["est_tokens"] for section in sections)

    return layers


def _build_governance_memory_sections() -> list[str]:
    return [
        "session_notes",
        "preferences",
        "tool_strategy",
        "directives",
        "discoveries",
        "learned_memory",
        "correction_directives",
        "experience_lessons",
        "visual_memories",
        "strategy_updates",
    ]


def _build_governance_intent_summary() -> dict:
    try:
        from app.services.model_selector import INTENT_MAP as intent_map
    except Exception:
        from app.services.intent_router import INTENT_MAP as intent_map

    model_to_intents: dict[str, list[str]] = {}
    for intent, cfg in intent_map.items():
        model = str(cfg.get("model") or "unknown")
        model_to_intents.setdefault(model, []).append(intent)

    by_model = []
    for model, intents in sorted(model_to_intents.items()):
        intents_sorted = sorted(intents)
        by_model.append({
            "model": model,
            "count": len(intents_sorted),
            "intents": intents_sorted,
        })

    return {
        "total_intents": len(intent_map),
        "model_distribution": {item["model"]: item["count"] for item in by_model},
        "by_model": by_model,
    }


def _build_governance_roadmap() -> list[dict]:
    return [
        {
            "phase": "W1",
            "title": "PromptCompiler + temperature 배선",
            "status": "in_progress",
            "items_done": 2,
            "items_total": 5,
            "items": [
                {"label": "build_layer1 기반 레이어 조립", "done": True},
                {"label": "model_selector prompt cache 블록 분리", "done": True},
                {"label": "PromptCompiler 모듈 도입", "done": False},
                {"label": "모델 프로필별 temperature 배선", "done": False},
                {"label": "compiled prompt provenance 기록", "done": False},
            ],
        },
        {
            "phase": "W2",
            "title": "ResponseCritic + 패리티 테스트",
            "status": "in_progress",
            "items_done": 1,
            "items_total": 4,
            "items": [
                {"label": "response_critic 서비스 추가", "done": True},
                {"label": "chat_service 사전 검증 연동", "done": False},
                {"label": "패리티/골든 테스트 세트", "done": False},
                {"label": "관리자 평가 결과 노출", "done": False},
            ],
        },
        {
            "phase": "W3",
            "title": "DB 이관 + CR 승인 흐름",
            "status": "planned",
            "items_done": 0,
            "items_total": 6,
            "items": [
                {"label": "prompt_assets 스키마", "done": False},
                {"label": "prompt_asset_versions 스키마", "done": False},
                {"label": "session_blueprints 스키마", "done": False},
                {"label": "prompt_change_requests 스키마", "done": False},
                {"label": "승인 큐/롤백 플로우", "done": False},
                {"label": "CR 기반 운영 반영 경로", "done": False},
            ],
        },
    ]


def _get_governance_pool():
    try:
        from app.core.database import get_pool
    except Exception:
        from app.core.db_pool import get_pool
    return get_pool()


async def _governance_table_exists(conn, table_name: str) -> bool:
    try:
        return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table_name}"))
    except Exception:
        return False


async def _governance_safe_count(conn, table_name: str, where_sql: str = "") -> int:
    try:
        if not await _governance_table_exists(conn, table_name):
            return 0
        row = await conn.fetchval(f"SELECT COUNT(*)::int FROM {table_name} {where_sql}")
        return int(row or 0)
    except Exception:
        return 0


async def _load_governance_roles() -> list[dict]:
    try:
        pool = _get_governance_pool()
        async with pool.acquire() as conn:
            if not await _governance_table_exists(conn, "chat_workspaces"):
                return []
            rows = await conn.fetch(
                "SELECT id::text AS workspace_id, name, icon, color "
                "FROM chat_workspaces ORDER BY created_at"
            )
    except Exception:
        return []

    return [
        {
            "workspace_id": row["workspace_id"],
            "name": row["name"],
            "icon": row["icon"] or "💬",
            "color": row["color"] or "#6366F1",
        }
        for row in rows
    ]


async def _load_governance_evolution_stats() -> dict:
    stats = {
        "observations": 0,
        "session_notes": 0,
        "error_patterns": 0,
        "memory_facts": 0,
        "response_critiques": 0,
    }

    try:
        pool = _get_governance_pool()
        async with pool.acquire() as conn:
            stats["observations"] = await _governance_safe_count(conn, "ai_observations")
            stats["session_notes"] = await _governance_safe_count(conn, "session_notes")
            stats["memory_facts"] = await _governance_safe_count(conn, "memory_facts")
            stats["response_critiques"] = await _governance_safe_count(conn, "response_critiques")
            if await _governance_table_exists(conn, "memory_facts"):
                stats["error_patterns"] = await _governance_safe_count(
                    conn,
                    "memory_facts",
                    "WHERE category = 'error_pattern'",
                )
    except Exception:
        return stats

    return stats


@router.get("/admin/governance")
async def get_governance_dashboard():
    """세션 거버넌스 현황 대시보드 요약."""
    layers = _build_governance_layers()

    roles = await _load_governance_roles()
    evolution_stats = await _load_governance_evolution_stats()

    return {
        "layers": [
            {
                "id": layer["id"],
                "name": layer["name"],
                "description": layer["description"],
                "section_count": layer["section_count"],
                "est_tokens": layer["est_tokens"],
                "implemented": layer["implemented"],
                "source": layer["source"],
            }
            for layer in layers
        ],
        "roles": roles,
        "intent_summary": _build_governance_intent_summary(),
        "memory_sections": _build_governance_memory_sections(),
        "evolution_stats": evolution_stats,
        "roadmap": _build_governance_roadmap(),
    }


@router.get("/admin/governance/layers")
async def get_governance_layers():
    """L0~L6 레이어별 상세 섹션 목록."""
    layers = _build_governance_layers()
    return {
        "layers": layers,
        "count": len(layers),
    }
