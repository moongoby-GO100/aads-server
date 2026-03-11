"""
AADS-186A: CEO Chat 3계층 Context Engineering (재설계)
Layer 1: 정적 시스템 정보 (~1400 토큰, Anthropic Prompt Caching 대상)
         XML 섹션: role/capabilities/tools_available/rules/response_guidelines
         프롬프트 텍스트는 app/core/prompts/system_prompt_v2.py 에서 관리
Layer 2: 동적 런타임 정보 (~300 토큰, 매 요청 갱신)
Layer 3: 대화 히스토리 (~3000~5000 토큰, 5턴 이전 도구 결과 압축)
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# #34: Layer 1 캐시 (워크스페이스별 정적 프롬프트 — 변경 안 됨)
_layer1_cache: Dict[str, str] = {}

# ─── Layer 1: system_prompt_v2.py 에서 로드 ──────────────────────────────────

from app.core.prompts.system_prompt_v2 import build_layer1 as _build_layer1_raw, WS_LAYER1 as _WS_LAYER1


def build_layer1(ws_key: str, base_system_prompt: str = "") -> str:
    """#34: 캐시된 Layer 1 빌더. ws_key + base_prompt 조합으로 캐시."""
    cache_key = f"{ws_key}::{hash(base_system_prompt)}"
    if cache_key not in _layer1_cache:
        _layer1_cache[cache_key] = _build_layer1_raw(ws_key, base_system_prompt)
    return _layer1_cache[cache_key]


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


async def _build_memory_layer(
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    AADS 메모리 자동 주입 (memory_recall 모듈 사용).
    5개 섹션: 대화 요약 / CEO 선호 / 도구 전략 / 활성 Directive / 발견 사항
    총 2,000 토큰 이내. 실패 시 빈 문자열 (기본 프롬프트 유지).
    """
    try:
        from app.core.memory_recall import build_memory_context
        block = await build_memory_context(session_id=session_id, project_id=project_id)
        return f"\n{block}" if block else ""
    except Exception as e:
        logger.warning(f"[Memory] context_builder 메모리 주입 실패: {e}")
        return ""




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

_OBSERVATION_WINDOW = int(os.getenv("OBSERVATION_WINDOW_SIZE", "20"))  # 최근 N턴 도구 결과 유지, 이전은 마스킹

def _build_layer3_messages(
    raw_messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    대화 히스토리 구성 — Observation Masking 적용.
    - 최근 _OBSERVATION_WINDOW 턴: 도구 결과 유지
    - 이전 턴: 도구 결과를 플레이스홀더로 교체 (AI 추론/결정은 보존)
    - JetBrains Research 근거: 도구 출력만 마스킹하는 것이 LLM 요약보다 비용 대비 동등+
    """
    if not raw_messages:
        return []

    # 전체 메시지 사용 (이전의 20개 제한 제거 — 무한 대화 지원)
    messages = raw_messages
    result = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # _OBSERVATION_WINDOW 이전 메시지: 도구 결과 마스킹 (#5: 최소 정보 보존, #32: 마스킹 최적화)
        if i < len(messages) - _OBSERVATION_WINDOW and len(content) > 200:
            # 패턴 1: "[시스템 도구 조회 결과" 블록 → 도구명 + 첫 줄 보존
            if "[시스템 도구 조회 결과" in content:
                lines = content.split("\n")
                header_idx = next(
                    (j for j, l in enumerate(lines) if "[시스템 도구 조회 결과" in l), -1
                )
                if header_idx >= 0:
                    before = "\n".join(lines[:header_idx])
                    # 도구명 헤더 보존 + 결과 첫 줄 요약
                    tool_header = lines[header_idx]
                    first_line = lines[header_idx + 1].strip() if header_idx + 1 < len(lines) else ""
                    content = before + f"\n{tool_header}\n{first_line[:100]}...\n[이전 도구 결과 축소]"

            # 패턴 2: 매우 긴 assistant 메시지 중 코드 블록 축소 (앞 500자 보존)
            if role == "assistant" and len(content) > 3000:
                import re
                content = re.sub(
                    r'```[\s\S]{1500,}?```',
                    lambda m: m.group(0)[:500] + f"\n... [{len(m.group(0))}자 코드 블록 생략]\n```",
                    content
                )

        result.append({"role": role, "content": content})

    # 토큰 추정 및 추가 압축 (80K 초과 시)
    try:
        from app.services.context_compressor import estimate_tokens, mask_old_observations
        total_est = estimate_tokens(result, "")
        if total_est > 60000:
            # 더 공격적인 observation masking
            _aggressive_window = max(10, _OBSERVATION_WINDOW // 2)
            result = mask_old_observations(result, window=_aggressive_window)
            logger.info(f"layer3_aggressive_masking: {total_est}t → window={_aggressive_window}")
    except Exception:
        pass

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
    document_context: str = "",
) -> tuple[List[Dict[str, Any]], str]:
    """
    3+D 계층 컨텍스트 구성 → (messages, system_prompt) 반환.
    system_prompt: Layer 1 + Layer 2 + (Layer D: 임시 문서 컨텍스트)
    messages: Layer 3 대화 히스토리
    """
    ws_key = _normalize_workspace(workspace_name)

    # Layer 1 (동기 — system_prompt_v2 기반 XML 섹션)
    layer1 = build_layer1(ws_key, base_system_prompt)

    # Layer 2 + 메모리 주입을 병렬 실행 (AADS-CRITICAL-FIX #31)
    _project = _normalize_workspace(workspace_name)
    layer2, memory_layer = await asyncio.gather(
        _build_layer2_dynamic(workspace_name, db_conn=db_conn),
        _build_memory_layer(session_id=session_id, project_id=_project),
    )

    system_prompt = layer1 + "\n\n" + layer2 + memory_layer

    # Layer D: 임시 문서 컨텍스트 (현재 턴에만 주입, 다음 턴 제거)
    if document_context:
        system_prompt += "\n\n" + document_context
        from app.core.token_utils import estimate_tokens as _est_tokens
        _doc_tokens = _est_tokens(document_context)
        logger.info(f"[LayerD] ephemeral document injected: ~{_doc_tokens} tokens")

    # Layer 3 (동기 — CPU 연산만)
    messages = _build_layer3_messages(raw_messages)

    # 컨텍스트 크기 체크 — 80K 토큰 초과 시 구조화 요약 트리거
    # Layer 0/1/2 (system_prompt) is NEVER modified by compaction — only Layer 3 messages
    try:
        from app.services.context_compressor import estimate_tokens, needs_structured_summary
        _est = estimate_tokens(messages, system_prompt)
        if needs_structured_summary(messages, system_prompt, threshold=80000):
            logger.warning(f"context_builder: tokens={_est} > 80K, triggering structured summary")
            from app.services.compaction_service import check_and_compact
            messages = await check_and_compact(session_id, messages, db_conn=db_conn)
    except Exception as e:
        logger.debug(f"context_builder token check error: {e}")

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

    # Layer 2 (동적) + CKP 주입 + 메모리 주입 — 병렬 실행 (AADS-CRITICAL-FIX #31)
    _project = _normalize_workspace(workspace_name)
    layer2, ckp_layer, memory_layer = await asyncio.gather(
        _build_layer2_dynamic(workspace_name, db_conn=db_conn),
        _build_ckp_layer(workspace_name),
        _build_memory_layer(session_id=session_id, project_id=_project),
    )
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
