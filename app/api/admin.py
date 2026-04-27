"""어드민 API — 시스템 프롬프트 관리 (Phase 2/3 기획안 구현)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

_TASK_BOARD_STATUSES = {"queued", "running", "awaiting_approval", "done", "error"}
_TASK_BOARD_STATUS_SQL = (
    "CASE "
    "WHEN status = 'queued' THEN 'queued' "
    "WHEN status IN ('running', 'claimed') THEN 'running' "
    "WHEN status = 'awaiting_approval' THEN 'awaiting_approval' "
    "WHEN status IN ('done', 'approved') THEN 'done' "
    "ELSE 'error' "
    "END"
)
_ADMIN_AGENT_ROLE_COLUMN_CANDIDATES = (
    "agent_role",
    "role",
    "owner_role",
    "worker_role",
    "assignee_role",
    "profile_role",
    "agent",
)
_DEPLOY_SERVER_GROUPS = (
    {"id": "68", "name": "서버68", "ip": "68.183.183.11", "projects": ("AADS",)},
    {"id": "211", "name": "서버211", "ip": "211.188.51.113", "projects": ("KIS", "GO100")},
    {"id": "114", "name": "서버114", "ip": "116.120.58.155", "projects": ("SF", "NTV2")},
)
_DEPLOY_STATUS_OK = {"done"}
_DEPLOY_STATUS_ERROR = {"error", "failed", "rejected", "cancelled", "canceled"}


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


def _load_model_parity_intent_map() -> tuple[str, dict[str, dict]]:
    source = "app.services.model_selector"
    try:
        from app.services.model_selector import INTENT_MAP as raw_intent_map
    except Exception:
        from app.services.intent_router import INTENT_MAP as raw_intent_map
        source = "app.services.intent_router"

    intent_map: dict[str, dict] = {}
    for intent, cfg in (raw_intent_map or {}).items():
        normalized = dict(cfg or {})
        normalized["model"] = str((cfg or {}).get("model") or "unknown")
        normalized["tools"] = bool((cfg or {}).get("tools"))
        normalized["group"] = str((cfg or {}).get("group") or "")
        normalized["thinking"] = bool((cfg or {}).get("thinking"))
        normalized["gemini_direct"] = str((cfg or {}).get("gemini_direct") or "")
        intent_map[intent] = normalized

    return source, intent_map


def _build_model_parity_routing() -> dict:
    source, intent_map = _load_model_parity_intent_map()

    route_buckets: dict[tuple[str, bool, str, bool, str], dict] = {}
    model_buckets: dict[str, dict] = {}

    tool_enabled = 0
    thinking_enabled = 0
    gemini_direct_enabled = 0

    for intent, cfg in sorted(intent_map.items()):
        model = str(cfg.get("model") or "unknown")
        tools = bool(cfg.get("tools"))
        group = str(cfg.get("group") or "")
        thinking = bool(cfg.get("thinking"))
        gemini_direct = str(cfg.get("gemini_direct") or "")

        if tools:
            tool_enabled += 1
        if thinking:
            thinking_enabled += 1
        if gemini_direct:
            gemini_direct_enabled += 1

        route_key = (model, tools, group, thinking, gemini_direct)
        route_bucket = route_buckets.setdefault(
            route_key,
            {
                "model": model,
                "tools": tools,
                "group": group,
                "thinking": thinking,
                "gemini_direct": gemini_direct,
                "count": 0,
                "intents": [],
            },
        )
        route_bucket["count"] += 1
        route_bucket["intents"].append(intent)

        model_bucket = model_buckets.setdefault(
            model,
            {
                "model": model,
                "count": 0,
                "tool_enabled": 0,
                "thinking_enabled": 0,
                "gemini_direct_enabled": 0,
                "intents": [],
            },
        )
        model_bucket["count"] += 1
        model_bucket["tool_enabled"] += 1 if tools else 0
        model_bucket["thinking_enabled"] += 1 if thinking else 0
        model_bucket["gemini_direct_enabled"] += 1 if gemini_direct else 0
        model_bucket["intents"].append(intent)

    by_route = sorted(
        route_buckets.values(),
        key=lambda item: (-item["count"], item["model"], item["group"], item["gemini_direct"]),
    )
    by_model = sorted(
        model_buckets.values(),
        key=lambda item: (-item["count"], item["model"]),
    )

    return {
        "source": source,
        "total_intents": len(intent_map),
        "tool_enabled_intents": tool_enabled,
        "thinking_enabled_intents": thinking_enabled,
        "gemini_direct_intents": gemini_direct_enabled,
        "by_model": by_model,
        "by_route": by_route,
    }


async def _admin_column_exists(conn, table_name: str, column_name: str) -> bool:
    try:
        exists = await conn.fetchval(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = $1
              AND column_name = $2
            LIMIT 1
            """,
            table_name,
            column_name,
        )
        return bool(exists)
    except Exception:
        return False


async def _admin_table_exists(conn, table_name: str) -> bool:
    try:
        exists = await conn.fetchval(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = $1
            LIMIT 1
            """,
            table_name,
        )
        return bool(exists)
    except Exception:
        return False


async def _admin_table_columns(conn, table_name: str) -> set[str]:
    try:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = $1
            """,
            table_name,
        )
        return {str(row["column_name"]) for row in rows if row and row["column_name"]}
    except Exception:
        return set()


def _admin_pick_column(columns: set[str], candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _admin_colref(alias: str, column_name: str) -> str:
    escaped = column_name.replace('"', '""')
    return f'{alias}."{escaped}"'


def _admin_column_expr(
    alias: str,
    columns: set[str],
    candidates: Sequence[str],
    fallback_sql: str,
) -> str:
    picked = _admin_pick_column(columns, candidates)
    if not picked:
        return fallback_sql
    return _admin_colref(alias, picked)


def _admin_to_string_list(value: Any) -> list[str]:
    parsed = _admin_parse_json(value)
    if parsed in (None, ""):
        return []
    if isinstance(parsed, (list, tuple, set)):
        items = []
        for item in parsed:
            text = str(item or "").strip()
            if text:
                items.append(text)
        return items
    if isinstance(parsed, str):
        text = parsed.strip()
        if not text:
            return []
        if text.startswith("{") and text.endswith("}") and "," in text:
            # PostgreSQL text[]가 문자열로 내려온 경우 대비
            raw_items = [chunk.strip().strip('"') for chunk in text[1:-1].split(",")]
            return [chunk for chunk in raw_items if chunk]
        return [text]
    return [str(parsed)]


@router.get("/admin/model-parity")
async def get_model_parity_dashboard():
    """최근 7일 모델 라우팅/사용량 패리티 대시보드."""
    routing = _build_model_parity_routing()
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    day_labels = [
        (window_start + timedelta(days=offset)).date().isoformat()
        for offset in range(7)
    ]

    response = {
        "summary": {
            "window_days": 7,
            "window_start": window_start.isoformat(),
            "window_end": now.isoformat(),
            "tracked_models": 0,
            "tracked_intents": routing["total_intents"],
            "tracked_messages": 0,
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
        },
        "routing": routing,
        "models": [],
        "daily": [],
    }

    try:
        pool = _get_governance_pool()
        async with pool.acquire() as conn:
            if not await _governance_table_exists(conn, "chat_messages"):
                return response

            required_columns = ("created_at", "model_used", "tokens_in", "tokens_out")
            for column_name in required_columns:
                if not await _admin_column_exists(conn, "chat_messages", column_name):
                    return response

            has_role = await _admin_column_exists(conn, "chat_messages", "role")
            has_intent = await _admin_column_exists(conn, "chat_messages", "intent")
            role_filter = " AND role = 'assistant'" if has_role else ""
            intent_expr = "COUNT(DISTINCT NULLIF(intent, ''))::int" if has_intent else "0::int"

            tracked_messages_where = (
                f"WHERE created_at >= TIMESTAMPTZ '{window_start.isoformat()}'{role_filter}"
            )
            tracked_messages = await _governance_safe_count(conn, "chat_messages", tracked_messages_where)

            model_rows = await conn.fetch(
                f"""
                SELECT
                    COALESCE(NULLIF(model_used, ''), 'unknown') AS model,
                    COUNT(*)::int AS calls,
                    COALESCE(SUM(tokens_in), 0)::bigint AS input_tokens,
                    COALESCE(SUM(tokens_out), 0)::bigint AS output_tokens,
                    COALESCE(SUM(tokens_in + tokens_out), 0)::bigint AS total_tokens,
                    {intent_expr} AS distinct_intents
                FROM chat_messages
                WHERE created_at >= $1
                  AND COALESCE(NULLIF(model_used, ''), '') <> ''
                  {role_filter}
                GROUP BY 1
                ORDER BY calls DESC, total_tokens DESC, model ASC
                """,
                window_start,
            )

            daily_rows = await conn.fetch(
                f"""
                SELECT
                    TO_CHAR(DATE(created_at AT TIME ZONE 'Asia/Seoul'), 'YYYY-MM-DD') AS day,
                    COALESCE(NULLIF(model_used, ''), 'unknown') AS model,
                    COUNT(*)::int AS calls,
                    COALESCE(SUM(tokens_in), 0)::bigint AS input_tokens,
                    COALESCE(SUM(tokens_out), 0)::bigint AS output_tokens,
                    COALESCE(SUM(tokens_in + tokens_out), 0)::bigint AS total_tokens
                FROM chat_messages
                WHERE created_at >= $1
                  AND COALESCE(NULLIF(model_used, ''), '') <> ''
                  {role_filter}
                GROUP BY 1, 2
                ORDER BY 1 ASC, 2 ASC
                """,
                window_start,
            )
    except Exception:
        return response

    routing_model_counts = {
        item["model"]: int(item["count"] or 0)
        for item in routing["by_model"]
    }

    models = []
    total_calls = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_tokens = 0

    for row in model_rows:
        calls = int(row["calls"] or 0)
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        total_row_tokens = int(row["total_tokens"] or 0)

        total_calls += calls
        total_input_tokens += input_tokens
        total_output_tokens += output_tokens
        total_tokens += total_row_tokens

        models.append(
            {
                "model": row["model"],
                "calls": calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_row_tokens,
                "avg_tokens_per_call": round(total_row_tokens / calls, 2) if calls else 0,
                "distinct_intents": int(row["distinct_intents"] or 0),
                "configured_intents": routing_model_counts.get(row["model"], 0),
            }
        )

    daily_lookup = {
        day: {
            "date": day,
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "models": [],
        }
        for day in day_labels
    }

    for row in daily_rows:
        day = row["day"]
        bucket = daily_lookup.get(day)
        if not bucket:
            continue
        calls = int(row["calls"] or 0)
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        total_row_tokens = int(row["total_tokens"] or 0)

        bucket["calls"] += calls
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["total_tokens"] += total_row_tokens
        bucket["models"].append(
            {
                "model": row["model"],
                "calls": calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_row_tokens,
            }
        )

    response["summary"] = {
        "window_days": 7,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "tracked_models": len(models),
        "tracked_intents": routing["total_intents"],
        "tracked_messages": tracked_messages,
        "total_calls": total_calls,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_tokens,
    }
    response["models"] = models
    response["daily"] = list(daily_lookup.values())

    return response


@router.get("/admin/model-parity/intent-map")
async def get_model_parity_intent_map():
    """모델 라우팅용 INTENT_MAP 전체 덤프."""
    source, intent_map = _load_model_parity_intent_map()
    return {
        "source": source,
        "count": len(intent_map),
        "intent_map": dict(sorted(intent_map.items())),
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


def _admin_task_status(status: Optional[str]) -> Optional[str]:
    normalized = (status or "").strip()
    if not normalized:
        return None
    if normalized not in _TASK_BOARD_STATUSES:
        raise HTTPException(400, f"Unknown status: {normalized}")
    return normalized


def _admin_iso(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def _admin_parse_json(value: Any) -> Any:
    if value in (None, ""):
        return []
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _admin_format_task_log(row: Any) -> dict[str, Any]:
    metadata = row["metadata"]
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {"raw": metadata}

    return {
        "id": row["id"],
        "log_type": row["log_type"] or "",
        "content": row["content"] or "",
        "phase": row["phase"] or "",
        "metadata": metadata or {},
        "created_at": _admin_iso(row["created_at"]),
    }


def _admin_short_commit(value: Any, fallback_job_id: Any = None) -> Optional[str]:
    for candidate in (value, fallback_job_id):
        if candidate in (None, ""):
            continue
        text = str(candidate).strip()
        if text:
            return text[:7]
    return None


def _admin_deploy_status(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in _DEPLOY_STATUS_OK:
        return "ok"
    if normalized in _DEPLOY_STATUS_ERROR:
        return "error"
    return "unknown"


@router.get("/admin/deploy/status")
async def get_admin_deploy_status():
    """서버별 마지막 배포 상태 조회."""
    from app.core.db_pool import get_pool

    projects = [project for server in _DEPLOY_SERVER_GROUPS for project in server["projects"]]
    latest_done_rows: dict[str, Any] = {}
    latest_rows: dict[str, Any] = {}

    pool = get_pool()
    async with pool.acquire() as conn:
        has_pipeline_jobs = await _admin_table_exists(conn, "pipeline_jobs")
        if has_pipeline_jobs:
            has_commit_hash_column = await _admin_column_exists(conn, "pipeline_jobs", "commit_hash")
            commit_expr = "NULLIF(TRIM(commit_hash::text), '') AS commit_hash" if has_commit_hash_column else "NULL::text AS commit_hash"

            done_rows = await conn.fetch(
                f"""
                WITH latest_done AS (
                    SELECT DISTINCT ON (upper(project))
                        upper(project) AS project,
                        job_id,
                        status,
                        COALESCE(updated_at, created_at) AS last_deploy_at,
                        {commit_expr}
                    FROM pipeline_jobs
                    WHERE upper(project) = ANY($1::text[])
                      AND status = 'done'
                    ORDER BY upper(project), COALESCE(updated_at, created_at) DESC NULLS LAST, created_at DESC NULLS LAST
                )
                SELECT project, job_id, status, last_deploy_at, commit_hash
                FROM latest_done
                """,
                projects,
            )
            status_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (upper(project))
                    upper(project) AS project,
                    job_id,
                    status
                FROM pipeline_jobs
                WHERE upper(project) = ANY($1::text[])
                ORDER BY upper(project), COALESCE(updated_at, created_at) DESC NULLS LAST, created_at DESC NULLS LAST
                """,
                projects,
            )
            latest_done_rows = {str(row["project"]).upper(): row for row in done_rows}
            latest_rows = {str(row["project"]).upper(): row for row in status_rows}

    servers = []
    for server in _DEPLOY_SERVER_GROUPS:
        server_projects = []
        for project in server["projects"]:
            done_row = latest_done_rows.get(project)
            latest_row = latest_rows.get(project)
            server_projects.append({
                "name": project,
                "status": _admin_deploy_status(latest_row["status"] if latest_row else None),
                "last_commit": _admin_short_commit(done_row["commit_hash"] if done_row else None, done_row["job_id"] if done_row else None),
                "last_deploy_at": _admin_iso(done_row["last_deploy_at"]) if done_row else None,
            })

        servers.append({
            "id": server["id"],
            "name": server["name"],
            "ip": server["ip"],
            "projects": server_projects,
        })

    return {"servers": servers}


@router.get("/admin/agents")
async def list_admin_agents():
    """Agent Registry 목록 + 최근 작업 통계."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        if not await _admin_table_exists(conn, "role_profiles"):
            return {"agents": [], "total": 0}

        role_columns = await _admin_table_columns(conn, "role_profiles")
        role_column = _admin_pick_column(role_columns, ("role",))
        if not role_column:
            return {"agents": [], "total": 0}

        has_pipeline_jobs = await _admin_table_exists(conn, "pipeline_jobs")
        pipeline_columns = await _admin_table_columns(conn, "pipeline_jobs") if has_pipeline_jobs else set()
        pipeline_role_column = _admin_pick_column(pipeline_columns, _ADMIN_AGENT_ROLE_COLUMN_CANDIDATES)

        role_expr = _admin_colref("rp", role_column)
        display_name_expr = _admin_column_expr("rp", role_columns, ("display_name", "name"), role_expr)
        base_model_expr = _admin_column_expr(
            "rp",
            role_columns,
            ("base_model", "default_model", "model"),
            "NULL::text",
        )
        allowed_intents_expr = _admin_column_expr(
            "rp",
            role_columns,
            ("allowed_intents", "intent_allowlist"),
            "NULL",
        )
        max_tokens_expr = _admin_column_expr(
            "rp",
            role_columns,
            ("max_tokens", "token_limit", "max_output_tokens"),
            "NULL::bigint",
        )
        created_at_expr = _admin_column_expr(
            "rp",
            role_columns,
            ("created_at", "updated_at"),
            "NULL::timestamptz",
        )

        if has_pipeline_jobs and pipeline_role_column:
            job_role_expr = f"NULLIF(TRIM({_admin_colref('pj', pipeline_role_column)}::text), '')"
            rows = await conn.fetch(
                f"""
                WITH role_jobs AS (
                    SELECT
                        {job_role_expr} AS job_role,
                        COUNT(*) FILTER (WHERE COALESCE(pj.updated_at, pj.created_at, pj.started_at) >= NOW() - INTERVAL '30 days') AS recent_tasks_count,
                        MAX(COALESCE(pj.updated_at, pj.created_at, pj.started_at)) AS last_active_at
                    FROM pipeline_jobs pj
                    WHERE {job_role_expr} IS NOT NULL
                    GROUP BY {job_role_expr}
                )
                SELECT
                    {role_expr} AS role,
                    {display_name_expr} AS display_name,
                    {base_model_expr} AS base_model,
                    {allowed_intents_expr} AS allowed_intents,
                    {max_tokens_expr} AS max_tokens,
                    {created_at_expr} AS created_at,
                    COALESCE(rj.recent_tasks_count, 0) AS recent_tasks_count,
                    rj.last_active_at AS last_active_at
                FROM role_profiles rp
                LEFT JOIN role_jobs rj
                  ON lower(rj.job_role) = lower({role_expr}::text)
                ORDER BY lower(COALESCE({display_name_expr}::text, {role_expr}::text))
                """
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT
                    {role_expr} AS role,
                    {display_name_expr} AS display_name,
                    {base_model_expr} AS base_model,
                    {allowed_intents_expr} AS allowed_intents,
                    {max_tokens_expr} AS max_tokens,
                    {created_at_expr} AS created_at,
                    0::bigint AS recent_tasks_count,
                    NULL::timestamptz AS last_active_at
                FROM role_profiles rp
                ORDER BY lower(COALESCE({display_name_expr}::text, {role_expr}::text))
                """
            )

    agents = [
        {
            "role": row["role"] or "",
            "display_name": row["display_name"] or row["role"] or "",
            "base_model": row["base_model"] or "",
            "allowed_intents": _admin_to_string_list(row["allowed_intents"]),
            "max_tokens": int(row["max_tokens"]) if row["max_tokens"] is not None else None,
            "created_at": _admin_iso(row["created_at"]),
            "recent_tasks_count": int(row["recent_tasks_count"] or 0),
            "last_active_at": _admin_iso(row["last_active_at"]),
        }
        for row in rows
    ]

    return {
        "agents": agents,
        "total": len(agents),
    }


@router.get("/admin/agents/stats")
async def get_admin_agent_stats():
    """에이전트별 작업 완료/에러 비율."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        if not await _admin_table_exists(conn, "role_profiles"):
            return {"agents": [], "total": 0}

        role_columns = await _admin_table_columns(conn, "role_profiles")
        role_column = _admin_pick_column(role_columns, ("role",))
        if not role_column:
            return {"agents": [], "total": 0}

        role_expr = _admin_colref("rp", role_column)
        display_name_expr = _admin_column_expr("rp", role_columns, ("display_name", "name"), role_expr)

        has_pipeline_jobs = await _admin_table_exists(conn, "pipeline_jobs")
        pipeline_columns = await _admin_table_columns(conn, "pipeline_jobs") if has_pipeline_jobs else set()
        pipeline_role_column = _admin_pick_column(pipeline_columns, _ADMIN_AGENT_ROLE_COLUMN_CANDIDATES)

        if not (has_pipeline_jobs and pipeline_role_column):
            rows = await conn.fetch(
                f"""
                SELECT
                    {role_expr} AS role,
                    {display_name_expr} AS display_name,
                    0::bigint AS total_tasks,
                    0::bigint AS completed_tasks,
                    0::bigint AS error_tasks,
                    NULL::timestamptz AS last_active_at
                FROM role_profiles rp
                ORDER BY lower(COALESCE({display_name_expr}::text, {role_expr}::text))
                """
            )
        else:
            job_role_expr = f"NULLIF(TRIM({_admin_colref('pj', pipeline_role_column)}::text), '')"
            job_status_expr = (
                "CASE "
                "WHEN pj.status = 'queued' THEN 'queued' "
                "WHEN pj.status IN ('running', 'claimed') THEN 'running' "
                "WHEN pj.status = 'awaiting_approval' THEN 'awaiting_approval' "
                "WHEN pj.status IN ('done', 'approved') THEN 'done' "
                "ELSE 'error' "
                "END"
            )
            rows = await conn.fetch(
                f"""
                WITH role_jobs AS (
                    SELECT
                        {job_role_expr} AS job_role,
                        COUNT(*) AS total_tasks,
                        COUNT(*) FILTER (WHERE {job_status_expr} = 'done') AS completed_tasks,
                        COUNT(*) FILTER (WHERE {job_status_expr} = 'error') AS error_tasks,
                        MAX(COALESCE(pj.updated_at, pj.created_at, pj.started_at)) AS last_active_at
                    FROM pipeline_jobs pj
                    WHERE {job_role_expr} IS NOT NULL
                    GROUP BY {job_role_expr}
                )
                SELECT
                    {role_expr} AS role,
                    {display_name_expr} AS display_name,
                    COALESCE(rj.total_tasks, 0) AS total_tasks,
                    COALESCE(rj.completed_tasks, 0) AS completed_tasks,
                    COALESCE(rj.error_tasks, 0) AS error_tasks,
                    rj.last_active_at AS last_active_at
                FROM role_profiles rp
                LEFT JOIN role_jobs rj
                  ON lower(rj.job_role) = lower({role_expr}::text)
                ORDER BY lower(COALESCE({display_name_expr}::text, {role_expr}::text))
                """
            )

    agents = []
    for row in rows:
        total_tasks = int(row["total_tasks"] or 0)
        completed_tasks = int(row["completed_tasks"] or 0)
        error_tasks = int(row["error_tasks"] or 0)
        completed_ratio = (completed_tasks / total_tasks) if total_tasks else 0.0
        error_ratio = (error_tasks / total_tasks) if total_tasks else 0.0
        agents.append(
            {
                "role": row["role"] or "",
                "display_name": row["display_name"] or row["role"] or "",
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "error_tasks": error_tasks,
                "completed_ratio": round(completed_ratio, 4),
                "error_ratio": round(error_ratio, 4),
                "last_active_at": _admin_iso(row["last_active_at"]),
            }
        )

    return {
        "agents": agents,
        "total": len(agents),
    }


@router.get("/admin/agents/{role}")
async def get_admin_agent(role: str):
    """Agent Registry 상세 + 최근 작업 10건."""
    from app.core.db_pool import get_pool

    role_key = (role or "").strip()
    if not role_key:
        raise HTTPException(400, "Role is required")

    pool = get_pool()
    async with pool.acquire() as conn:
        if not await _admin_table_exists(conn, "role_profiles"):
            raise HTTPException(404, "Agent registry not found")

        role_columns = await _admin_table_columns(conn, "role_profiles")
        role_column = _admin_pick_column(role_columns, ("role",))
        if not role_column:
            raise HTTPException(404, "Agent registry not found")

        role_expr = _admin_colref("rp", role_column)
        display_name_expr = _admin_column_expr("rp", role_columns, ("display_name", "name"), role_expr)
        base_model_expr = _admin_column_expr(
            "rp",
            role_columns,
            ("base_model", "default_model", "model"),
            "NULL::text",
        )
        allowed_intents_expr = _admin_column_expr(
            "rp",
            role_columns,
            ("allowed_intents", "intent_allowlist"),
            "NULL",
        )
        max_tokens_expr = _admin_column_expr(
            "rp",
            role_columns,
            ("max_tokens", "token_limit", "max_output_tokens"),
            "NULL::bigint",
        )
        created_at_expr = _admin_column_expr(
            "rp",
            role_columns,
            ("created_at", "updated_at"),
            "NULL::timestamptz",
        )

        profile_row = await conn.fetchrow(
            f"""
            SELECT
                {role_expr} AS role,
                {display_name_expr} AS display_name,
                {base_model_expr} AS base_model,
                {allowed_intents_expr} AS allowed_intents,
                {max_tokens_expr} AS max_tokens,
                {created_at_expr} AS created_at
            FROM role_profiles rp
            WHERE lower({role_expr}::text) = lower($1)
            LIMIT 1
            """,
            role_key,
        )
        if not profile_row:
            raise HTTPException(404, "Agent not found")

        has_pipeline_jobs = await _admin_table_exists(conn, "pipeline_jobs")
        pipeline_columns = await _admin_table_columns(conn, "pipeline_jobs") if has_pipeline_jobs else set()
        pipeline_role_column = _admin_pick_column(pipeline_columns, _ADMIN_AGENT_ROLE_COLUMN_CANDIDATES)

        recent_tasks: list[dict[str, Any]] = []
        total_tasks = 0
        completed_tasks = 0
        error_tasks = 0
        last_active_at = None

        if has_pipeline_jobs and pipeline_role_column:
            job_role_expr = f"NULLIF(TRIM({_admin_colref('pj', pipeline_role_column)}::text), '')"
            job_status_expr = (
                "CASE "
                "WHEN pj.status = 'queued' THEN 'queued' "
                "WHEN pj.status IN ('running', 'claimed') THEN 'running' "
                "WHEN pj.status = 'awaiting_approval' THEN 'awaiting_approval' "
                "WHEN pj.status IN ('done', 'approved') THEN 'done' "
                "ELSE 'error' "
                "END"
            )

            task_rows = await conn.fetch(
                f"""
                SELECT
                    pj.job_id,
                    pj.project,
                    {job_status_expr} AS status,
                    pj.phase,
                    substring(COALESCE(pj.instruction, '') from 1 for 160) AS instruction,
                    pj.model,
                    pj.worker_model,
                    pj.actual_model,
                    pj.error_detail,
                    pj.started_at,
                    pj.created_at,
                    pj.updated_at
                FROM pipeline_jobs pj
                WHERE lower({job_role_expr}) = lower($1)
                ORDER BY COALESCE(pj.updated_at, pj.created_at, pj.started_at) DESC NULLS LAST
                LIMIT 10
                """,
                profile_row["role"] or role_key,
            )

            recent_tasks = [
                {
                    "job_id": task_row["job_id"],
                    "project": task_row["project"] or "",
                    "status": task_row["status"] or "",
                    "phase": task_row["phase"] or "",
                    "instruction": task_row["instruction"] or "",
                    "model": task_row["model"] or "",
                    "worker_model": task_row["worker_model"] or "",
                    "actual_model": task_row["actual_model"] or "",
                    "error_detail": task_row["error_detail"] or "",
                    "started_at": _admin_iso(task_row["started_at"]),
                    "created_at": _admin_iso(task_row["created_at"]),
                    "updated_at": _admin_iso(task_row["updated_at"]),
                }
                for task_row in task_rows
            ]

            stat_row = await conn.fetchrow(
                f"""
                SELECT
                    COUNT(*) AS total_tasks,
                    COUNT(*) FILTER (WHERE {job_status_expr} = 'done') AS completed_tasks,
                    COUNT(*) FILTER (WHERE {job_status_expr} = 'error') AS error_tasks,
                    MAX(COALESCE(pj.updated_at, pj.created_at, pj.started_at)) AS last_active_at
                FROM pipeline_jobs pj
                WHERE lower({job_role_expr}) = lower($1)
                """,
                profile_row["role"] or role_key,
            )
            if stat_row:
                total_tasks = int(stat_row["total_tasks"] or 0)
                completed_tasks = int(stat_row["completed_tasks"] or 0)
                error_tasks = int(stat_row["error_tasks"] or 0)
                last_active_at = stat_row["last_active_at"]

    completed_ratio = (completed_tasks / total_tasks) if total_tasks else 0.0
    error_ratio = (error_tasks / total_tasks) if total_tasks else 0.0

    return {
        "agent": {
            "role": profile_row["role"] or role_key,
            "display_name": profile_row["display_name"] or profile_row["role"] or role_key,
            "base_model": profile_row["base_model"] or "",
            "allowed_intents": _admin_to_string_list(profile_row["allowed_intents"]),
            "max_tokens": int(profile_row["max_tokens"]) if profile_row["max_tokens"] is not None else None,
            "created_at": _admin_iso(profile_row["created_at"]),
            "recent_tasks_count": total_tasks,
            "last_active_at": _admin_iso(last_active_at),
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "error_tasks": error_tasks,
            "completed_ratio": round(completed_ratio, 4),
            "error_ratio": round(error_ratio, 4),
        },
        "recent_tasks": recent_tasks,
    }


@router.get("/admin/tasks")
async def list_admin_tasks(
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
):
    """Pipeline Runner 작업 목록 조회 — Task Board용."""
    from app.core.db_pool import get_pool

    normalized_status = _admin_task_status(status)
    offset = (page - 1) * page_size
    pool = get_pool()

    jobs_cte = f"""
        WITH jobs AS (
            SELECT
                job_id,
                project,
                {_TASK_BOARD_STATUS_SQL} AS board_status,
                phase,
                substring(COALESCE(instruction, '') from 1 for 100) AS instruction,
                model,
                worker_model,
                created_at,
                updated_at,
                error_detail
            FROM pipeline_jobs
        )
    """

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            jobs_cte + "SELECT COUNT(*) FROM jobs WHERE ($1::text IS NULL OR board_status = $1)",
            normalized_status,
        )
        rows = await conn.fetch(
            jobs_cte
            + """
            SELECT
                job_id,
                project,
                board_status,
                phase,
                instruction,
                model,
                worker_model,
                created_at,
                updated_at,
                error_detail
            FROM jobs
            WHERE ($1::text IS NULL OR board_status = $1)
            ORDER BY created_at DESC NULLS LAST, updated_at DESC NULLS LAST
            LIMIT $2 OFFSET $3
            """,
            normalized_status,
            page_size,
            offset,
        )

    return {
        "tasks": [
            {
                "job_id": row["job_id"],
                "project": row["project"] or "",
                "status": row["board_status"],
                "phase": row["phase"] or "",
                "instruction": row["instruction"] or "",
                "model": row["model"] or "",
                "worker_model": row["worker_model"] or "",
                "created_at": _admin_iso(row["created_at"]),
                "updated_at": _admin_iso(row["updated_at"]),
                "error_detail": row["error_detail"] or "",
            }
            for row in rows
        ],
        "total": total or 0,
        "page": page,
    }


@router.get("/admin/tasks/stats")
async def get_admin_task_stats():
    """Task Board용 상태별 집계."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            WITH jobs AS (
                SELECT {_TASK_BOARD_STATUS_SQL} AS board_status
                FROM pipeline_jobs
            )
            SELECT
                COUNT(*) FILTER (WHERE board_status = 'queued') AS queued,
                COUNT(*) FILTER (WHERE board_status = 'running') AS running,
                COUNT(*) FILTER (WHERE board_status = 'awaiting_approval') AS awaiting_approval,
                COUNT(*) FILTER (WHERE board_status = 'done') AS done,
                COUNT(*) FILTER (WHERE board_status = 'error') AS error,
                COUNT(*) AS total
            FROM jobs
            """
        )

    return {
        "queued": row["queued"] or 0,
        "running": row["running"] or 0,
        "awaiting_approval": row["awaiting_approval"] or 0,
        "done": row["done"] or 0,
        "error": row["error"] or 0,
        "total": row["total"] or 0,
    }


@router.get("/admin/tasks/{job_id}")
async def get_admin_task(job_id: str):
    """Task Board용 작업 상세 조회."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        has_logs_column = await _admin_column_exists(conn, "pipeline_jobs", "logs")
        row = await conn.fetchrow(
            f"""
            SELECT
                job_id,
                project,
                instruction,
                status AS raw_status,
                {_TASK_BOARD_STATUS_SQL} AS board_status,
                phase,
                cycle,
                max_cycles,
                model,
                worker_model,
                actual_model,
                size,
                {"logs AS log_snapshot," if has_logs_column else "NULL::text AS log_snapshot,"}
                result_output,
                git_diff,
                review_feedback,
                error_detail,
                started_at,
                created_at,
                updated_at
            FROM pipeline_jobs
            WHERE job_id = $1
            """,
            job_id,
        )
        if not row:
            raise HTTPException(404, "Task not found")

        log_rows = await conn.fetch(
            """
            SELECT id, log_type, content, phase, metadata, created_at
            FROM task_logs
            WHERE task_id = $1
            ORDER BY created_at DESC
            LIMIT 200
            """,
            job_id,
        )

    logs = [_admin_format_task_log(log_row) for log_row in reversed(log_rows)]

    return {
        "job_id": row["job_id"],
        "project": row["project"] or "",
        "status": row["board_status"],
        "raw_status": row["raw_status"] or "",
        "phase": row["phase"] or "",
        "cycle": row["cycle"] or 0,
        "max_cycles": row["max_cycles"] or 0,
        "instruction": row["instruction"] or "",
        "model": row["model"] or "",
        "worker_model": row["worker_model"] or "",
        "actual_model": row["actual_model"] or "",
        "size": row["size"] or "",
        "logs": logs,
        "log_snapshot": _admin_parse_json(row["log_snapshot"]),
        "result_output": row["result_output"] or "",
        "git_diff": row["git_diff"] or "",
        "review_feedback": row["review_feedback"] or "",
        "error_detail": row["error_detail"] or "",
        "started_at": _admin_iso(row["started_at"]),
        "created_at": _admin_iso(row["created_at"]),
        "updated_at": _admin_iso(row["updated_at"]),
    }


# ─── Emergency Kill-Switch (Q17) ────────────────────────────────────────────


class EmergencyActionRequest(BaseModel):
    action: str  # "kill" or "restore"
    reason: str = ""


@router.get("/admin/emergency")
async def get_emergency_status():
    from app.core.feature_flags import get_flag
    pool = _get_governance_pool()
    gov_enabled = await get_flag("governance_enabled", default=True)
    recent_actions: list[dict] = []
    try:
        async with pool.acquire() as conn:
            if await _governance_table_exists(conn, "governance_emergency_actions"):
                rows = await conn.fetch(
                    "SELECT action_type, triggered_by, reason, affected_scope, created_at, resolved_at, status "
                    "FROM governance_emergency_actions ORDER BY created_at DESC LIMIT 20"
                )
                recent_actions = [
                    {
                        "action_type": r["action_type"],
                        "triggered_by": r["triggered_by"],
                        "reason": r["reason"] or "",
                        "affected_scope": r["affected_scope"] or "",
                        "created_at": _admin_iso(r["created_at"]),
                        "resolved_at": _admin_iso(r["resolved_at"]),
                        "status": r["status"] or "",
                    }
                    for r in rows
                ]
    except Exception as exc:
        logger.warning("emergency_status_load_failed: %s", exc)
    return {"governance_enabled": gov_enabled, "recent_actions": recent_actions}


@router.post("/admin/emergency")
async def post_emergency_action(req: EmergencyActionRequest):
    from app.core.feature_flags import set_flag
    action = req.action.strip().lower()
    if action not in ("kill", "restore"):
        raise HTTPException(status_code=400, detail="action must be 'kill' or 'restore'")
    new_enabled = action == "restore"
    result = await set_flag("governance_enabled", new_enabled, changed_by="ceo_emergency")
    pool = _get_governance_pool()
    try:
        async with pool.acquire() as conn:
            if await _governance_table_exists(conn, "governance_emergency_actions"):
                await conn.execute(
                    "INSERT INTO governance_emergency_actions (action_type, triggered_by, reason, affected_scope, status) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    action, "ceo", req.reason or "", "governance_global", "active",
                )
    except Exception as exc:
        logger.warning("emergency_action_log_failed: %s", exc)
    return {"ok": True, "action": action, "governance_enabled": new_enabled, "flag_result": result}
# ─── Prompt Assets CRUD (5-Layer Architecture) ──────────────────────────────

class PromptAssetCreate(BaseModel):
    slug: str
    title: str
    layer_id: int
    content: str
    workspace_scope: list[str] = ["*"]
    intent_scope: list[str] = ["*"]
    target_models: list[str] = ["*"]
    role_scope: list[str] = ["*"]
    priority: int = 10
    model_variants: Optional[dict] = None

class PromptAssetUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    layer_id: Optional[int] = None
    workspace_scope: Optional[list[str]] = None
    intent_scope: Optional[list[str]] = None
    target_models: Optional[list[str]] = None
    role_scope: Optional[list[str]] = None
    priority: Optional[int] = None
    model_variants: Optional[dict] = None
    enabled: Optional[bool] = None


@router.get("/admin/prompt-assets")
async def list_prompt_assets(layer: Optional[int] = Query(None)):
    """5-Layer prompt_assets 전체 목록. layer 필터 가능."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        if not await _admin_table_exists(conn, "prompt_assets"):
            return {"assets": [], "count": 0, "layer_names": {}}
        if layer:
            rows = await conn.fetch(
                "SELECT * FROM prompt_assets WHERE layer_id = $1 ORDER BY layer_id, priority, slug", layer
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM prompt_assets ORDER BY layer_id, priority, slug"
            )
    layer_names = {1: "Global", 2: "Project", 3: "Role", 4: "Intent", 5: "AI Model"}
    assets = []
    for r in rows:
        assets.append({
            "id": r["id"],
            "slug": r["slug"],
            "title": r["title"],
            "layer_id": r["layer_id"],
            "layer_name": layer_names.get(r["layer_id"], f"L{r['layer_id']}"),
            "content": r["content"] or "",
            "chars": len(r["content"] or ""),
            "model_variants": r["model_variants"],
            "workspace_scope": r["workspace_scope"] or [],
            "intent_scope": r["intent_scope"] or [],
            "target_models": r["target_models"] or [],
            "role_scope": r["role_scope"] or [],
            "priority": r["priority"],
            "enabled": r["enabled"],
            "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return {"assets": assets, "count": len(assets), "layer_names": layer_names}


@router.post("/admin/prompt-assets")
async def create_prompt_asset(req: PromptAssetCreate):
    """새 prompt_asset 생성."""
    from app.core.db_pool import get_pool
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if req.layer_id not in (1, 2, 3, 4, 5):
        raise HTTPException(400, "layer_id must be 1-5")

    pool = get_pool()
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    async with pool.acquire() as conn:
        existing = await conn.fetchval("SELECT 1 FROM prompt_assets WHERE slug = $1", req.slug)
        if existing:
            raise HTTPException(409, f"slug '{req.slug}' already exists")
        await conn.execute(
            """INSERT INTO prompt_assets (slug, title, layer_id, content, model_variants,
               workspace_scope, intent_scope, target_models, role_scope, priority, enabled, created_by, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true,'CEO',$11,$11)""",
            req.slug, req.title, req.layer_id, req.content,
            json.dumps(req.model_variants) if req.model_variants else None,
            req.workspace_scope, req.intent_scope, req.target_models, req.role_scope,
            req.priority, now, now,
        )
    logger.info("admin.prompt_asset_created slug=%s layer=%d", req.slug, req.layer_id)
    return {"ok": True, "slug": req.slug}


@router.put("/admin/prompt-assets/{slug}")
async def update_prompt_asset(slug: str, req: PromptAssetUpdate):
    """prompt_asset 수정 + 버전 백업."""
    from app.core.db_pool import get_pool
    from datetime import datetime
    from zoneinfo import ZoneInfo

    pool = get_pool()
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    async with pool.acquire() as conn:
        old = await conn.fetchrow("SELECT * FROM prompt_assets WHERE slug = $1", slug)
        if not old:
            raise HTTPException(404, f"Asset '{slug}' not found")

        # Version backup
        if await _admin_table_exists(conn, "prompt_versions"):
            await conn.execute(
                "INSERT INTO prompt_versions (section_name, content, changed_by) VALUES ($1, $2, $3)",
                f"asset_{slug}", old["content"] or "", "CEO",
            )

        updates = []
        params = []
        idx = 1
        for field in ("title", "content", "layer_id", "workspace_scope", "intent_scope",
                       "target_models", "role_scope", "priority", "enabled"):
            val = getattr(req, field, None)
            if val is not None:
                updates.append(f"{field} = ${idx}")
                params.append(val)
                idx += 1
        if req.model_variants is not None:
            updates.append(f"model_variants = ${idx}")
            params.append(json.dumps(req.model_variants))
            idx += 1

        if not updates:
            return {"ok": True, "slug": slug, "changed": False}

        updates.append(f"updated_at = ${idx}")
        params.append(now)
        idx += 1
        params.append(slug)

        await conn.execute(
            f"UPDATE prompt_assets SET {', '.join(updates)} WHERE slug = ${idx}",
            *params,
        )

    from app.services.context_builder import _layer1_cache
    _layer1_cache.clear()
    logger.info("admin.prompt_asset_updated slug=%s", slug)
    return {"ok": True, "slug": slug, "changed": True}


@router.patch("/admin/prompt-assets/{slug}/toggle")
async def toggle_prompt_asset(slug: str):
    """prompt_asset enabled 토글."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE prompt_assets SET enabled = NOT enabled, updated_at = now() WHERE slug = $1 RETURNING enabled",
            slug,
        )
        if not row:
            raise HTTPException(404, f"Asset '{slug}' not found")
    logger.info("admin.prompt_asset_toggled slug=%s enabled=%s", slug, row["enabled"])
    return {"ok": True, "slug": slug, "enabled": row["enabled"]}


@router.delete("/admin/prompt-assets/{slug}")
async def delete_prompt_asset(slug: str):
    """prompt_asset 삭제."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM prompt_assets WHERE slug = $1", slug)
        if result == "DELETE 0":
            raise HTTPException(404, f"Asset '{slug}' not found")
    logger.info("admin.prompt_asset_deleted slug=%s", slug)
    return {"ok": True, "slug": slug}


@router.post("/admin/prompt-assets/preview")
async def preview_compiled_prompt(req: PromptPreviewRequest):
    """워크스페이스+인텐트 조합으로 5-Layer 컴파일된 최종 프롬프트 미리보기."""
    from app.services.prompt_compiler import PromptCompiler
    from app.core.prompts.token_profiler import estimate_tokens

    compiled = await PromptCompiler().compile(
        workspace_name=req.workspace_key,
        intent=req.intent,
        model="",
        session_id="preview",
        role=req.workspace_key,
        base_system_prompt=req.base_system_prompt,
    )
    return {
        "prompt": compiled.system_prompt,
        "total_chars": len(compiled.system_prompt),
        "total_tokens": estimate_tokens(compiled.system_prompt),
        "provenance": compiled.provenance,
    }
