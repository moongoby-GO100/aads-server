"""Chat -> LLMOps trace integration. Non-fatal: failures never affect chat responses."""
from __future__ import annotations
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


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

        tool_list = None
        if tools_called:
            tool_list = [
                {
                    "tool_name": (
                        (tc.get("name") or tc.get("tool_name") or "unknown")
                        if isinstance(tc, dict) else str(tc)
                    ),
                    "status": tc.get("status", "success") if isinstance(tc, dict) else "success",
                }
                for tc in tools_called[:20]
            ]
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
