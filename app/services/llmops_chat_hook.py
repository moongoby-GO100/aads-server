"""Chat -> LLMOps trace integration. Non-fatal: failures never affect chat responses."""
from __future__ import annotations
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def pair_tool_calls(tools_called: Optional[list]) -> Optional[list]:
    """tool_use 와 tool_result 를 tool_use_id 로 짝지어 한 행으로 만든다.

    이전에는 두 종류를 그대로 20건까지 잘라 넣어 latency_ms 가 항상
    비었고(24시간 2,849건 전부 NULL), 도구 지연과 모델 왕복을 가를 수 없었다.

    2026-09-17 추가 실측: 짝짓기를 넣은 뒤에도 NULL 이 남았는데, 남은 것은
    전부 재개(resume)된 턴이었다. 재개 턴은 부분 스냅샷을 이어받아 tool_use
    없이 tool_result 만 들고 오는 경우가 있고, 그 결과를 통째로 버리고 있었다
    — 그래서 재개 턴의 도구 호출은 집계에서 아예 빠졌다. 이제 지연은 못 재도
    호출 사실은 남기고, 왜 NULL 인지를 metadata 에 적는다.
    """
    if not tools_called:
        return None
    rows: list[dict] = []
    by_id: dict[str, dict] = {}
    for tc in tools_called:
        if not isinstance(tc, dict):
            rows.append({"tool_name": str(tc), "status": "success"})
            continue
        use_id = str(tc.get("tool_use_id") or "")
        if tc.get("type") == "tool_result":
            target = by_id.pop(use_id, None) if use_id else None
            if target is None:
                orphan = {
                    "tool_name": (tc.get("tool_name") or tc.get("name") or "unknown"),
                    "status": "error" if tc.get("is_error") else "success",
                    "called_at": tc.get("at"),
                    "metadata": {"latency_missing": "orphan_tool_result"},
                }
                if tc.get("is_error"):
                    orphan["error"] = str(tc.get("error_type") or tc.get("content") or "tool_error")[:500]
                rows.append(orphan)
                continue
            started = target.pop("_at", None)
            ended = tc.get("at")
            if started is not None and ended is not None:
                try:
                    target["latency_ms"] = max(0, int((float(ended) - float(started)) * 1000))
                except (TypeError, ValueError):
                    pass
            if tc.get("is_error"):
                target["status"] = "error"
                target["error"] = str(tc.get("error_type") or tc.get("content") or "tool_error")[:500]
            continue
        row = {
            "tool_name": (tc.get("name") or tc.get("tool_name") or "unknown"),
            "status": tc.get("status", "success"),
            # created_at 을 호출 시각으로 남긴다. 기록은 턴 종료 시
            # 일괄로 일어나므로 INSERT 시점은 호출 시각이 아니다.
            "called_at": tc.get("at"),
            "_at": tc.get("at"),
        }
        rows.append(row)
        if use_id:
            by_id[use_id] = row
    # 끝내 짝을 못 만난 tool_use — 결과가 오기 전에 턴이 끊긴 경우다.
    for row in by_id.values():
        row.setdefault("metadata", {})["latency_missing"] = "no_tool_result"
    for row in rows:
        row.pop("_at", None)
    return rows[:20]


async def record_chat_trace(
    *,
    session_id: str,
    execution_id: Optional[str] = None,
    project: Optional[str] = None,
    user_message: str = "",
    ai_response: str = "",
    model: Optional[str] = None,
    duration_sec: Optional[float] = None,
    cost: Optional[float] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    tools_called: Optional[list] = None,
    intent: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    try:
        from app.services.llmops_store import record_trace, find_promotion_candidates, promote_trace_to_dataset

        tool_list = pair_tool_calls(tools_called)
        has_response = bool(ai_response and ai_response.strip())
        status = "error" if error or not has_response else "success"
        sid = session_id if len(str(session_id)) >= 32 else None
        await record_trace(
            graph_run_id=f"chat:{execution_id or session_id}",
            project=project or "AADS",
            session_id=sid,
            source="chat",
            run_type="chat",
            status=status,
            model=model,
            input_summary=user_message,
            output_summary=ai_response,
            latency_ms=int(duration_sec * 1000) if duration_sec else None,
            cost_usd=float(cost) if cost else None,
            error=error,
            trace_key=f"chat:{execution_id}" if execution_id else None,
            tool_calls=tool_list,
            tags=[t for t in ["chat", project, intent] if t],
            metadata={
                "tokens_in": int(tokens_in or 0),
                "tokens_out": int(tokens_out or 0),
                "intent": intent,
            },
        )
        if status == "error":
            try:
                candidates = await find_promotion_candidates(project=project, hours=1, limit=3)
                for c in candidates:
                    await promote_trace_to_dataset(c["id"])
            except Exception:
                pass
    except Exception as exc:
        logger.debug("llmops_chat_trace_error: %s", str(exc)[:200])
