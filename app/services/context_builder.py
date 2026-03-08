"""
AADS-185: CEO Chat 전면 재설계 — 3계층 Context Engineering
Layer 1: 정적 시스템 정보 (~1500 토큰, Anthropic Prompt Caching 대상)
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

# ─── Layer 1: 정적 컨텍스트 (~1500 토큰, 캐싱 대상) ─────────────────────────

_LAYER1_STATIC = """당신은 AADS(Autonomous AI Development System)의 CEO moongoby 전용 AI 어시스턴트입니다.

## 역할 계층
CEO(moongoby) → PM(Claude) → 개발자(Claude) → QA(Claude) → Ops(Claude)
AADS는 이 역할 분리 멀티 AI 에이전트 자율 개발 시스템입니다.

## 6개 프로젝트
| 프로젝트 | 설명 | 서버 | Task ID |
|---------|------|------|---------|
| AADS | 자율 AI 개발 시스템 본체 | 서버68 | AADS-xxx |
| SF | ShortFlow 숏폼 동영상 자동화 | 서버114:7916 | SF-xxx |
| KIS | 자동매매 시스템 | 서버211 | KIS-xxx |
| GO100 | 빡억이 투자분석 | 서버211 | GO100-xxx |
| NTV2 | NewTalk V2 소셜플랫폼 | 서버114 | NT-xxx |
| NAS | 이미지처리 | Cafe24 | NAS-xxx |

## 3개 서버
- 서버68 (68.183.183.11): AADS Backend(FastAPI 0.115) + Dashboard(Next.js 16) + PostgreSQL 15
- 서버211 (211.188.51.113): Hub, Bridge, KIS/GO100 실행 환경
- 서버114 (116.120.58.155): SF/NTV2/NAS 실행 환경 (포트 7916)

## 보안 정책 (절대 금지)
- DB DROP/TRUNCATE 명령 실행 금지
- .env, secret, key 파일 커밋 금지
- 서비스 무단 재시작 금지 (CEO 승인 필수)
- 프로세스 탐색 시 /proc grep -r 금지 (pgrep, ps, lsof 사용)

## 도구 사용 원칙
질문에 답하기 전에 관련 도구를 호출하여 실제 데이터를 확인하세요.
- 서버 상태 질문 → health_check 도구 호출
- 작업 현황 질문 → dashboard_query 또는 task_history 도구 호출
- 웹/최신 정보 질문 → web_search_brave 도구 호출
- 파일 내용 질문 → read_github_file 도구 호출
- DB 조회 필요 → query_database 도구 호출 (SELECT만 허용)

## 지시서 포맷 (>>>DIRECTIVE_START 블록)
필수 필드: TASK_ID, TITLE, PRIORITY(P0-P3), SIZE(XS/S/M/L/XL), MODEL(haiku/sonnet/opus)
선택 필드: DEPENDS_ON, parallel_group, subagents, review_required, ASSIGNEE, files_owned
보고: GitHub 브라우저 URL 포함, 비용($) 명시, HANDOVER.md 업데이트 의무(R-001)

## 워크스페이스별 역할
- [CEO] 통합지시: 전략 지시, 지시서 작성, 파이프라인 모니터링, Deep Research
- [AADS] 워크스페이스: FastAPI/Next.js/PostgreSQL 개발, 파이프라인 운영
- [SF] 워크스페이스: 숏폼 동영상 자동화 개발
- [KIS] 워크스페이스: 자동매매 시스템 개발
- [GO100] 워크스페이스: 투자분석 시스템 개발
- [NTV2] 워크스페이스: Laravel 12 소셜플랫폼 개발
- [NAS] 워크스페이스: 이미지처리 시스템 개발

## 응답 스타일
- 직접적, 핵심만, 미사여구 없음
- 검증 없이 완료 선언 금지
- GitHub 브라우저 경로로 보고 (R-008)
- 비용 효율 최우선"""

# ─── 워크스페이스별 추가 Layer 1 컨텍스트 ─────────────────────────────────────

_WS_LAYER1: Dict[str, str] = {
    "CEO": (
        "\n## CEO 워크스페이스 추가 컨텍스트\n"
        "D-039: 지시서 발행 전 GET /api/v1/directives/preflight 호출 필수\n"
        "D-022: 지시서 포맷 v2.0 (필수6 + 선택7 필드)\n"
        "D-027: Worktree 병렬 — parallel_group 필드 감지 시 자동 분기\n"
        "D-028: 서브에이전트 — subagents 필드 기반 에이전트 활성화\n"
        "파이프라인: auto_trigger.sh → claude_exec.sh → RESULT → done 폴더\n"
        "대시보드: https://aads.newtalk.kr/ | GitHub: https://github.com/moongoby-GO100/"
    ),
    "AADS": (
        "\n## AADS 워크스페이스 추가 컨텍스트\n"
        "서버68: FastAPI 0.115 + Next.js 16 + PostgreSQL 15 + Docker Compose\n"
        "API: /api/v1/chat/*, /api/v1/ops/*, /api/v1/directives/*, /api/v1/managers\n"
        "파이프라인: /root/aads/scripts/auto_trigger.sh → claude_exec.sh\n"
        "D-039: 지시서 발행 전 GET /api/v1/directives/preflight 필수\n"
        "배포: docker compose -f docker-compose.prod.yml up -d --build aads-server"
    ),
    "SF": (
        "\n## SF 워크스페이스 추가 컨텍스트\n"
        "서버114 (116.120.58.155), 포트 7916. 숏폼 동영상 자동화.\n"
        "Task ID: SF-xxx."
    ),
    "KIS": (
        "\n## KIS 워크스페이스 추가 컨텍스트\n"
        "서버211 (211.188.51.113). KIS API 연동 자동매매.\n"
        "Task ID: KIS-xxx."
    ),
    "GO100": (
        "\n## GO100 워크스페이스 추가 컨텍스트\n"
        "서버211 (211.188.51.113). 빡억이 투자분석.\n"
        "Task ID: GO100-xxx."
    ),
    "NTV2": (
        "\n## NTV2 워크스페이스 추가 컨텍스트\n"
        "서버114 (116.120.58.155). Laravel 12 소셜플랫폼.\n"
        "Task ID: NT-xxx."
    ),
    "NAS": (
        "\n## NAS 워크스페이스 추가 컨텍스트\n"
        "Cafe24 + Flask/FastAPI 이미지처리.\n"
        "Task ID: NAS-xxx."
    ),
}


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

    # Layer 1
    layer1 = _LAYER1_STATIC + _WS_LAYER1.get(ws_key, "")
    if base_system_prompt:
        layer1 += f"\n\n## 워크스페이스 지시\n{base_system_prompt}"

    # Layer 2
    layer2 = await _build_layer2_dynamic(workspace_name, db_conn=db_conn)

    system_prompt = layer1 + "\n\n" + layer2

    # Layer 3
    messages = _build_layer3_messages(raw_messages)

    return messages, system_prompt


def build_system_context(workspace_name: str) -> str:
    """하위 호환 동기 버전 (Layer 1 + 현재 시각만)."""
    ws_key = _normalize_workspace(workspace_name)
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    date_str = now.strftime("%Y-%m-%d %H:%M KST (%A)")
    layer1 = _LAYER1_STATIC + _WS_LAYER1.get(ws_key, "")
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

    # Layer 1 (정적 — 캐싱 대상)
    layer1 = _LAYER1_STATIC + _WS_LAYER1.get(ws_key, "")
    if base_system_prompt:
        layer1 += f"\n\n## 워크스페이스 지시\n{base_system_prompt}"

    # Layer 2 (동적)
    layer2 = await _build_layer2_dynamic(workspace_name, db_conn=db_conn)

    # Anthropic 시스템 블록 (cache_control = Prompt Caching 대상)
    system_blocks: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": layer1,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": layer2,
        },
    ]
    system_text = layer1 + "\n\n---\n\n" + layer2

    return ContextResult(
        system_blocks=system_blocks,
        system_text=system_text,
        workspace_name=ws_key,
        workspace_id=workspace_id,
        layer2_text=layer2,
    )
