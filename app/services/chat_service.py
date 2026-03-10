"""
AADS-170: CEO Chat-First 시스템 — 채팅 서비스 레이어
DB CRUD, 메시지 전송(SSE 스트리밍), 파일 업로드/다운로드 비즈니스 로직.
AADS-188C: Claude Agent SDK 통합 (execute/code_modify 인텐트 → SDK primary, bridge fallback).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import asyncpg
from anthropic import AsyncAnthropic, APIStatusError
from app.config import Settings

logger = logging.getLogger(__name__)


# ── SSE heartbeat wrapper ─────────────────────────────────────────
import asyncio as _heartbeat_asyncio


async def with_heartbeat(
    gen: AsyncGenerator[str, None],
    interval: float = 8.0,
) -> AsyncGenerator[str, None]:
    """Wrap an SSE async generator to interleave heartbeat events.

    If the inner generator hasn't yielded anything for *interval* seconds,
    a lightweight ``{"type": "heartbeat"}`` SSE line is emitted so that
    Cloudflare(100s)/Nginx/frontend can keep the connection alive.

    interval=8s → Cloudflare 100s 유휴 타임아웃 대비 충분한 여유.
    """
    HEARTBEAT = f'data: {json.dumps({"type": "heartbeat"})}\n\n'
    ait = gen.__aiter__()
    pending: _heartbeat_asyncio.Task | None = None
    while True:
        if pending is None:
            pending = _heartbeat_asyncio.ensure_future(ait.__anext__())
        try:
            chunk = await _heartbeat_asyncio.wait_for(
                _heartbeat_asyncio.shield(pending), timeout=interval,
            )
            pending = None  # consumed — get next on next iteration
            yield chunk
        except _heartbeat_asyncio.TimeoutError:
            yield HEARTBEAT  # pending is still running, will retry
        except StopAsyncIteration:
            break
        except Exception as exc:
            logger.warning(f"with_heartbeat inner generator error: {type(exc).__name__}: {exc}")
            # 에러도 SSE로 전달 후 종료 (조용히 삼키지 않음)
            yield f'data: {json.dumps({"type": "error", "content": f"Stream error: {type(exc).__name__}"})}\n\n'
            break

# ── Background completion wrapper ─────────────────────────────────
# 클라이언트 SSE 연결이 끊겨도 LLM 생성을 백그라운드에서 완료하여 DB에 저장.
# _active_bg_tasks: session_id → asyncio.Task (동시 중복 방지)
_active_bg_tasks: Dict[str, _heartbeat_asyncio.Task] = {}


async def _drain_generator_to_db(gen: AsyncGenerator[str, None], session_id: str) -> None:
    """SSE generator를 끝까지 소비하여 DB 저장을 보장 (yield 결과는 버림)."""
    try:
        async for _ in gen:
            pass  # generator 내부에서 DB 저장이 일어남
    except Exception as e:
        logger.warning(f"bg_drain_error session={session_id}: {e}")
    finally:
        _active_bg_tasks.pop(session_id, None)
        logger.info(f"bg_completion_done session={session_id}")


async def with_background_completion(
    gen: AsyncGenerator[str, None],
    session_id: str,
) -> AsyncGenerator[str, None]:
    """SSE generator를 감싸서, 클라이언트 연결 종료 시 백그라운드로 이어받는 래퍼.

    동작 방식:
    1. 정상: yield로 클라이언트에 SSE 전달 (generator 소비)
    2. 클라이언트 disconnect → GeneratorExit 발생
    3. 남은 generator를 asyncio.Task로 백그라운드 실행 → DB 저장 보장
    """
    inner_gen = gen.__aiter__()
    exhausted = False
    try:
        async for chunk in inner_gen:
            yield chunk
        exhausted = True
    except (GeneratorExit, _heartbeat_asyncio.CancelledError):
        # 클라이언트가 연결을 끊음 → 백그라운드로 이어받기
        logger.info(f"client_disconnected session={session_id} — continuing in background")

        async def _continue():
            try:
                async for _ in inner_gen:
                    pass
            except Exception as e:
                logger.warning(f"bg_continue_error session={session_id}: {e}")
            finally:
                _active_bg_tasks.pop(session_id, None)
                logger.info(f"bg_completion_done session={session_id}")

        if session_id not in _active_bg_tasks:
            task = _heartbeat_asyncio.create_task(_continue())
            _active_bg_tasks[session_id] = task
    except Exception as e:
        logger.error(f"with_background_completion error: {e}")
        raise


def get_active_bg_tasks() -> Dict[str, bool]:
    """현재 백그라운드 진행 중인 세션 목록 (health check / 디버그용)."""
    return {sid: not task.done() for sid, task in _active_bg_tasks.items()}


# AADS-186C: Langfuse 트레이스 (optional — graceful degradation)
try:
    from app.core.langfuse_config import create_trace, is_enabled as langfuse_is_enabled
    _LANGFUSE_AVAILABLE = True
except ImportError:
    _LANGFUSE_AVAILABLE = False
    def create_trace(*args, **kwargs): return None  # type: ignore[misc]
    def langfuse_is_enabled() -> bool: return False  # type: ignore[misc]
settings = Settings()

# ─── DB 연결 ──────────────────────────────────────────────────────────────────

def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql://", "postgres://") if url else url


async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_db_url(), timeout=10)


# ─── Anthropic 클라이언트 ──────────────────────────────────────────────────────

_anthropic = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())


# ─── Workspace CRUD ───────────────────────────────────────────────────────────

async def list_workspaces() -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_workspaces ORDER BY created_at"
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def create_workspace(data: Dict[str, Any]) -> Dict[str, Any]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_workspaces (name, system_prompt, files, settings, color, icon)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
            RETURNING *
            """,
            data["name"],
            data.get("system_prompt"),
            json.dumps(data.get("files", [])),
            json.dumps(data.get("settings", {})),
            data.get("color", "#6366F1"),
            data.get("icon", "💬"),
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def update_workspace(workspace_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("name", "system_prompt", "color", "icon"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        for jfield in ("files", "settings"):
            if jfield in data and data[jfield] is not None:
                sets.append(f"{jfield} = ${idx}::jsonb")
                vals.append(json.dumps(data[jfield]))
                idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_workspaces WHERE id = $1", uuid.UUID(workspace_id))
            return _row_to_dict(row) if row else None
        sets.append(f"updated_at = NOW()")
        vals.append(uuid.UUID(workspace_id))
        row = await conn.fetchrow(
            f"UPDATE chat_workspaces SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def delete_workspace(workspace_id: str) -> bool:
    conn = await _get_conn()
    try:
        result = await conn.execute(
            "DELETE FROM chat_workspaces WHERE id = $1", uuid.UUID(workspace_id)
        )
        return result == "DELETE 1"
    finally:
        await conn.close()


# ─── Session CRUD ─────────────────────────────────────────────────────────────

async def list_sessions(workspace_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_sessions WHERE workspace_id = $1 ORDER BY pinned DESC, updated_at DESC LIMIT $2",
            uuid.UUID(workspace_id),
            limit,
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def create_session(data: Dict[str, Any]) -> Dict[str, Any]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions (workspace_id, title)
            VALUES ($1, $2)
            RETURNING *
            """,
            uuid.UUID(str(data["workspace_id"])),
            data.get("title"),
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def update_session(session_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("title", "summary"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        if "pinned" in data and data["pinned"] is not None:
            sets.append(f"pinned = ${idx}")
            vals.append(data["pinned"])
            idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_sessions WHERE id = $1", uuid.UUID(session_id))
            return _row_to_dict(row) if row else None
        sets.append("updated_at = NOW()")
        vals.append(uuid.UUID(session_id))
        row = await conn.fetchrow(
            f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def delete_session(session_id: str) -> bool:
    conn = await _get_conn()
    try:
        result = await conn.execute(
            "DELETE FROM chat_sessions WHERE id = $1", uuid.UUID(session_id)
        )
        return result == "DELETE 1"
    finally:
        await conn.close()


# ─── Message ──────────────────────────────────────────────────────────────────

async def list_messages(session_id: str, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_messages WHERE session_id = $1 ORDER BY created_at LIMIT $2 OFFSET $3",
            uuid.UUID(session_id),
            limit,
            offset,
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def _save_message(
    conn: asyncpg.Connection,
    session_id: uuid.UUID,
    role: str,
    content: str,
    model_used: Optional[str] = None,
    intent: Optional[str] = None,
    cost: Decimal = Decimal("0"),
    tokens_in: int = 0,
    tokens_out: int = 0,
    attachments: Optional[List[Any]] = None,
    sources: Optional[List[Any]] = None,
    tools_called: Optional[List[str]] = None,
    thinking_summary: Optional[str] = None,
) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO chat_messages
            (session_id, role, content, model_used, intent, cost, tokens_in, tokens_out,
             attachments, sources, tools_called, thinking_summary)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb, $12)
        RETURNING *
        """,
        session_id,
        role,
        content,
        model_used,
        intent,
        cost,
        tokens_in,
        tokens_out,
        json.dumps(attachments or []),
        json.dumps(sources or []),
        json.dumps(tools_called or []),
        thinking_summary,
    )
    # Update session message count
    await conn.execute(
        "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1",
        session_id,
    )
    return _row_to_dict(row)


async def send_message_stream(
    session_id: str,
    content: str,
    attachments: Optional[List[Any]] = None,
    model_override: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    AADS-185: 3계층 Context Engineering + IntentRouter + ModelSelector + Tool Use 루프.
    SSE 청크: data: {"type": "delta"|"thinking"|"tool_use"|"tool_result"|"done"|"error", ...}
    """
    conn = await _get_conn()
    # AADS-186C: Langfuse 트레이스 시작
    _lf_trace = create_trace(
        name="chat_turn",
        session_id=session_id,
        user_id="CEO",
        input_data={"content": content[:500], "model_override": model_override},
    )
    _lf_span_intent = None
    _lf_span_llm = None
    _trace_start_time = __import__("time").monotonic()

    try:
        sid = uuid.UUID(session_id)

        # 1. 첨부파일 내용 추출 → content에 추가
        if attachments:
            file_texts = []
            for att in attachments:
                file_path = att.get("path", "") if isinstance(att, dict) else ""
                file_name = att.get("name", "") if isinstance(att, dict) else str(att)
                if file_path and os.path.isfile(file_path):
                    try:
                        ext = os.path.splitext(file_path)[1].lower()
                        if ext in (".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css", ".yaml", ".yml", ".toml", ".sh", ".sql", ".log", ".xml"):
                            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                                file_content = f.read(100000)  # 100KB 제한
                            file_texts.append(f"\n\n--- 첨부파일: {file_name} ---\n{file_content}")
                        else:
                            file_texts.append(f"\n\n[첨부파일: {file_name} ({ext} 파일)]")
                    except Exception as e:
                        logger.warning(f"attachment_read_error: {file_name}: {e}")
                        file_texts.append(f"\n\n[첨부파일: {file_name} (읽기 실패)]")
                elif file_name:
                    file_texts.append(f"\n\n[첨부파일: {file_name}]")
            if file_texts:
                content = content + "".join(file_texts)

        # 사용자 메시지 저장
        await _save_message(conn, sid, "user", content, attachments=attachments or [])

        # 2. 워크스페이스 정보 조회
        sp_row = await conn.fetchrow(
            """
            SELECT w.id::text AS workspace_id, w.system_prompt, w.name AS workspace_name
            FROM chat_workspaces w
            JOIN chat_sessions s ON s.workspace_id = w.id
            WHERE s.id = $1
            """,
            sid,
        )
        base_prompt = (sp_row["system_prompt"] if sp_row and sp_row["system_prompt"] else "")
        workspace_name = (sp_row["workspace_name"] if sp_row and sp_row["workspace_name"] else "CEO")

        # 3. 세션 히스토리 조회 (무한 대화 — 전체 비압축 메시지, observation masking은 context_builder에서)
        hist_rows = await conn.fetch(
            """
            SELECT role, content FROM chat_messages
            WHERE session_id = $1 AND (is_compacted IS NULL OR is_compacted = false)
            ORDER BY created_at DESC LIMIT 200
            """,
            sid,
        )
        raw_messages = [{"role": r["role"], "content": r["content"]} for r in reversed(hist_rows)]

        # 세션 누적 비용 조회 (프론트엔드 표시용)
        _session_cost_row = await conn.fetchrow(
            "SELECT cost_total, message_count FROM chat_sessions WHERE id = $1", sid
        )
        _session_cost = float(_session_cost_row["cost_total"] or 0) if _session_cost_row else 0
        _session_turns = int(_session_cost_row["message_count"] or 0) if _session_cost_row else 0

        # 4. 3계층 컨텍스트 빌드
        from app.services.context_builder import build_messages_context
        messages, system_prompt = await build_messages_context(
            workspace_name=workspace_name,
            session_id=session_id,
            raw_messages=raw_messages,
            base_system_prompt=base_prompt,
            db_conn=conn,
        )

        # 4.5. AADS-188E: 시맨틱 코드 검색 컨텍스트 주입 (code_search 관련 키워드 감지)
        _CODE_SEARCH_KEYWORDS = (
            "코드", "함수", "클래스", "어디", "어디야", "파일", "소스", "구현",
            "처리", "로직", "어디서", "찾아", "검색", "code", "where", "function",
        )
        if any(kw in content for kw in _CODE_SEARCH_KEYWORDS) and len(content) < 200:
            try:
                from app.services.semantic_code_search import SemanticCodeSearch
                _scs = SemanticCodeSearch()
                if _scs._is_available():
                    _search_results = await _scs.search(content, top_k=3)
                    if _search_results and not any("error" in r for r in _search_results):
                        _ctx_lines = ["<codebase_knowledge_inline>"]
                        for _r in _search_results[:3]:
                            _ctx_lines.append(
                                f"  {_r.get('file','?')}:{_r.get('start_line','?')} "
                                f"[{_r.get('type','?')}] {_r.get('name','?')} "
                                f"(유사도: {_r.get('similarity_score', 0):.2f})"
                            )
                            if _r.get("code_snippet"):
                                _ctx_lines.append(f"    {_r['code_snippet'][:150]}")
                        _ctx_lines.append("</codebase_knowledge_inline>")
                        _inline_ctx = "\n".join(_ctx_lines)
                        # 시스템 프롬프트 마지막에 삽입
                        system_prompt = system_prompt + "\n\n" + _inline_ctx
                        logger.debug(f"[188E] 시맨틱 코드 검색 컨텍스트 주입: {len(_search_results)}개 청크")
            except Exception as _sce:
                logger.debug(f"[188E] 시맨틱 코드 검색 컨텍스트 주입 실패 (무시): {_sce}")

        # 5. 자동 압축 (20턴 초과 시)
        from app.services.compaction_service import check_and_compact
        messages = await check_and_compact(session_id, messages, db_conn=conn)

        # 6. 인텐트 분류 + 모델/도구 결정
        from app.services.intent_router import classify, get_model_for_override
        intent_result = await classify(content, workspace_name, recent_messages=messages)
        intent = intent_result.intent
        # Langfuse: intent_classification span
        if _lf_trace is not None:
            try:
                _lf_span_intent = _lf_trace.span(
                    name="intent_classification",
                    input={"content": content[:300], "workspace": workspace_name},
                    output={"intent": intent, "model": intent_result.model, "use_tools": intent_result.use_tools},
                    metadata={"use_gemini_direct": intent_result.use_gemini_direct},
                )
                if _lf_span_intent:
                    _lf_span_intent.end()
            except Exception:
                pass

        if model_override:
            intent_result.model = get_model_for_override(model_override)
            intent_result.use_gemini_direct = False

        # 7. Gemini Direct (Grounding / Deep Research)
        if intent_result.use_gemini_direct:
            if intent_result.gemini_mode == "grounding":
                from app.services.gemini_search_service import GeminiSearchService
                svc = GeminiSearchService()
                result = None
                try:
                    result = await svc.search_grounded(content)
                except Exception as e:
                    logger.warning(f"gemini_grounding_failed: {e}")
                if result is None:
                    # Fallback: Naver (타입별 or 통합) → Kakao → Brave
                    from app.services.naver_search_service import NaverSearchService
                    naver = NaverSearchService()
                    if naver.is_available():
                        try:
                            naver_type = getattr(intent_result, "naver_type", "")
                            if naver_type:
                                # 특화 검색 (뉴스/블로그/쇼핑/지역/책/이미지/백과/지식iN)
                                result = await naver.search(content, search_type=naver_type, count=5)
                            else:
                                # 일반 검색: 웹+블로그+뉴스+지식iN 통합
                                result = await naver.multi_search(content, count=3)
                            if result.error:
                                result = None
                        except Exception as e:
                            logger.warning(f"naver_search_failed: {e}")
                if result is None:
                    from app.services.kakao_search_service import KakaoSearchService
                    kakao = KakaoSearchService()
                    if kakao.is_available():
                        try:
                            result = await kakao.search(content)
                            if result.error:
                                result = None
                        except Exception as e:
                            logger.warning(f"kakao_search_failed: {e}")
                if result is None:
                    from app.services.brave_search_service import BraveSearchService
                    brave = BraveSearchService()
                    result = await brave.search(content)
                yield f"data: {json.dumps({'type': 'delta', 'content': result.text})}\n\n"
                if result.citations:
                    yield f"data: {json.dumps({'type': 'sources', 'sources': result.citations})}\n\n"
                await _save_message(conn, sid, "assistant", result.text,
                    model_used="gemini-flash", intent=intent, cost=Decimal("0"), sources=result.citations)
                await conn.execute(
                    "UPDATE chat_sessions SET updated_at = NOW() WHERE id = $1", sid)
                yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': 'gemini-flash', 'cost': '0'})}\n\n"
                return

            elif intent_result.gemini_mode == "deep_research":
                from app.services.deep_research_service import DeepResearchService
                dr_svc = DeepResearchService()
                if not dr_svc.is_available():
                    # API 키 없으면 Claude 폴백
                    intent_result.model = "claude-sonnet"
                    intent_result.use_gemini_direct = False
                else:
                    try:
                        # research_start SSE 발송
                        yield f"data: {json.dumps({'type': 'research_start', 'message': '딥리서치를 시작합니다... (3~10분 소요, 수십 개 소스 탐색)'})}\n\n"

                        collected_report_parts: list[str] = []
                        final_citations: list[dict] = []
                        final_interaction_id = ""
                        cost_usd = 3.0

                        # AADS-188A: research_stream() 사용 — planning/searching/analyzing 실시간 SSE
                        async for ev in await dr_svc.research_stream(content, timeout=600):
                            ev_type = ev.type
                            if ev_type in ("planning", "searching", "analyzing"):
                                yield f"data: {json.dumps({'type': 'research_progress', 'phase': ev_type, 'content': ev.content or '', 'progress_pct': ev.progress_pct or 0})}\n\n"
                            elif ev_type == "thinking" and ev.content:
                                yield f"data: {json.dumps({'type': 'thinking', 'thinking': (ev.content or '')[:2000]})}\n\n"
                            elif ev_type == "content" and ev.content:
                                collected_report_parts.append(ev.content)
                                yield f"data: {json.dumps({'type': 'delta', 'content': ev.content})}\n\n"
                            elif ev_type == "complete":
                                if ev.content and not collected_report_parts:
                                    # 청크 없이 완료된 경우 — 보고서를 delta로 분할 전송
                                    chunk_size = 500
                                    report_text = ev.content
                                    collected_report_parts.append(report_text)
                                    for i in range(0, len(report_text), chunk_size):
                                        yield f"data: {json.dumps({'type': 'delta', 'content': report_text[i:i+chunk_size]})}\n\n"
                                if ev.sources:
                                    final_citations = ev.sources
                                if ev.interaction_id:
                                    final_interaction_id = ev.interaction_id
                            elif ev_type == "error":
                                # error 이벤트: Claude 폴백으로 이동
                                raise Exception(ev.content or "deep_research error")

                        report_text = "".join(collected_report_parts)

                        if final_citations:
                            yield f"data: {json.dumps({'type': 'sources', 'sources': final_citations})}\n\n"

                        yield f"data: {json.dumps({'type': 'research_complete', 'interaction_id': final_interaction_id, 'cost': str(cost_usd)})}\n\n"

                        await _save_message(conn, sid, "assistant", report_text,
                            model_used="gemini-deep-research", intent=intent,
                            cost=Decimal(str(cost_usd)), sources=final_citations)
                        await conn.execute(
                            "UPDATE chat_sessions SET updated_at = NOW() WHERE id = $1", sid)
                        yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': 'gemini-deep-research', 'cost': str(cost_usd)})}\n\n"
                        return
                    except Exception as e:
                        logger.warning(f"gemini_deep_research_failed: {e}")
                        intent_result.model = "claude-sonnet"
                        intent_result.use_gemini_direct = False

        # 8. 도구 목록 (Anthropic Tool Use 포맷)
        tools_for_api = None
        if intent_result.use_tools:
            from app.services.tool_registry import ToolRegistry
            tools_for_api = ToolRegistry().get_tools(intent_result.tool_group)

        # 8.5a. AADS-188C: Agent SDK 실시간 자율 실행 (execute/code_modify 인텐트)
        # primary: Agent SDK, fallback: bridge(AutonomousExecutor) 경로
        _AGENT_SDK_INTENTS = frozenset({"execute", "code_modify"})
        if intent in _AGENT_SDK_INTENTS:
            # resume 지원: 세션 메타에서 sdk_session_id 조회
            sdk_session_id: Optional[str] = None
            try:
                meta_row = await conn.fetchrow(
                    "SELECT settings FROM chat_sessions WHERE id = $1", sid
                )
                if meta_row:
                    _settings = _row_to_dict(meta_row).get("settings") or {}
                    sdk_session_id = _settings.get("sdk_session_id")
            except Exception:
                pass

            from app.services.agent_sdk_service import get_agent_sdk_service, AGENT_SDK_ENABLED as _sdk_flag
            sdk_svc = get_agent_sdk_service()
            sdk_success = False

            if sdk_svc.is_available() and _sdk_flag:
                try:
                    full_response = ""
                    _captured_sdk_sid: Optional[str] = None
                    model_used = "claude-opus-4-6"
                    cost_usd = Decimal("0")
                    tools_called: list = []

                    async for sse_line in sdk_svc.execute_stream(
                        prompt=content,
                        session_id=sdk_session_id,
                    ):
                        yield sse_line
                        # 이벤트 파싱: session_id 캡처 + 텍스트 수집
                        try:
                            _ev = json.loads(sse_line.replace("data: ", "").strip())
                            _et = _ev.get("type", "")
                            if _et == "sdk_session":
                                _captured_sdk_sid = _ev.get("session_id")
                            elif _et == "delta":
                                full_response += _ev.get("content", "")
                            elif _et == "sdk_complete":
                                sdk_success = True
                        except Exception:
                            pass

                    # sdk_session_id를 세션 설정에 저장 (resume용)
                    if _captured_sdk_sid:
                        try:
                            _new_settings = {}
                            _s_row = await conn.fetchrow(
                                "SELECT settings FROM chat_sessions WHERE id = $1", sid
                            )
                            if _s_row:
                                _new_settings = _row_to_dict(_s_row).get("settings") or {}
                            _new_settings["sdk_session_id"] = _captured_sdk_sid
                            await conn.execute(
                                "UPDATE chat_sessions SET settings = $1::jsonb, updated_at = NOW() WHERE id = $2",
                                json.dumps(_new_settings), sid,
                            )
                        except Exception as _se:
                            logger.debug(f"sdk_session_id 저장 실패: {_se}")

                    if sdk_success:
                        await _save_message(
                            conn, sid, "assistant", full_response,
                            model_used=model_used, intent=intent,
                            cost=cost_usd, tokens_in=0, tokens_out=0,
                            sources=[], tools_called=tools_called,
                        )
                        await conn.execute(
                            "UPDATE chat_sessions SET cost_total = cost_total + $1, updated_at = NOW() WHERE id = $2",
                            cost_usd, sid,
                        )
                        yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': model_used, 'cost': str(cost_usd), 'agent_sdk': True})}\n\n"
                        return

                except Exception as _sdk_err:
                    logger.warning(f"agent_sdk_failed (fallback to bridge): {_sdk_err}")
                    # SDK 실패 → AutonomousExecutor fallback으로 계속 진행

        # 8.5. 복잡 인텐트 → AutonomousExecutor (max_iterations=25) (AADS-186E-3)
        _AUTONOMOUS_INTENTS = frozenset({
            "cto_code_analysis", "cto_verify", "service_inspection", "cto_impact",
        })
        if intent in _AUTONOMOUS_INTENTS and intent_result.use_tools and tools_for_api:
            from app.services.autonomous_executor import AutonomousExecutor
            auto_exec = AutonomousExecutor(max_iterations=25, cost_limit=2.0)
            full_response = ""
            thinking_summary = ""
            model_used = intent_result.model
            cost_usd = Decimal("0")
            input_tokens = 0
            output_tokens = 0
            tools_called: list = []

            async for sse_line in auto_exec.execute_task(
                task_description="",
                tools=tools_for_api,
                messages=messages,
                model=intent_result.model,
                system_prompt=system_prompt,
            ):
                yield sse_line
                # 완료/비용/오류 이벤트 파싱하여 응답 수집
                try:
                    import json as _json
                    _data = _json.loads(sse_line.replace("data: ", "").strip())
                    _etype = _data.get("type", "")
                    if _etype == "delta":
                        full_response += _data.get("content", "")
                    elif _etype in ("complete", "max_iterations", "cost_limit"):
                        cost_usd = Decimal(str(_data.get("total_cost", "0")))
                        if _etype == "complete":
                            full_response = _data.get("content", full_response)
                    elif _etype == "tool_use":
                        tools_called.append(_data.get("tool_name", ""))
                except Exception:
                    pass

            # 응답 저장 및 done 이벤트
            await _save_message(
                conn, sid, "assistant", full_response,
                model_used=model_used, intent=intent,
                cost=cost_usd, tokens_in=0, tokens_out=0,
                sources=[], tools_called=tools_called, thinking_summary=None,
            )
            await conn.execute(
                "UPDATE chat_sessions SET cost_total = cost_total + $1, updated_at = NOW() WHERE id = $2",
                cost_usd, sid,
            )
            yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': model_used, 'cost': str(cost_usd), 'input_tokens': 0, 'output_tokens': 0, 'autonomous': True})}\n\n"
            return

        # 9. 모델 선택기 → SSE 스트리밍
        from app.services.model_selector import call_stream
        # Langfuse: llm_generation span 시작
        if _lf_trace is not None:
            try:
                _lf_span_llm = _lf_trace.span(
                    name="llm_generation",
                    input={"model": intent_result.model, "intent": intent},
                )
            except Exception:
                pass
        full_response = ""
        thinking_summary = ""
        model_used = intent_result.model
        cost_usd = Decimal("0")
        input_tokens = 0
        output_tokens = 0
        tools_called: list = []

        async for event in call_stream(
            intent_result=intent_result,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools_for_api,
            model_override=model_override,
        ):
            etype = event.get("type", "")
            if etype == "heartbeat":
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            elif etype == "delta":
                full_response += event.get("content", "")
                yield f"data: {json.dumps({'type': 'delta', 'content': event['content']})}\n\n"
            elif etype == "thinking":
                thinking_summary += event.get("thinking", "")
                yield f"data: {json.dumps({'type': 'thinking', 'thinking': event['thinking']})}\n\n"
            elif etype == "tool_use":
                tools_called.append(event["tool_name"])
                yield f"data: {json.dumps({'type': 'tool_use', 'tool_name': event['tool_name'], 'tool_use_id': event['tool_use_id']})}\n\n"
            elif etype == "tool_result":
                yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': event['tool_name'], 'content': str(event.get('content', ''))[:5000]})}\n\n"
            elif etype == "yellow_limit":
                yield f"data: {json.dumps({'type': 'yellow_limit', 'content': event.get('content', ''), 'tool_name': event.get('tool_name', ''), 'consecutive_count': event.get('consecutive_count', 0)})}\n\n"
            elif etype == "done":
                model_used = event.get("model", intent_result.model)
                cost_usd = Decimal(str(event.get("cost", "0")))
                input_tokens = event.get("input_tokens", 0) or 0
                output_tokens = event.get("output_tokens", 0) or 0
                thinking_summary = event.get("thinking_summary") or thinking_summary
                tools_called = event.get("tools_called", tools_called)
            elif etype == "error":
                yield f"data: {json.dumps({'type': 'error', 'content': event.get('content', '오류')})}\n\n"
                return

        # 9.5 Layer ④: Output Validator — 빈 약속 응답 감지 및 재시도 (AADS-188C Phase 3)
        from app.services.output_validator import validate_response
        _validation = validate_response(
            response_text=full_response,
            tools_called=bool(tools_called),
            intent=intent,
        )
        if not _validation.is_valid:
            logger.warning(
                f"output_validator: {_validation.violation_type} — {_validation.message} "
                f"(intent={intent}, model={model_used}, tokens_out={output_tokens})"
            )
            # 재시도: output_validator가 생성한 retry_prompt 사용
            _retry_messages = list(messages)
            _retry_messages.append({"role": "assistant", "content": full_response.strip()})
            _retry_messages.append({"role": "user", "content": _validation.retry_prompt})

            _retry_response = ""
            async for event in call_stream(
                intent_result=intent_result,
                system_prompt=system_prompt,
                messages=_retry_messages,
                tools=tools_for_api,
                model_override=model_override,
            ):
                etype = event.get("type", "")
                if etype == "heartbeat":
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                elif etype == "delta":
                    _retry_response += event.get("content", "")
                    yield f"data: {json.dumps({'type': 'delta', 'content': event['content']})}\n\n"
                elif etype == "thinking":
                    yield f"data: {json.dumps({'type': 'thinking', 'thinking': event['thinking']})}\n\n"
                elif etype == "tool_use":
                    tools_called.append(event["tool_name"])
                    yield f"data: {json.dumps({'type': 'tool_use', 'tool_name': event['tool_name'], 'tool_use_id': event['tool_use_id']})}\n\n"
                elif etype == "tool_result":
                    yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': event['tool_name'], 'content': str(event.get('content', ''))[:5000]})}\n\n"
                elif etype == "done":
                    model_used = event.get("model", intent_result.model)
                    cost_usd += Decimal(str(event.get("cost", "0")))
                    input_tokens = event.get("input_tokens", 0) or 0
                    output_tokens = event.get("output_tokens", 0) or 0
                    tools_called = event.get("tools_called", tools_called)
                elif etype == "error":
                    yield f"data: {json.dumps({'type': 'error', 'content': event.get('content', '오류')})}\n\n"
                    return

            # 재시도 응답으로 교체
            if _retry_response.strip():
                full_response = full_response + "\n\n" + _retry_response

        # 10. 응답 저장
        await _save_message(
            conn, sid, "assistant", full_response,
            model_used=model_used,
            intent=intent,
            cost=cost_usd,
            tokens_in=input_tokens,
            tokens_out=output_tokens,
            sources=[],
            tools_called=tools_called,
            thinking_summary=thinking_summary or None,
        )
        await conn.execute(
            "UPDATE chat_sessions SET cost_total = cost_total + $1, updated_at = NOW() WHERE id = $2",
            cost_usd, sid,
        )

        # 11. 20턴 이상 시 세션 노트 자동 저장 + 자동 관찰 (AADS-186E-2/186E-3, 비동기)
        try:
            msg_count_row = await conn.fetchrow(
                "SELECT message_count FROM chat_sessions WHERE id = $1", sid
            )
            msg_count = (msg_count_row["message_count"] if msg_count_row else 0) or 0
            if msg_count >= 20 and msg_count % 20 == 0:
                import asyncio as _asyncio
                _asyncio.create_task(_auto_save_session_note(session_id, raw_messages))
                _asyncio.create_task(_auto_observe_session(raw_messages))
        except Exception:
            pass

        # 누적 비용 업데이트
        _session_cost += float(cost_usd)
        _session_turns += 2  # user + assistant

        yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'model': model_used, 'cost': str(cost_usd), 'input_tokens': input_tokens, 'output_tokens': output_tokens, 'thinking_summary': (thinking_summary[:2000] if thinking_summary else None), 'session_cost': f'${_session_cost:.2f}', 'session_turns': _session_turns})}\n\n"

    finally:
        await conn.close()


async def _auto_save_session_note(session_id: str, messages: List[Dict[str, Any]]) -> None:
    """20턴 컴팩션 시 자동 세션 노트 저장 (백그라운드 태스크)."""
    try:
        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        await mgr.save_session_note(session_id=session_id, messages=messages)
        logger.info(f"auto_save_session_note: session_id={session_id}")
    except Exception as e:
        logger.warning(f"auto_save_session_note error: {e}")


async def _auto_observe_session(messages: List[Dict[str, Any]]) -> None:
    """세션 종료 시 자동 패턴 관찰 (백그라운드 태스크, AADS-186E-3)."""
    try:
        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        await mgr.auto_observe_from_session(messages)
        logger.info("auto_observe_session: 완료")
    except Exception as e:
        logger.warning(f"auto_observe_session error: {e}")


async def toggle_bookmark(message_id: str) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "UPDATE chat_messages SET bookmarked = NOT bookmarked WHERE id = $1 RETURNING *",
            uuid.UUID(message_id),
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def search_messages(query: str, workspace_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        if workspace_id:
            rows = await conn.fetch(
                """
                SELECT m.* FROM chat_messages m
                JOIN chat_sessions s ON s.id = m.session_id
                WHERE s.workspace_id = $1
                  AND to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $2)
                ORDER BY m.created_at DESC LIMIT $3
                """,
                uuid.UUID(workspace_id),
                query,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM chat_messages
                WHERE to_tsvector('simple', content) @@ plainto_tsquery('simple', $1)
                ORDER BY created_at DESC LIMIT $2
                """,
                query,
                limit,
            )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


# ─── Artifact ────────────────────────────────────────────────────────────────

async def list_artifacts(session_id: str) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_artifacts WHERE session_id = $1 ORDER BY created_at",
            uuid.UUID(session_id),
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def get_artifact(artifact_id: str) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM chat_artifacts WHERE id = $1",
            uuid.UUID(artifact_id),
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def update_artifact(artifact_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        sets = []
        vals: List[Any] = []
        idx = 1
        for field in ("title", "content"):
            if field in data and data[field] is not None:
                sets.append(f"{field} = ${idx}")
                vals.append(data[field])
                idx += 1
        if "metadata" in data and data["metadata"] is not None:
            sets.append(f"metadata = ${idx}::jsonb")
            vals.append(json.dumps(data["metadata"]))
            idx += 1
        if not sets:
            row = await conn.fetchrow("SELECT * FROM chat_artifacts WHERE id = $1", uuid.UUID(artifact_id))
            return _row_to_dict(row) if row else None
        sets.append("updated_at = NOW()")
        vals.append(uuid.UUID(artifact_id))
        row = await conn.fetchrow(
            f"UPDATE chat_artifacts SET {', '.join(sets)} WHERE id = ${idx} RETURNING *",
            *vals,
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def export_artifact(artifact_id: str, fmt: str) -> Dict[str, Any]:
    """단순 텍스트 내보내기. PDF는 향후 확장."""
    artifact = await get_artifact(artifact_id)
    if not artifact:
        return {}
    content = artifact["content"]
    if fmt == "md":
        body = f"# {artifact.get('title', 'Artifact')}\n\n{content}"
        mime = "text/markdown"
    elif fmt == "html":
        body = f"<html><body><h1>{artifact.get('title', 'Artifact')}</h1><pre>{content}</pre></body></html>"
        mime = "text/html"
    else:
        # pdf: 텍스트로 반환 (실제 PDF 변환은 별도 라이브러리 필요)
        body = content
        mime = "application/pdf"
    return {"content": body, "mime": mime, "filename": f"artifact_{artifact_id}.{fmt}"}


# ─── Drive ───────────────────────────────────────────────────────────────────

UPLOAD_DIR = Path(os.getenv("CHAT_UPLOAD_DIR", "/root/aads/uploads/chat"))


async def list_drive_files(workspace_id: str) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM chat_drive_files WHERE workspace_id = $1 ORDER BY created_at DESC",
            uuid.UUID(workspace_id),
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


async def save_drive_file(
    workspace_id: str,
    filename: str,
    file_bytes: bytes,
    file_type: Optional[str],
    uploaded_by: str = "user",
) -> Dict[str, Any]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4()}_{filename}"
    file_path = UPLOAD_DIR / safe_name
    file_path.write_bytes(file_bytes)

    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_drive_files (workspace_id, filename, file_path, file_type, file_size, uploaded_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            uuid.UUID(workspace_id),
            filename,
            str(file_path),
            file_type,
            len(file_bytes),
            uploaded_by,
        )
        return _row_to_dict(row)
    finally:
        await conn.close()


async def delete_drive_file(file_id: str) -> bool:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "DELETE FROM chat_drive_files WHERE id = $1 RETURNING file_path",
            uuid.UUID(file_id),
        )
        if not row:
            return False
        path = Path(row["file_path"])
        if path.exists():
            path.unlink(missing_ok=True)
        return True
    finally:
        await conn.close()


async def get_drive_file(file_id: str) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM chat_drive_files WHERE id = $1",
            uuid.UUID(file_id),
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


# ─── Research Archive ────────────────────────────────────────────────────────

async def get_research_cache(topic: str, days: int = 7) -> Optional[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            SELECT * FROM research_archive
            WHERE topic ILIKE $1
              AND created_at >= NOW() - ($2 || ' days')::INTERVAL
            ORDER BY created_at DESC LIMIT 1
            """,
            f"%{topic}%",
            str(days),
        )
        return _row_to_dict(row) if row else None
    finally:
        await conn.close()


async def list_research_history(limit: int = 50) -> List[Dict[str, Any]]:
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM research_archive ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        await conn.close()


# ─── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _row_to_dict(row: asyncpg.Record) -> Dict[str, Any]:
    """asyncpg Record → Python dict. JSONB 문자열 파싱 포함."""
    if row is None:
        return {}
    result = {}
    for key in row.keys():
        val = row[key]
        # asyncpg는 JSONB를 문자열로 반환하는 경우가 있음 → 배열/객체 모두 파싱
        if isinstance(val, str) and len(val) >= 2 and val[0] in ("{", "["):
            try:
                val = json.loads(val)
            except Exception:
                pass
        result[key] = val
    return result
