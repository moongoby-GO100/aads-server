"""
AADS-186A: CEO Chat 3계층 Context Engineering (재설계)
Layer 1: 정적 시스템 정보 (~1400 토큰, Anthropic Prompt Caching 대상)
         XML 섹션: role/capabilities/tools_available/rules/response_guidelines
         프롬프트 텍스트는 app/core/prompts/system_prompt_v2.py 에서 관리
Layer 2: 동적 런타임 정보 (~300 토큰, 매 요청 갱신)
Layer 3: 대화 히스토리 (~3000~5000 토큰, 5턴 이전 도구 결과 압축)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ─── Layer 1: system_prompt_v2.py 에서 로드 ──────────────────────────────────

from app.core.prompts.system_prompt_v2 import build_layer1, WS_LAYER1 as _WS_LAYER1


# ─── Layer 2: 동적 컨텍스트 (매 요청 갱신) ──────────────────────────────────

async def _build_layer2_dynamic(
    workspace_name: str,
    db_conn=None,
) -> str:
    """현재 시간 + 최근 완료 작업 + pending/running 수."""
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    date_str = now.strftime("%Y-%m-%d %H:%M KST (%A)")

    parts = [f"## 현재 상태 (동적)\n현재 시각: {date_str}"]

    if db_conn:
        try:
            rows = await db_conn.fetch(
                """
                SELECT task_id, title, status, completed_at
                FROM directive_lifecycle
                WHERE status = 'done'
                ORDER BY completed_at DESC
                LIMIT 3
                """,
            )
            if rows:
                recent = ", ".join(
                    f"{r['task_id']}({(r['title'] or '')[:20]})"
                    for r in rows
                )
                parts.append(f"최근 완료: {recent}")

            cnt_row = await db_conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending_cnt,
                    COUNT(*) FILTER (WHERE status = 'running') AS running_cnt
                FROM directive_lifecycle
                WHERE status IN ('pending', 'running')
                """
            )
            if cnt_row:
                parts.append(
                    f"대기: {cnt_row['pending_cnt']}건 | 실행중: {cnt_row['running_cnt']}건"
                )
        except Exception as e:
            logger.debug(f"context_builder layer2 db error: {e}")

    ws_display = workspace_name or "CEO"
    parts.append(f"현재 워크스페이스: {ws_display}")

    return "\n".join(parts)


async def _build_ckp_layer(workspace_name: str) -> str:
    """AADS-186B/D: CKP 요약을 <codebase_knowledge> 태그로 반환.
    AADS/CEO 워크스페이스: AADS 프로젝트 CKP 주입.
    원격 프로젝트 워크스페이스 (KIS/GO100/SF/NTV2/NAS): 해당 프로젝트 CKP 주입.
    """
    ws = (workspace_name or "").upper()
    _SUPPORTED_WS = {"AADS", "CEO", "KIS", "GO100", "SF", "NTV2", "NAS"}
    if ws not in _SUPPORTED_WS:
        return ""
    try:
        from app.services.ckp_manager import CKPManager
        mgr = CKPManager(db_conn=None)
        project_key = "AADS" if ws in ("AADS", "CEO") else ws
        summary = await mgr.get_ckp_summary(project_key, max_tokens=1500)
        if summary:
            return f"\n<codebase_knowledge>\n{summary}\n</codebase_knowledge>"
    except Exception as e:
        logger.debug(f"[CKP] context_builder CKP 주입 실패: {e}")
    return ""


def _build_tool_guide_layer() -> str:
    """AADS-186D: 도구 카테고리 안내 텍스트 반환 (Layer 1 보조)."""
    try:
        from app.services.tool_registry import TOOL_CATEGORY_GUIDE
        return f"\n\n{TOOL_CATEGORY_GUIDE}"
    except Exception as e:
        logger.debug(f"[ToolGuide] 도구 안내 로드 실패: {e}")
        return ""


async def _build_memory_layer() -> str:
    """
    AADS-186E-2: 영속 메모리 주입.
    <recent_sessions>: 최근 3개 세션 노트 (Layer 2)
    <learned_patterns>: CEO 선호도 + 알려진 이슈 (Layer 4)
    186B의 <codebase_knowledge>와 별도 XML 태그 사용.
    """
    parts: list[str] = []
    try:
        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()

        # 최근 세션 노트 (Layer 2)
        notes = await mgr.get_recent_notes(3)
        if notes:
            note_lines = []
            for note in notes:
                line = f"- {note.summary}"
                if note.key_decisions:
                    line += f" | 결정: {', '.join(note.key_decisions[:2])}"
                if note.action_items:
                    line += f" | 액션: {', '.join(note.action_items[:2])}"
                note_lines.append(line)
            parts.append(
                "<recent_sessions>\n"
                + "\n".join(note_lines)
                + "\n</recent_sessions>"
            )

        # 메타 기억 요약 (Layer 4) — ai_observations + ai_meta_memory 통합
        meta = await mgr.build_meta_context(max_tokens=500)
        if meta:
            parts.append(f"<meta_memory>\n{meta}\n</meta_memory>")

    except Exception as e:
        logger.debug(f"[Memory] context_builder 메모리 주입 실패: {e}")

    return "\n" + "\n".join(parts) if parts else ""




async def _build_semantic_code_layer(
    last_user_message: str,
    workspace_name: str = "",
) -> str:
    """
    AADS-188B: 시맨틱 코드 컨텍스트 주입.
    CEO 질의에서 코드 관련 키워드 감지 시 ChromaDB에서 관련 청크 검색·삽입.
    최대 5개 청크, 약 3000토큰 이하.
    ChromaDB 미초기화 / 임베딩 실패 시 graceful skip.
    """
    if not last_user_message:
        return ""
    _CODE_KEYWORDS = (
        "어디", "함수", "클래스", "로직", "코드", "파일",
        "where", "function", "class", "logic", "code", "file",
        "구현", "메서드", "찾아", "검색", "어떻게",
    )
    if not any(kw in last_user_message for kw in _CODE_KEYWORDS):
        return ""
    try:
        from app.services.semantic_code_search import SemanticCodeSearch
        svc = SemanticCodeSearch()
        ws = (workspace_name or "").upper()
        project: Optional[str] = ws if ws in ("AADS", "KIS", "GO100", "SF", "NTV2", "NAS") else None
        return await svc.build_code_context(last_user_message, project=project)
    except Exception as e:
        logger.debug(f"[SemanticCode] context_builder 시맨틱 검색 실패: {e}")
        return ""

# ─── Layer 3: 대화 히스토리 ────────────────────────────────────────────────

COMPRESS_AFTER_TURNS = 5

def _build_layer3_messages(
    raw_messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """최근 20개 메시지, 5턴 이전 도구 결과 압축."""
    if not raw_messages:
        return []

    messages = raw_messages[-20:]
    result = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 5턴 이전 메시지의 도구 결과 압축
        if i < len(messages) - COMPRESS_AFTER_TURNS:
            if "[시스템 도구 조회 결과" in content:
                lines = content.split("\n")
                header_idx = next(
                    (j for j, l in enumerate(lines) if "[시스템 도구 조회 결과" in l), -1
                )
                if header_idx >= 0:
                    before = "\n".join(lines[:header_idx])
                    content = before + "\n[도구 결과 생략 — 이전 조회]"

        result.append({"role": role, "content": content})

    return result


# ─── 정규화 ─────────────────────────────────────────────────────────────────

def _normalize_workspace(name: str) -> str:
    ws = (name or "").upper().strip()
    if ws.startswith("["):
        end = ws.find("]")
        if end != -1:
            ws = ws[1:end].strip()
    for key in _WS_LAYER1:
        if key in ws:
            return key
    return ws


# ─── 메인 빌더 ──────────────────────────────────────────────────────────────

async def build_messages_context(
    workspace_name: str,
    session_id: str,
    raw_messages: List[Dict[str, Any]],
    base_system_prompt: str = "",
    db_conn=None,
) -> tuple[List[Dict[str, Any]], str]:
    """
    3계층 컨텍스트 구성 → (messages, system_prompt) 반환.
    system_prompt: Layer 1 + Layer 2
    messages: Layer 3 대화 히스토리
    """
    ws_key = _normalize_workspace(workspace_name)

    # Layer 1 (system_prompt_v2 기반 XML 섹션)
    layer1 = build_layer1(ws_key, base_system_prompt)

    # Layer 2 (동적 상태)
    layer2 = await _build_layer2_dynamic(workspace_name, db_conn=db_conn)

    # Layer 2+: 메모리 주입 (AADS-186E-2) — 186B CKP와 별도 XML 태그
    memory_layer = await _build_memory_layer()

    system_prompt = layer1 + "\n\n" + layer2 + memory_layer

    # Layer 3
    messages = _build_layer3_messages(raw_messages)

    return messages, system_prompt


def build_system_context(workspace_name: str) -> str:
    """하위 호환 동기 버전 (Layer 1 + 현재 시각만)."""
    ws_key = _normalize_workspace(workspace_name)
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    date_str = now.strftime("%Y-%m-%d %H:%M KST (%A)")
    layer1 = build_layer1(ws_key)
    return f"현재 시각: {date_str}\n\n{layer1}\n"


# ─── ContextResult + build() — AADS-185 신규 ────────────────────────────────

@dataclass
class ContextResult:
    """3계층 컨텍스트 빌드 결과 (model_selector.py 에서 소비)."""
    # Anthropic Prompt Caching 포맷: Layer 1에 cache_control 적용
    system_blocks: List[Dict[str, Any]] = field(default_factory=list)
    # 플랫 텍스트 버전 (LiteLLM / Gemini용)
    system_text: str = ""
    workspace_name: str = "CEO"
    workspace_id: str = ""
    layer2_text: str = ""


async def build(
    workspace_name: str,
    session_id: str,
    db_conn=None,
    workspace_id: str = "",
    base_system_prompt: str = "",
) -> ContextResult:
    """
    AADS-185-A1: 비동기 3계층 컨텍스트 빌드 → ContextResult 반환.
    system_blocks: Anthropic Tool Use API용 (cache_control 포함)
    system_text: LiteLLM/Gemini용 플랫 문자열
    """
    ws_key = _normalize_workspace(workspace_name)

    # Layer 1 (정적 — 캐싱 대상, system_prompt_v2 기반 XML 섹션 + 도구 안내)
    layer1_base = build_layer1(ws_key, base_system_prompt)
    tool_guide = _build_tool_guide_layer()  # AADS-186D: 도구 카테고리 안내
    layer1 = layer1_base + tool_guide

    # Layer 2 (동적) + CKP 주입 (AADS-186B/D) + 메모리 주입 (AADS-186E-2)
    layer2 = await _build_layer2_dynamic(workspace_name, db_conn=db_conn)
    ckp_layer = await _build_ckp_layer(workspace_name)
    memory_layer = await _build_memory_layer()  # <recent_sessions> + <learned_patterns>
    layer2_full = layer2 + ckp_layer + memory_layer

    # AADS-186D: Prompt Caching 최적화 적용
    try:
        from app.core.cache_config import build_cached_system_blocks
        system_blocks = build_cached_system_blocks(layer1, layer2, ckp_layer + memory_layer)
    except Exception:
        # fallback: 기존 방식
        system_blocks = [
            {
                "type": "text",
                "text": layer1,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": layer2_full,
            },
        ]

    system_text = layer1 + "\n\n---\n\n" + layer2_full

    return ContextResult(
        system_blocks=system_blocks,
        system_text=system_text,
        workspace_name=ws_key,
        workspace_id=workspace_id,
        layer2_text=layer2_full,
    )
