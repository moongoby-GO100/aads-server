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
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# #34: Layer 1 캐시 (워크스페이스별 정적 프롬프트 — 변경 안 됨)
_layer1_cache: Dict[str, str] = {}

# ─── TTL 캐시: Layer 2 + 메모리 (TTFT 단축) ─────────────────────────────────
_layer_cache: Dict[str, Tuple[float, str]] = {}
_CACHE_TTL = 60  # 60초 — 같은 세션 내 반복 빌드 방지


async def _get_cached_or_build(key: str, builder_coro) -> str:
    """TTL 기반 async 빌더 캐시. TTL 내 히트 시 코루틴 실행 생략."""
    now = time.monotonic()
    cached = _layer_cache.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    result = await builder_coro
    _layer_cache[key] = (now, result)
    return result

# ─── Layer 1: system_prompt_v2.py 에서 로드 ──────────────────────────────────

from app.core.prompts.system_prompt_v2 import build_layer1 as _build_layer1_raw, build_layer4 as _build_layer4, WS_LAYER1 as _WS_LAYER1
from app.services.prompt_compiler import PromptCompiler


def build_layer1(ws_key: str, base_system_prompt: str = "", intent: str = "", **_kwargs) -> str:
    """#34: 캐시된 Layer 1 빌더. ws_key + base_prompt + intent 조합으로 캐시."""
    cache_key = f"{ws_key}::{intent}::{hashlib.sha256(base_system_prompt.encode()).hexdigest()[:16]}"
    if cache_key not in _layer1_cache:
        _layer1_cache[cache_key] = _build_layer1_raw(ws_key, base_system_prompt, intent=intent)
    return _layer1_cache[cache_key]


# ─── Layer 2: 동적 컨텍스트 (매 요청 갱신) ──────────────────────────────────

async def _build_layer2_dynamic(
    workspace_name: str,
    db_conn=None,
    session_id: str = "",
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

    # 서버 사실과 지금 죽어 있는 것.
    #
    # 2026-09-14 대표님 지적: "서버정보 등 최신 변경사항이 세션에 주입이
    # 안 되나? 왜 옛날 정보를 보고하지". 실측해 보니 Layer 2 에는 시각과
    # 지시 건수뿐이고 **서버에 관한 것이 하나도 없었다.** 세션이 도구를
    # 직접 부르지 않으면 과거 대화에서 주워온 옛 수치를 말하게 된다.
    #
    # 정적 프롬프트에는 "## 3개 서버" 라고 손으로 적혀 있었는데 등록부에는
    # 4대가 있었다(진아 서버가 같은 날 추가됨). 손으로 적은 목록은 반드시
    # 틀어지므로 여기서 등록부를 읽는다.
    if db_conn:
        try:
            srv = await db_conn.fetch(
                "SELECT server_key, ip, coalesce(project,'') AS project, "
                "       coalesce(description,'') AS description "
                "FROM server_registry ORDER BY server_key"
            )
            if srv:
                lines = [
                    f"- {r['server_key']} ({r['ip']})"
                    + (f" {r['project']}" if r["project"] else "")
                    + (f" — {r['description'][:46]}" if r["description"] else "")
                    for r in srv
                ]
                parts.append(f"\n## 서버 {len(srv)}대 (등록부)\n" + "\n".join(lines))
        except Exception as e:
            logger.debug(f"context_builder 서버 등록부 조회 실패: {e}")

        try:
            # **최근에 검사한 것만 본다.** 오래된 행을 그대로 읽으면 낡은
            # 판정을 현재처럼 말하게 된다 — 고치려던 문제를 그대로 되풀이한다.
            down = await db_conn.fetch(
                """
                SELECT server, service_name, consecutive_failures,
                       to_char(last_check AT TIME ZONE 'Asia/Seoul', 'HH24:MI') AS at
                FROM monitored_services
                WHERE enabled AND last_status = 'fail'
                  AND last_check > NOW() - interval '15 minutes'
                ORDER BY server, service_name
                LIMIT 12
                """
            )
            checked = await db_conn.fetchval(
                "SELECT to_char(max(last_check) AT TIME ZONE 'Asia/Seoul', 'HH24:MI') "
                "FROM monitored_services WHERE enabled"
            )
            if down:
                # **연속 실패가 아주 많으면 방금 죽은 게 아니다.**
                # 감시 주기가 30초이므로 1,000회는 8시간, 100,000회는 35일이다.
                # 2026-09-14 실측에서 contabo14 서비스 12개가 10만 회대로
                # 잡혀 있었는데 전부 정상 가동 중이었다 — 감시기의 서버 키가
                # 안 맞아 SSH 를 시도조차 못 하고 있었다.
                #
                # 그런 행을 "죽었다" 고 주입하면 낡은 거짓을 현재처럼 말하게
                # 된다. 고치려던 문제를 그대로 되풀이하는 셈이다. 나눠 적는다.
                fresh = [r for r in down if (r["consecutive_failures"] or 0) < 1000]
                stale = [r for r in down if (r["consecutive_failures"] or 0) >= 1000]
                if fresh:
                    items = ", ".join(
                        f"{r['server']}:{r['service_name']}({r['consecutive_failures']}회)"
                        for r in fresh
                    )
                    parts.append(f"\n## 지금 죽어 있는 서비스 ({checked} 검사)\n{items}")
                if stale:
                    names = ", ".join(f"{r['server']}:{r['service_name']}" for r in stale)
                    parts.append(
                        f"\n## 장기 실패로 기록된 것 {len(stale)}건 — 감시 설정 의심\n"
                        f"{names}\n"
                        "연속 실패가 1,000회(8시간)를 넘었다. 실제 장애보다 "
                        "감시 항목 설정이 틀렸을 가능성이 높다. 단정하지 말고 "
                        "필요하면 직접 확인해라."
                    )
            elif checked:
                parts.append(f"\n## 서비스 상태\n{checked} 검사 기준 전부 정상")
        except Exception as e:
            logger.debug(f"context_builder 서비스 상태 조회 실패: {e}")

    # 같이 일하는 담당 명단.
    #
    # 2026-09-14 실측. 명단이 **역할 프롬프트 8개에 각각 박혀 있었다.**
    # 담당을 한 명 추가하면 나머지 7개를 전부 고쳐야 하고, 안 고치면 새
    # 담당이 외톨이가 된다 — 자기는 남을 부를 수 있는데 남들은 그를 모른다.
    #
    # 오늘 같은 문제를 다섯 번 고쳤다(통합지시 목록 네 벌, 감시기 서버 지도,
    # 골 스케줄러 프로젝트, /goals 필터, Layer 1 의 "3개 서버").
    # **손으로 적은 목록은 반드시 갈라진다.** 여기서 읽어 넣는다.
    if db_conn and session_id:
        try:
            # **목표별로 나눈다.** 한 세션이 목표 두 개에 참여할 수 있는데,
            # 처음 구현은 `DISTINCT ON (s.id)` 로 세션당 한 행만 남기고 첫
            # 행의 목표 제목을 통째로 붙였다. 실측에서 두 목표의 담당이 한
            # 목록으로 섞이고 제목은 엉뚱한 쪽이 붙었다.
            #
            #   ## 같이 일하는 담당 (__두번째)
            #   - `CTO` — 백억이 기능테스트        ← 두번째 목표 소속
            #   - `StrategyCardLead` — …           ← #310 소속
            #
            # 이러면 담당이 다른 목표 사람에게 말을 건다.
            team = await db_conn.fetch(
                """
                SELECT DISTINCT ON (g.id, s.id)
                       g.id::text AS goal_id,
                       g.title AS goal_title,
                       COALESCE(s.role_key, '') AS role_key,
                       s.title AS session_title,
                       (s.id = $2::uuid) AS is_me,
                       (s.role_key LIKE '%Lead') AS is_lead
                FROM goal_task_links me
                JOIN goals g ON g.id = me.goal_id
                JOIN goal_task_links peer ON peer.goal_id = g.id
                                         AND peer.task_type = 'chat_session'
                                         AND COALESCE(peer.link_state,'active') = 'active'
                JOIN chat_sessions s ON s.id = peer.task_id::uuid
                WHERE me.task_type = 'chat_session' AND me.task_id = $1
                  AND COALESCE(me.link_state,'active') = 'active'
                  AND g.status IN ('draft','active','blocked')
                  AND s.role_key IS NOT NULL
                ORDER BY g.id, s.id, g.created_at
                """,
                session_id, session_id,
            )
            by_goal: dict[str, list[str]] = {}
            titles: dict[str, str] = {}
            for r in team:
                gid = r["goal_id"]
                titles[gid] = r["goal_title"]
                if r["is_me"]:
                    by_goal.setdefault(gid, [])
                    continue
                mark = "주도" if r["is_lead"] else "    "
                by_goal.setdefault(gid, []).append(
                    f"- {mark} `{r['role_key']}` — {r['session_title']}"
                )
            blocks = [
                f"\n## 같이 일하는 담당 ({titles[gid]})\n" + "\n".join(lines)
                for gid, lines in by_goal.items() if lines
            ]
            if blocks:
                parts.append(
                    "".join(blocks)
                    + "\n\n`ask_session(target=\"역할키\", question=..., context=...)` 로 "
                      "부른다. 한글 이름으로 불러도 찾는다. 답은 나중에 이 대화로 "
                      "자동으로 들어온다 — 기다리지 말고 다른 일을 계속해라.\n"
                      "**목표가 둘 이상이면 같은 목표의 담당에게 물어라** — "
                      "다른 목표 사람에게 물으면 맥락이 안 맞는다."
                )
        except Exception as e:
            # debug 로 두면 안 된다. 2026-09-14 여기서 asyncpg 타입 충돌
            # (`$1` 을 text 와 uuid 로 동시 사용)이 났는데 debug 라서 조용히
            # 빈 명단이 나갔다 — 오늘 내내 고친 그 패턴을 내가 다시 만들었다.
            logger.warning("context_builder 담당 명단 조회 실패: %s", str(e)[:200])

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




# Auto-RAG 를 기다리는 상한. 넘으면 **첫 토큰을 먼저 낸다.**
#
# 2026-09-14 실측. 질문 임베딩 한 번이 CPU Ollama 에서 2,558ms 다. 그동안
# 화면은 비어 있다. 그런데 근거가 **첫 문장에 필요한 경우는 드물다** —
# 대개 답을 시작한 뒤 중간에 쓰인다.
#
# 700ms 인 이유: 짧은 질문이나 캐시에 있는 것은 그 안에 끝난다(실측
# 0.1초). 긴 질문만 밀린다.
_AUTO_RAG_WAIT_MS = int(os.getenv("AUTO_RAG_WAIT_MS", "700"))

# 늦게 끝난 근거를 담아 둔다. **버리지 않는다** — 버리면 그 질문에 대한
# 근거가 영영 안 붙는다. 다음 턴에 붙인다.
_late_rag: dict[str, str] = {}
_LATE_RAG_MAX = 200


def _take_late_rag(session_id: str) -> str:
    """지난 턴에 늦어서 못 붙인 근거를 꺼낸다(한 번만)."""
    return _late_rag.pop(session_id, "")


# Auto-RAG 가 실제로 몇 ms 를 쓰는지는 이 빌더 안쪽에서만 알 수 있다.
# ContextVar 는 쓸 수 없다 — `asyncio.gather` 로 갈라진 태스크에서 set 해도
# 부모 컨텍스트로 돌아오지 않는다. `_late_rag` 와 같은 세션 키 방식으로 둔다.
_rag_ms: Dict[str, int] = {}
_RAG_MS_MAX = 200


def take_rag_ms(session_id: str) -> Optional[int]:
    """이번 턴 Auto-RAG 소요(ms). 한 번 꺼내면 지운다."""
    return _rag_ms.pop(session_id, None)


async def _build_auto_rag_layer_bounded(
    last_user_message: str,
    session_id: str,
    project: Optional[str] = None,
    current_message_ids: Optional[set[str]] = None,
) -> str:
    """상한 안에 끝나면 붙이고, 늦으면 다음 턴으로 넘긴다.

    **조용히 빼지 않는다.** 근거 없이 답한 것을 대표님이 모르시면 안 된다 —
    2026-09-14 하루 종일 고친 것이 전부 그런 종류였다.
    """
    carried = _take_late_rag(session_id)
    _rag_t0 = time.perf_counter()

    def _record_rag_ms() -> None:
        if len(_rag_ms) < _RAG_MS_MAX:
            _rag_ms[session_id] = int((time.perf_counter() - _rag_t0) * 1000)

    task = asyncio.create_task(
        _build_auto_rag_layer(last_user_message, session_id, project, current_message_ids)
    )
    # `wait_for(shield(...))` 는 상한에서 취소 예외를 그대로 올린다(실측).
    # `asyncio.wait` 는 **작업을 건드리지 않고** 기다리기만 한다 — 늦은
    # 근거를 뒤에서 마저 끝내려면 이쪽이어야 한다.
    done, _pending = await asyncio.wait({task}, timeout=_AUTO_RAG_WAIT_MS / 1000.0)
    if task in done:
        try:
            block = task.result()
        except Exception as exc:
            logger.debug("auto_rag_failed: %s", str(exc)[:160])
            block = ""
        _record_rag_ms()
        return (carried + block) if carried else block

    # 늦은 것은 백그라운드에서 끝내 다음 턴에 쓰도록 담아 둔다.
    def _stash(t: asyncio.Task) -> None:
        # `CancelledError` 는 `Exception` 이 아니라 `BaseException` 이다.
        # `except Exception` 으로 잡으면 콜백 밖으로 새어 나가 이벤트 루프
        # 예외 핸들러에 찍힌다 — 실측에서 그렇게 됐다.
        try:
            out = t.result()
        except BaseException:
            return
        if out and len(_late_rag) < _LATE_RAG_MAX:
            _late_rag[session_id] = out

    task.add_done_callback(_stash)
    logger.info(
        "auto_rag_deferred session=%s wait_ms=%s", session_id[:8], _AUTO_RAG_WAIT_MS
    )
    note = (
        "\n<auto_rag_context>\n## 관련 과거 컨텍스트\n"
        "근거 검색이 늦어 이번 답에는 반영되지 않았다. 다음 답부터 반영된다.\n"
        "지금 답이 과거 기록에 의존해야 하는 내용이면 그렇다고 밝혀라.\n"
        "</auto_rag_context>"
    )
    _record_rag_ms()
    return (carried + note) if carried else note


async def _build_auto_rag_layer(
    last_user_message: str,
    session_id: str,
    project: Optional[str] = None,
    current_message_ids: Optional[set[str]] = None,
) -> str:
    """F1/F3: Auto-RAG — 매 턴 사용자 메시지에 대한 시맨틱 검색 결과 주입 (Layer 4.5)."""
    try:
        from app.services.auto_rag import build_auto_rag_context
        block = await build_auto_rag_context(
            user_message=last_user_message,
            session_id=session_id,
            project=project,
            current_message_ids=current_message_ids,
        )
        return f"\n{block}" if block else ""
    except Exception as e:
        logger.debug(f"[AutoRAG] context_builder Auto-RAG 주입 실패: {e}")
        return ""


async def _build_workspace_preload_layer(
    project: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """F6: Workspace Preloading — 프로젝트 컨텍스트 자동 주입 (Layer 2.5)."""
    try:
        from app.services.workspace_preloader import build_workspace_preload
        block = await build_workspace_preload(project=project, session_id=session_id)
        return f"\n{block}" if block else ""
    except Exception as e:
        logger.debug(f"[PreLoad] context_builder Workspace Preload 주입 실패: {e}")
        return ""


async def _build_artifact_context_layer(
    session_id: Optional[str] = None,
    db_conn=None,
) -> str:
    """워크스페이스 최근 아티팩트 제목 목록을 AI에게 주입 (최대 20건)."""
    if not session_id:
        return ""
    try:
        import uuid as _uuid
        from app.core.db_pool import get_pool
        _conn = db_conn or await get_pool().acquire()
        _should_release = db_conn is None
        try:
            _ws_id = await _conn.fetchval(
                "SELECT workspace_id FROM chat_sessions WHERE id = $1",
                _uuid.UUID(session_id),
            )
            if not _ws_id:
                return ""
            _rows = await _conn.fetch(
                """SELECT type, title, created_at::text
                   FROM chat_artifacts
                   WHERE workspace_id = $1
                   ORDER BY created_at DESC LIMIT 20""",
                _ws_id,
            )
            if not _rows:
                return ""
            _lines = [f"- [{r['type']}] {r['title']} ({r['created_at'][:16]})" for r in _rows]
            return (
                "\n<recent_artifacts>\n## 이 워크스페이스의 최근 아티팩트\n"
                + "\n".join(_lines)
                + "\n</recent_artifacts>\n"
            )
        finally:
            if _should_release and not db_conn:
                await get_pool().release(_conn)
    except Exception as e:
        logger.debug(f"[ArtifactCtx] 아티팩트 컨텍스트 주입 실패: {e}")
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

# 메시지당 본문 상한과, 상한을 적용하지 않는 최근 구간.
# 최근 구간을 너무 좁히면 지시대명사("그거", "아까 그 방식")가 가리키는 맥락이
# 잘린다. 너무 넓히면 상한이 무의미해진다.
_MSG_CHAR_CAP = int(os.getenv("HISTORY_MSG_CHAR_CAP", "4000"))
_VERBATIM_RECENT = int(os.getenv("HISTORY_VERBATIM_RECENT", "6"))

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

            # 패턴 3: Deep Compression — 2×윈도우 이전 메시지 초강력 압축
            # 매우 오래된 대화는 핵심만 보존 (user: 첫 100자, assistant: 첫 200자)
            if i < len(messages) - _OBSERVATION_WINDOW * 2:
                if role == "user" and len(content) > 100:
                    content = content[:100] + "…[이전 질문 축소]"
                elif role == "assistant" and len(content) > 200:
                    content = content[:200] + "…[이전 응답 축소]"

        # 메시지당 상한 — 위치와 무관하게 한 건이 컨텍스트를 삼키지 못하게 한다.
        #
        # 기존 패턴 3(Deep Compression)은 2×윈도우(=40턴) **이전** 에만 걸린다.
        # 그래서 최근 40건은 아무리 길어도 원문이 들어간다. 2026-09-14 실측:
        # 세션 5090a247 의 컨텍스트 54건이 95,321 토큰이었고, 그중 한 메시지가
        # 58,906자(34%)였다. 설계 목표는 3,000~5,000 토큰인데 20배였다.
        #
        # 도구 결과가 아니라 **긴 산문 답변**이 원인이라 기존 마스킹은 걸리지
        # 않는다(본문 패턴 "[시스템 도구 조회 결과" 를 찾기 때문).
        #
        # 최근 _VERBATIM_RECENT 건은 손대지 않는다. "그거 다시 해줘" 같은
        # 지시대명사가 직전 맥락을 가리키므로 그 구간을 자르면 대화가 깨진다.
        if (
            i < len(messages) - _VERBATIM_RECENT
            and isinstance(content, str)
            and len(content) > _MSG_CHAR_CAP
        ):
            _dropped = len(content) - _MSG_CHAR_CAP
            content = content[:_MSG_CHAR_CAP] + f"\n…[{_dropped:,}자 생략 — 앞부분만 유지]"

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

# 직전 턴의 구간별 자수. provenance 기록부(prompt_compiler)가 읽어 간다.
# 조립과 기록이 다른 모듈이라 값을 넘길 경로가 없어 모듈 수준에 둔다 —
# 한 프로세스가 한 턴씩 처리하므로 섞이지 않는다.
_SECTION_CHARS_LAST: dict[str, int] = {}


async def build_messages_context(
    workspace_name: str,
    session_id: str,
    raw_messages: List[Dict[str, Any]],
    base_system_prompt: str = "",
    db_conn=None,
    document_context: str = "",
    intent: str = "",
    apply_prompt_assets: bool = True,
) -> tuple[List[Dict[str, Any]], str]:
    """
    3+D 계층 컨텍스트 구성 → (messages, system_prompt) 반환.
    system_prompt: Layer 1 + Layer 2 + (Layer D: 임시 문서 컨텍스트)
    messages: Layer 3 대화 히스토리
    intent: 인텐트명 (Prompt Compression — 단순 인텐트 시 경량 프롬프트)
    """
    ws_key = _normalize_workspace(workspace_name)

    # Prompt Compression: intent가 없으면 마지막 user 메시지로 간이 판별
    _effective_intent = intent
    if not _effective_intent and raw_messages:
        _last_msg = ""
        for _rm in reversed(raw_messages):
            if _rm.get("role") == "user":
                _last_msg = (_rm.get("content", "") or "")
                if isinstance(_last_msg, list):
                    _last_msg = " ".join(b.get("text", "") for b in _last_msg if isinstance(b, dict))
                break
        _SIMPLE_PATTERNS = ("안녕", "ㅎㅇ", "하이", "hi", "hello", "뭐해", "고마워", "감사", "ㅋㅋ", "ㄳ", "ㅇㅇ", "ok", "ㅎ", "네", "응")
        if _last_msg and len(_last_msg) < 20 and any(p in _last_msg.lower() for p in _SIMPLE_PATTERNS):
            _effective_intent = "greeting"

    # Layer 1 (동기 — system_prompt_v2 기반 XML 섹션, Prompt Compression 적용)
    layer1 = build_layer1(ws_key, base_system_prompt, intent=_effective_intent)
    # 5-Layer prompt asset 적용 (AADS-PROMPT-INFRA).
    # 일반 채팅 경로는 모델/역할 확정 후 chat_service에서 한 번만 compile한다.
    if apply_prompt_assets:
        try:
            # 세션의 담당을 찾아 넘긴다.
            #
            # 예전에는 `role=""` 이 박혀 있었다. 컴파일러는 `role_scope` 가
            # 지정된 자산을 그 키로 거르므로, 빈 문자열을 넘기면 **담당 역할
            # 프롬프트가 하나도 안 붙는다.** 2026-09-14 실측에서 #310 하네스의
            # 주도 세션을 이 경로로 조립했더니 `StrategyCardLead` 도
            # `ask_session` 도 시스템 프롬프트에 없었다 — 담당이 자기가 누구인지
            # 모르는 채로 이어쓰기를 한다.
            _role_key = ""
            try:
                if db_conn is not None:
                    _role_key = await db_conn.fetchval(
                        "SELECT role_key FROM chat_sessions WHERE id = $1::uuid",
                        session_id,
                    ) or ""
                else:
                    from app.core.db_pool import get_pool as _gp

                    _role_key = await _gp().fetchval(
                        "SELECT role_key FROM chat_sessions WHERE id = $1::uuid",
                        session_id,
                    ) or ""
            except Exception as _role_exc:
                logger.debug("role_key_lookup_failed", error=str(_role_exc))

            compiler = PromptCompiler()
            compiled = await compiler.compile(
                # 표시명이 아니라 **정규화된 프로젝트 키**를 넘긴다.
                #
                # 자산의 `workspace_scope` 는 `{GO100}` 인데 세션의 워크스페이스
                # 이름은 `[GO100] 백억이` 다. 컴파일러는 정규화하지 않고 받은
                # 문자열을 그대로 비교하므로, 표시명을 넘기면 **그 프로젝트의
                # 자산이 하나도 안 걸린다.** 2026-09-14 실측:
                #   ws='[GO100] 백억이' → 12,656자, 팀 명단 없음
                #   ws='GO100'         → 15,459자, 팀 명단 있음
                # `ws_key` 는 이 함수 맨 위에서 이미 이걸 계산해 뒀다.
                workspace_name=ws_key,
                intent=intent,
                model="",
                session_id=session_id,
                role=_role_key,
                base_system_prompt=layer1,
            )
            layer1 = compiled.system_prompt
        except Exception as exc:
            logger.warning(f"PromptCompiler fallback: {exc}")

    # Phase 3: LAYER4 독립 빌드 — Layer1에서 분리하여 Prompt Cache 적중률 극대화
    if db_conn:
        try:
            from app.core.memory_recall import get_evolution_stats
            _evo = await get_evolution_stats(db_conn)
            _layer4 = _build_layer4(_evo)
        except Exception:
            _layer4 = _build_layer4()
    else:
        _layer4 = _build_layer4()

    # Layer 2 + 메모리 주입 + Auto-RAG(F1/F3) + Workspace Preload(F6) 병렬 실행
    _project = _normalize_workspace(workspace_name)
    # 마지막 user 메시지 추출 (Auto-RAG용)
    _last_user_msg = ""
    _current_message_ids: set[str] = {
        str(_m.get("id") or _m.get("message_id"))
        for _m in raw_messages
        if _m.get("id") or _m.get("message_id")
    }
    for _m in reversed(raw_messages):
        if _m.get("role") == "user":
            _last_user_msg = _m.get("content", "")
            if isinstance(_last_user_msg, list):
                _last_user_msg = " ".join(b.get("text", "") for b in _last_user_msg if isinstance(b, dict) and b.get("type") == "text")
            break

    # Layer 2와 메모리는 TTL 캐시 적용 (60초, TTFT 단축)
    _l2_cache_key = f"l2:{ws_key}:{session_id}"
    _mem_cache_key = f"mem:{session_id}:{_project}"
    layer2, memory_layer, auto_rag_layer, preload_layer, artifact_layer = await asyncio.gather(
        _get_cached_or_build(_l2_cache_key, _build_layer2_dynamic(workspace_name, db_conn=db_conn, session_id=session_id)),
        _get_cached_or_build(_mem_cache_key, _build_memory_layer(session_id=session_id, project_id=_project)),
        _build_auto_rag_layer_bounded(_last_user_msg, session_id, _project, _current_message_ids),
        _build_workspace_preload_layer(_project, session_id),
        _build_artifact_context_layer(session_id, db_conn=db_conn),
    )

    # KST 현재시각은 **꼬리**에 붙인다 (매 턴 동적).
    #
    # 최상단에 두면 분이 바뀔 때마다 프롬프트가 0번째 글자부터 달라진다.
    # Anthropic prompt caching 은 프리픽스 일치로 판정하므로, 첫 글자가 바뀌면
    # 그 뒤 시스템 프롬프트 전체(실측 34,000~40,000자)가 매 턴 캐시 미스가 된다.
    # 모델이 시각을 읽는 데에는 위치가 상관없으므로 맥락 손실 없이 적중만 올린다.
    _kst_now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST (%A)")
    system_prompt = layer1 + "\n\n" + layer2 + memory_layer + preload_layer + auto_rag_layer + artifact_layer + "\n\n" + _layer4 + f"\n\n<currentTime>\n{_kst_now}\n</currentTime>"

    # 구간별 계측.
    #
    # 총량(system_prompt_chars)만 남기면 무엇을 줄여야 할지 알 수 없다.
    # 2026-09-16, "도구 142개가 25,000토큰" 이라고 글자 수로 추정해 우선순위를
    # 거꾸로 잡았다. 실측하니 도구는 1,768토큰이고 시스템 프롬프트가 26,677토큰
    # 이었다 — 14배 틀렸다. 구간별로 남겨야 다음에 같은 실수를 안 한다.
    #
    # 토큰 환산은 실측 계수를 쓴다. 한국어 혼합 프롬프트에서 36,546자가
    # 26,677토큰이었으므로 약 1.37자/토큰이다. 기존 1.5 는 과소 추정이었다.
    _sections = {
        "layer1": len(layer1),
        "layer2": len(layer2),
        "memory": len(memory_layer),
        "preload": len(preload_layer),
        "auto_rag": len(auto_rag_layer),
        "artifact": len(artifact_layer),
        "layer4": len(_layer4),
    }
    _sp_chars = len(system_prompt)
    logger.info(
        "system_prompt_sections chars=%d est_tokens=%d %s",
        _sp_chars,
        int(_sp_chars / 1.37),
        " ".join(f"{k}={v}" for k, v in sorted(_sections.items(), key=lambda x: -x[1])),
    )
    try:
        _SECTION_CHARS_LAST.clear()
        _SECTION_CHARS_LAST.update(_sections)
    except Exception:
        pass

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
    # ★ Layer D(ephemeral document)는 컴팩션 판정에서 제외 — 다음 턴 자동 소멸하므로
    _prompt_for_compaction = system_prompt.split("<ephemeral_document_context>")[0].rstrip() if "<ephemeral_document_context>" in system_prompt else system_prompt
    try:
        from app.services.context_compressor import estimate_tokens, needs_structured_summary
        _est = estimate_tokens(messages, _prompt_for_compaction)
        _COMPACTION_THRESHOLD = int(os.getenv("COMPACTION_TRIGGER_TOKENS", "80000"))
        if needs_structured_summary(messages, _prompt_for_compaction, threshold=_COMPACTION_THRESHOLD):
            logger.warning(f"context_builder: tokens={_est} > 80K, triggering structured summary")
            from app.services.compaction_service import check_and_compact
            messages = await check_and_compact(session_id, messages, db_conn=db_conn)
    except Exception as e:
        logger.warning(f"context_builder compaction error: {e}")
        # Emergency Truncation: compaction 실패 시 LLM 없이 최근 30개만 유지
        _EMERGENCY_KEEP = 30
        if len(messages) > _EMERGENCY_KEEP:
            messages = [{"role": "system", "content": f"[이전 {len(messages) - _EMERGENCY_KEEP}개 메시지 자동 생략]"}] + messages[-_EMERGENCY_KEEP:]

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
    last_user_message: str = "",
    intent: str = "",
) -> ContextResult:
    """
    AADS-185-A1: 비동기 3계층 컨텍스트 빌드 → ContextResult 반환.
    system_blocks: Anthropic Tool Use API용 (cache_control 포함)
    system_text: LiteLLM/Gemini용 플랫 문자열
    intent: 인텐트명 (Prompt Compression — 단순 인텐트 시 경량 프롬프트)
    """
    ws_key = _normalize_workspace(workspace_name)

    # Layer 1 (정적 — 캐싱 대상, system_prompt_v2 기반 XML 섹션 + 도구 안내)
    layer1_base = build_layer1(ws_key, base_system_prompt, intent=intent)
    tool_guide = _build_tool_guide_layer()  # AADS-186D: 도구 카테고리 안내
    layer1 = layer1_base + tool_guide

    # Phase 3: LAYER4 독립 빌드 — Layer1에서 분리
    if db_conn:
        try:
            from app.core.memory_recall import get_evolution_stats
            _evo = await get_evolution_stats(db_conn)
            _layer4 = _build_layer4(_evo)
        except Exception:
            _layer4 = _build_layer4()
    else:
        _layer4 = _build_layer4()

    # Layer 2 (동적) + CKP + 메모리 + Workspace Preload(F6) + Auto-RAG(F1/F3) — 병렬 실행
    # Layer 2와 메모리는 TTL 캐시 적용 (60초, TTFT 단축)
    _project = _normalize_workspace(workspace_name)
    _l2_cache_key = f"l2:{ws_key}:{session_id}"
    _mem_cache_key = f"mem:{session_id}:{_project}"
    layer2, ckp_layer, memory_layer, preload_layer, auto_rag_layer = await asyncio.gather(
        _get_cached_or_build(_l2_cache_key, _build_layer2_dynamic(workspace_name, db_conn=db_conn, session_id=session_id)),
        _build_ckp_layer(workspace_name),
        _get_cached_or_build(_mem_cache_key, _build_memory_layer(session_id=session_id, project_id=_project)),
        _build_workspace_preload_layer(_project, session_id),
        _build_auto_rag_layer_bounded(last_user_message, session_id, _project),
    )
    layer2_full = layer2 + ckp_layer + memory_layer + preload_layer + auto_rag_layer

    # AADS-186D: Prompt Caching 최적화 적용 (3 breakpoints: Layer1 + Layer2+CKP + Memory)
    _ckp_and_extras = ckp_layer + preload_layer + auto_rag_layer
    _memory_block = memory_layer
    try:
        from app.core.cache_config import build_cached_system_blocks
        system_blocks = build_cached_system_blocks(
            layer1, layer2, _ckp_and_extras, memory_text=_memory_block,
        )
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

    # KST 현재시각은 **꼬리**에 붙인다 (매 턴 동적).
    #
    # 예전에는 비캐시 블록으로 만들어 system_blocks 맨 앞에 끼웠다. 그러면
    # 바로 위에서 만든 3개 breakpoint(AADS-186D)가 **하나도 적중하지 않는다** —
    # 캐시는 프리픽스 일치로 판정하는데 0번 블록이 분마다 바뀌기 때문이다.
    # 꼬리로 옮기면 Layer1/Layer2/Memory 프리픽스가 그대로 유지되고, 모델이
    # 읽는 내용은 동일하므로 맥락 유지에는 영향이 없다.
    _kst_now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST (%A)")
    _kst_block = f"<currentTime>\n{_kst_now}\n</currentTime>"
    system_text = layer1 + "\n\n---\n\n" + layer2_full + "\n\n" + _layer4 + "\n\n" + _kst_block
    # system_blocks 꼬리에 KST 시각 주입 (비캐시 블록 — 캐시 프리픽스를 깨지 않는 위치)
    system_blocks = system_blocks + [{"type": "text", "text": _kst_block}]

    # 토큰 절감 측정 로깅
    _sp_chars = len(system_text)
    logger.info("system_prompt_tokens chars=%d est_tokens=%d", _sp_chars, int(_sp_chars / 1.5))

    return ContextResult(
        system_blocks=system_blocks,
        system_text=system_text,
        workspace_name=ws_key,
        workspace_id=workspace_id,
        layer2_text=layer2_full,
    )
