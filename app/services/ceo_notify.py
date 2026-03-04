"""
CEO 알림 서비스 — T-026.

notify_ceo(project_id, qa_result, screenshots, scorecard)
  1. 텔레그램으로 전송 (GO100_TELEGRAM_BOT_TOKEN 또는 AADS_TELEGRAM_BOT_TOKEN)
     - 스크린샷 이미지 (diff 있으면 diff 이미지도)
     - 스코어카드 요약
     - PASS/CONDITIONAL/FAIL 판정
  2. Context API (system_memory)에도 결과 저장
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# 텔레그램 토큰 환경변수 (우선순위: AADS_TELEGRAM_BOT_TOKEN → GO100_TELEGRAM_BOT_TOKEN)
def _get_telegram_token() -> str:
    return (
        os.getenv("AADS_TELEGRAM_BOT_TOKEN", "")
        or os.getenv("GO100_TELEGRAM_BOT_TOKEN", "")
    )

def _get_telegram_chat_id() -> str:
    return (
        os.getenv("AADS_TELEGRAM_CHAT_ID", "")
        or os.getenv("GO100_TELEGRAM_CHAT_ID", "")
    )


# ---------------------------------------------------------------------------
# 텔레그램 헬퍼
# ---------------------------------------------------------------------------

async def _send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """텔레그램 메시지 전송 (텍스트)."""
    import aiohttp

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning("telegram_send_failed", error=data)
                    return False
                return True
    except Exception as e:
        logger.warning("telegram_send_error", error=str(e))
        return False


async def _send_telegram_photo(token: str, chat_id: str, photo_path: str, caption: str = "") -> bool:
    """텔레그램 사진 전송."""
    import aiohttp

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("chat_id", chat_id)
            form.add_field("caption", caption[:1024])
            form.add_field("photo", f, filename=Path(photo_path).name, content_type="image/png")
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    data = await resp.json()
                    if not data.get("ok"):
                        logger.warning("telegram_photo_failed", path=photo_path, error=data)
                        return False
                    return True
    except Exception as e:
        logger.warning("telegram_photo_error", path=photo_path, error=str(e))
        return False


# ---------------------------------------------------------------------------
# 메인 알림 함수
# ---------------------------------------------------------------------------

async def notify_ceo(
    project_id: str,
    qa_result: Dict[str, Any],
    screenshots: Optional[List[str]] = None,
    scorecard: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    CEO 알림 전송.

    1. 텔레그램: 텍스트 메시지 + 스크린샷/diff 이미지
    2. Context API: system_memory qa_notifications 카테고리에 저장

    Returns:
        {"telegram_sent": bool, "context_saved": bool, "notified_at": str}
    """
    notified_at = datetime.utcnow().isoformat()
    telegram_sent = False
    context_saved = False

    verdict = qa_result.get("verdict", "UNKNOWN")
    test_status = qa_result.get("test_status", "?")
    visual_status = qa_result.get("visual_status", "?")
    design_score = qa_result.get("design_score", 0)
    deploy_url = qa_result.get("deploy_url", "")

    # ------------------------------------------------------------------
    # 1. 메시지 조립
    # ------------------------------------------------------------------
    verdict_emoji = {
        "AUTO PASS": "✅",
        "CEO 확인 요청": "⚠️",
        "AUTO FAIL": "❌",
    }.get(verdict, "❓")

    scorecard_text = ""
    if scorecard:
        scores = scorecard.get("scores", {})
        category_labels = {
            "visual_consistency": "시각 일관성",
            "accessibility": "접근성",
            "interaction_clarity": "인터랙션 명확성",
            "brand_coherence": "브랜드 일관성",
            "polish": "완성도",
        }
        score_lines = []
        for key, label in category_labels.items():
            s = scores.get(key, {})
            if s:
                score_lines.append(f"  • {label}: {s.get('score',0)}/10")
        scorecard_text = "\n".join(score_lines)

        critical = scorecard.get("critical_issues", [])
        if critical:
            scorecard_text += "\n<b>즉시 수정:</b>\n" + "\n".join(f"  - {i}" for i in critical[:3])

    message = (
        f"{verdict_emoji} <b>AADS QA 결과</b>\n\n"
        f"<b>프로젝트</b>: {project_id}\n"
        f"<b>판정</b>: {verdict}\n"
        f"<b>배포 URL</b>: {deploy_url}\n\n"
        f"<b>테스트</b>: {test_status}\n"
        f"<b>Visual Regression</b>: {visual_status}\n"
        f"<b>디자인 점수</b>: {design_score}/50\n"
    )
    if scorecard:
        message += f"\n<b>스코어카드:</b>\n{scorecard_text}\n"
    message += f"\n<i>{notified_at} UTC</i>"

    # ------------------------------------------------------------------
    # 2. 텔레그램 전송
    # ------------------------------------------------------------------
    token = _get_telegram_token()
    chat_id = _get_telegram_chat_id()

    if token and chat_id:
        # 텍스트 메시지
        ok = await _send_telegram_message(token, chat_id, message)
        telegram_sent = ok

        # 스크린샷 이미지 (최대 2장)
        if screenshots:
            for path in (screenshots or [])[:2]:
                if path and Path(path).exists():
                    caption = f"스크린샷: {Path(path).name}"
                    await _send_telegram_photo(token, chat_id, path, caption)

        # diff 이미지
        diff_images = qa_result.get("diff_images", [])
        for diff_path in (diff_images or [])[:2]:
            if diff_path and Path(diff_path).exists():
                caption = f"Visual Diff: {Path(diff_path).name}"
                await _send_telegram_photo(token, chat_id, diff_path, caption)

        logger.info(
            "ceo_notify_telegram",
            project_id=project_id,
            verdict=verdict,
            telegram_sent=telegram_sent,
        )
    else:
        logger.info(
            "ceo_notify_telegram_skipped",
            reason="token or chat_id not set",
            project_id=project_id,
        )

    # ------------------------------------------------------------------
    # 3. Context API 저장
    # ------------------------------------------------------------------
    try:
        from app.memory.store import memory_store

        await memory_store.put_system(
            category="qa_notifications",
            key=f"{project_id}_{notified_at[:19].replace(':', '-')}",
            value={
                "project_id": project_id,
                "verdict": verdict,
                "test_status": test_status,
                "visual_status": visual_status,
                "design_score": design_score,
                "deploy_url": deploy_url,
                "scorecard_summary": scorecard.get("summary", "") if scorecard else "",
                "telegram_sent": telegram_sent,
                "notified_at": notified_at,
            },
            updated_by="ceo_notify",
        )
        context_saved = True
        logger.info("ceo_notify_context_saved", project_id=project_id)
    except Exception as e:
        logger.warning("ceo_notify_context_save_failed", error=str(e))

    return {
        "telegram_sent": telegram_sent,
        "context_saved": context_saved,
        "notified_at": notified_at,
    }


async def notify_ceo_escalation(
    project_id: str,
    task_id: str,
    reason: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    CEO 에스컬레이션 알림 — Supervisor max_iterations / LLM 한도 초과 시.
    텔레그램으로 즉시 알림 + Context API 저장.
    """
    notified_at = datetime.utcnow().isoformat()
    token = _get_telegram_token()
    chat_id = _get_telegram_chat_id()
    telegram_sent = False

    ctx = context or {}
    message = (
        f"🚨 <b>AADS CEO 에스컬레이션</b>\n\n"
        f"<b>프로젝트</b>: {project_id}\n"
        f"<b>태스크 ID</b>: {task_id}\n"
        f"<b>사유</b>: {reason}\n\n"
        f"<b>반복 횟수</b>: {ctx.get('iteration_count', 0)}\n"
        f"<b>LLM 호출</b>: {ctx.get('llm_calls_count', 0)}\n"
        f"<b>비용</b>: ${ctx.get('total_cost_usd', 0.0):.4f}\n"
        f"<b>설명</b>: {ctx.get('description', '')[:200]}\n\n"
        f"<i>{notified_at} UTC</i>"
    )

    if token and chat_id:
        telegram_sent = await _send_telegram_message(token, chat_id, message)

    logger.info(
        "ceo_escalation_sent",
        project_id=project_id,
        task_id=task_id,
        reason=reason,
        telegram_sent=telegram_sent,
    )
    return {"telegram_sent": telegram_sent, "notified_at": notified_at}
