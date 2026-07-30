"""OHVIS Loop Executor — Phase 1 반복 실행 엔진.

단일 iteration 실행: 안전 체크 → LLM 호출 → 결과 판단 → DB 기록 → 다음 실행 예약.
기획서: docs/AADS-LAYOUT-001_OHVIS-LOOP-SYSTEM.md §8, §9
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.services.loop_controller import (
    get_loop,
    check_safety_limits,
    record_iteration,
    update_loop_status,
    recalculate_cost_on_fallback,
)

logger = logging.getLogger("ohvis.loop_executor")


async def run_iteration(loop_id: int) -> dict:
    """단일 iteration 실행. 반환: {ok, loop_id, iteration, status, summary}"""
    loop = await get_loop(loop_id)
    if not loop:
        return {"ok": False, "error": "loop_not_found"}

    if loop["status"] != "active":
        return {"ok": False, "error": f"loop_not_active: {loop['status']}"}

    safety = await check_safety_limits(loop_id)
    if not safety["ok"]:
        action = safety.get("action", "pause")
        reason = safety["reason"]
        if action == "complete":
            await update_loop_status(loop_id, "completed", reason)
        elif action == "fail":
            await update_loop_status(loop_id, "failed", reason)
        else:
            await update_loop_status(loop_id, "paused", reason)
        return {"ok": False, "error": reason, "action": action}

    iteration_num = (loop["current_iteration"] or 0) + 1
    model_id = loop["execution_model_id"]
    loop_type = loop["loop_type"]
    t0 = time.monotonic()

    try:
        result = await _execute_by_type(loop, iteration_num)
        duration_ms = int((time.monotonic() - t0) * 1000)

        iter_status = result.get("status", "success")
        summary = result.get("summary", "")
        cost = result.get("cost_usd", 0.0)
        llm_calls = result.get("llm_calls", 1)
        alert_sent = result.get("alert_sent", False)

        await record_iteration(
            loop_id=loop_id,
            iteration_num=iteration_num,
            status=iter_status,
            result_summary=summary,
            result_data=result.get("data"),
            llm_calls=llm_calls,
            cost_usd=cost,
            duration_ms=duration_ms,
            model_used=model_id,
            alert_sent=alert_sent,
            alert_channel=result.get("alert_channel"),
        )

        if result.get("goal_reached"):
            await update_loop_status(loop_id, "completed", "goal_reached")

        logger.info(
            "Loop #%d iter %d: %s (%.1fs, $%.4f)",
            loop_id, iteration_num, iter_status, duration_ms / 1000, cost,
        )
        return {
            "ok": True,
            "loop_id": loop_id,
            "iteration": iteration_num,
            "status": iter_status,
            "summary": summary,
            "duration_ms": duration_ms,
            "cost_usd": cost,
        }

    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.exception("Loop #%d iter %d failed: %s", loop_id, iteration_num, exc)

        await record_iteration(
            loop_id=loop_id,
            iteration_num=iteration_num,
            status="failure",
            result_summary=str(exc)[:500],
            llm_calls=0,
            cost_usd=0.0,
            duration_ms=duration_ms,
            model_used=model_id,
        )

        safety_after = await check_safety_limits(loop_id)
        if not safety_after["ok"]:
            action = safety_after.get("action", "fail")
            await update_loop_status(loop_id, "failed" if action == "fail" else "paused", safety_after["reason"])

        return {
            "ok": False,
            "loop_id": loop_id,
            "iteration": iteration_num,
            "status": "failure",
            "error": str(exc)[:500],
            "duration_ms": duration_ms,
        }


async def _execute_by_type(loop: dict, iteration_num: int) -> dict:
    loop_type = loop["loop_type"]
    if loop_type == "monitor":
        return await _execute_monitor(loop, iteration_num)
    elif loop_type == "task":
        return await _execute_task(loop, iteration_num)
    elif loop_type == "sequential":
        return await _execute_sequential(loop, iteration_num)
    else:
        return {"status": "failure", "summary": f"Unknown loop_type: {loop_type}"}


async def _execute_monitor(loop: dict, iteration_num: int) -> dict:
    from app.core.anthropic_client import call_llm_with_fallback

    command = loop["original_command"]
    model_id = loop["execution_model_id"]
    success_cond = loop.get("success_condition") or {}
    alert_cond = loop.get("alert_condition") or {}

    system_prompt = (
        "당신은 OHVIS 감시 에이전트입니다. 주어진 감시 지시를 수행하고 결과를 JSON으로 보고하세요.\n"
        'JSON 형식: {"status": "normal|warning|critical", "summary": "한줄 요약", '
        '"details": "상세 내용", "needs_alert": true/false}\n'
        "반드시 유효한 JSON만 출력하세요."
    )
    user_msg = (
        f"감시 지시: {command}\n"
        f"반복 #{iteration_num}\n"
        f"성공조건: {success_cond}\n"
        f"알림조건: {alert_cond}"
    )

    resp = await call_llm_with_fallback(
        prompt=user_msg,
        model=model_id or "claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=system_prompt,
    )

    text = resp or ""
    parsed = _try_parse_json(text)
    needs_alert = parsed.get("needs_alert", False) if parsed else False
    status_str = parsed.get("status", "normal") if parsed else "success"

    alert_sent = False
    if needs_alert and alert_cond.get("telegram", True):
        try:
            from app.services.telegram_service import send_telegram_message
            await send_telegram_message(
                f"⚠️ OHVIS 감시 알림 (루프 #{loop['id']}, iter #{iteration_num})\n"
                f"{parsed.get('summary', text[:200])}"
            )
            alert_sent = True
        except Exception as e:
            logger.warning("Telegram alert failed: %s", e)

    goal_reached = status_str == "normal" and success_cond.get("on_normal_count")
    return {
        "status": "success" if status_str in ("normal", "warning") else "failure",
        "summary": parsed.get("summary", text[:200]) if parsed else text[:200],
        "data": parsed,
        "llm_calls": 1,
        "cost_usd": _estimate_cost(model_id, text),
        "alert_sent": alert_sent,
        "alert_channel": "telegram" if alert_sent else None,
        "goal_reached": goal_reached,
    }


async def _execute_task(loop: dict, iteration_num: int) -> dict:
    from app.core.anthropic_client import call_llm_with_fallback

    command = loop["original_command"]
    model_id = loop["execution_model_id"]
    last_result = loop.get("last_result") or {}

    system_prompt = (
        "당신은 OHVIS 작업 에이전트입니다. 주어진 목표를 달성하세요.\n"
        "이전 시도 결과를 참고하여 개선된 접근을 시도하세요.\n"
        'JSON 형식: {"status": "progress|done|blocked", "summary": "한줄 요약", '
        '"details": "수행 내용", "goal_reached": true/false, "next_action": "다음 할 일"}\n'
        "반드시 유효한 JSON만 출력하세요."
    )
    prev_summary = last_result.get("summary", "첫 번째 시도") if isinstance(last_result, dict) else "첫 번째 시도"
    user_msg = (
        f"목표: {command}\n"
        f"반복 #{iteration_num}\n"
        f"이전 결과: {prev_summary}"
    )

    resp = await call_llm_with_fallback(
        prompt=user_msg,
        model=model_id or "claude-haiku-4-5-20251001",
        max_tokens=2000,
        system=system_prompt,
    )

    text = resp or ""
    parsed = _try_parse_json(text)
    goal_reached = parsed.get("goal_reached", False) if parsed else False

    return {
        "status": "success" if parsed else "failure",
        "summary": parsed.get("summary", text[:200]) if parsed else text[:200],
        "data": parsed,
        "llm_calls": 1,
        "cost_usd": _estimate_cost(model_id, text),
        "goal_reached": goal_reached,
    }


async def _execute_sequential(loop: dict, iteration_num: int) -> dict:
    parsed_intent = loop.get("parsed_intent") or {}
    task_list = parsed_intent.get("task_list", [])

    if not task_list:
        return {"status": "failure", "summary": "task_list empty", "goal_reached": False}

    task_idx = min(iteration_num - 1, len(task_list) - 1)
    current_task = task_list[task_idx]

    from app.core.anthropic_client import call_llm_with_fallback

    model_id = loop["execution_model_id"]
    system_prompt = (
        "당신은 OHVIS 순차 작업 에이전트입니다.\n"
        "현재 단계의 작업을 수행하고 결과를 보고하세요.\n"
        'JSON 형식: {"status": "done|failed", "summary": "한줄 요약", "details": "수행 내용"}\n'
        "반드시 유효한 JSON만 출력하세요."
    )
    user_msg = (
        f"전체 목표: {loop['original_command']}\n"
        f"현재 단계 ({task_idx + 1}/{len(task_list)}): {current_task}\n"
        f"반복 #{iteration_num}"
    )

    resp = await call_llm_with_fallback(
        prompt=user_msg,
        model=model_id or "claude-haiku-4-5-20251001",
        max_tokens=2000,
        system=system_prompt,
    )

    text = resp or ""
    parsed = _try_parse_json(text)
    all_done = task_idx >= len(task_list) - 1 and parsed and parsed.get("status") == "done"

    return {
        "status": parsed.get("status", "success") if parsed else "failure",
        "summary": parsed.get("summary", text[:200]) if parsed else text[:200],
        "data": {**(parsed or {}), "task_index": task_idx, "total_tasks": len(task_list)},
        "llm_calls": 1,
        "cost_usd": _estimate_cost(model_id, text),
        "goal_reached": all_done,
    }


def _estimate_cost(model_id: str | None, response_text: str) -> float:
    """call_llm_with_fallback는 str을 반환하므로 모델 단가 기반 추정."""
    if not response_text:
        return 0.0
    out_tokens = max(len(response_text) // 4, 1)
    in_tokens = out_tokens * 3
    per_m_in, per_m_out = 3.0, 15.0
    if model_id:
        m = model_id.lower()
        if "opus" in m:
            per_m_in, per_m_out = 15.0, 75.0
        elif "haiku" in m:
            per_m_in, per_m_out = 0.80, 4.0
        elif "gpt" in m:
            per_m_in, per_m_out = 2.50, 10.0
        elif "gemini" in m:
            per_m_in, per_m_out = 0.15, 0.60
    return round((in_tokens * per_m_in + out_tokens * per_m_out) / 1_000_000, 6)


def _try_parse_json(text: str) -> dict | None:
    import json
    import re
    text = text.strip()
    m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
