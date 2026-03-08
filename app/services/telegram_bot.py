"""
AADS-186C: Telegram 알림 봇
- python-telegram-bot>=21.0 기반
- 토큰 미설정 시 graceful 비활성화 (에러 아님)
- 마크다운 형식 알림: 🔴 CRITICAL / 🟡 WARNING / 🔵 INFO
- 매일 09:00 KST 일일 요약
- CEO 명령 처리: /status, /cost, /alerts
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.alert_manager import Alert

logger = logging.getLogger(__name__)

# KST = UTC+9
KST = timezone(timedelta(hours=9))

_SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "WARNING": "🟡",
    "INFO": "🔵",
}


class TelegramBot:
    """
    AADS Telegram 알림 봇.
    TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정 시 모든 메서드 no-op.
    """

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._bot: Any = None
        self._initialized = False

        if token and chat_id:
            try:
                from telegram import Bot  # type: ignore[import]
                self._bot = Bot(token=token)
                self._initialized = True
                logger.info("telegram_bot_initialized", chat_id=chat_id)
            except ImportError:
                logger.warning(
                    "python_telegram_bot_not_installed: "
                    "pip install 'python-telegram-bot>=21.0' 필요"
                )
            except Exception as e:
                logger.warning("telegram_bot_init_failed", error=str(e))

    @property
    def is_ready(self) -> bool:
        return self._initialized and self._bot is not None

    # ─── 알림 발송 ────────────────────────────────────────────────────────────

    async def send_alert(self, alert: "Alert") -> None:
        """마크다운 형식 알림 발송."""
        if not self.is_ready:
            return

        emoji = _SEVERITY_EMOJI.get(alert.severity, "⚪")
        now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

        lines = [
            f"{emoji} *[{alert.severity}] {alert.title}*",
            "",
            f"📋 *카테고리*: `{alert.category}`",
        ]
        if alert.server:
            lines.append(f"🖥️ *서버*: `{alert.server}`")
        if alert.project:
            lines.append(f"📦 *프로젝트*: `{alert.project}`")
        lines += [
            f"🕐 *시각*: {now_kst}",
            "",
            f"📝 {alert.message}",
        ]

        text = "\n".join(lines)
        await self._send(text)

    async def send_message(self, text: str) -> None:
        """일반 텍스트 메시지 발송."""
        if not self.is_ready:
            return
        await self._send(text)

    # ─── 일일 요약 ────────────────────────────────────────────────────────────

    async def send_daily_summary(self) -> None:
        """매일 09:00 KST 일일 요약 발송."""
        if not self.is_ready:
            return

        try:
            summary = await self._build_daily_summary()
            await self._send(summary)
        except Exception as e:
            logger.warning("telegram_daily_summary_failed", error=str(e))

    async def _build_daily_summary(self) -> str:
        """일일 요약 텍스트 빌드."""
        from app.services.alert_manager import get_alert_manager

        now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
        manager = get_alert_manager()
        active_alerts = await manager.get_active_alerts()

        critical_count = sum(1 for a in active_alerts if a.get("severity") == "CRITICAL")
        warning_count = sum(1 for a in active_alerts if a.get("severity") == "WARNING")

        # 완료된 태스크 수 조회
        completed_tasks = await self._get_daily_completed_tasks()
        daily_cost = await self._get_daily_cost()

        lines = [
            "📊 *AADS 일일 요약*",
            f"🕐 {now_kst}",
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🖥️ *서버 상태*",
            f"  • 서버 68 (AADS): 운영 중",
            f"  • 서버 211 (Hub): -",
            f"  • 서버 114 (SF/NTV2): -",
            "",
            f"✅ *완료 태스크*: {completed_tasks}건",
            f"💰 *일일 AI 비용*: ${daily_cost:.2f}",
            "",
            f"🚨 *활성 알림*: {len(active_alerts)}건",
            f"  • 🔴 CRITICAL: {critical_count}건",
            f"  • 🟡 WARNING: {warning_count}건",
        ]

        if active_alerts:
            lines.append("")
            lines.append("*최근 미확인 알림*:")
            for a in active_alerts[:3]:
                emoji = _SEVERITY_EMOJI.get(a.get("severity", ""), "⚪")
                lines.append(f"  {emoji} {a.get('title', '')} — {a.get('category', '')}")

        return "\n".join(lines)

    async def _get_daily_completed_tasks(self) -> int:
        try:
            import asyncpg  # type: ignore[import]
            db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
            conn = await asyncpg.connect(db_url, timeout=10)
            try:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS cnt FROM directive_lifecycle
                    WHERE status = 'done'
                      AND updated_at > NOW() - INTERVAL '24 hours'
                    """
                )
                return int(row["cnt"]) if row else 0
            finally:
                await conn.close()
        except Exception:
            return 0

    async def _get_daily_cost(self) -> float:
        try:
            import asyncpg  # type: ignore[import]
            db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
            conn = await asyncpg.connect(db_url, timeout=10)
            try:
                row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(cost), 0) AS daily_cost
                    FROM chat_messages
                    WHERE created_at >= NOW() - INTERVAL '24 hours'
                      AND role = 'assistant'
                    """
                )
                return float(row["daily_cost"]) if row else 0.0
            finally:
                await conn.close()
        except Exception:
            return 0.0

    # ─── CEO 명령 처리 ────────────────────────────────────────────────────────

    async def handle_command(self, command: str) -> str:
        """
        CEO Telegram 명령 처리.
        - /status → 전체 서버 상태
        - /cost → 오늘 AI 비용
        - /alerts → 미확인 알림 목록
        """
        cmd = command.strip().lower().split()[0] if command.strip() else ""

        if cmd == "/status":
            return await self._cmd_status()
        elif cmd == "/cost":
            return await self._cmd_cost()
        elif cmd == "/alerts":
            return await self._cmd_alerts()
        else:
            return (
                "📋 *AADS Bot 명령어*\n\n"
                "/status — 전체 서버 상태\n"
                "/cost — 오늘 AI 비용\n"
                "/alerts — 미확인 알림 목록"
            )

    async def _cmd_status(self) -> str:
        now_kst = datetime.now(KST).strftime("%H:%M KST")
        return (
            f"🖥️ *서버 상태* ({now_kst})\n\n"
            "• 서버 68 (68.183.183.11) — AADS\n"
            "• 서버 211 (211.188.51.113) — Hub/Bridge/KIS/GO100\n"
            "• 서버 114 (116.120.58.155) — SF/NTV2/NAS"
        )

    async def _cmd_cost(self) -> str:
        cost = await self._get_daily_cost()
        return f"💰 *오늘 AI 비용*: ${cost:.4f}"

    async def _cmd_alerts(self) -> str:
        from app.services.alert_manager import get_alert_manager
        manager = get_alert_manager()
        alerts = await manager.get_active_alerts()
        if not alerts:
            return "✅ 미확인 알림 없음"

        lines = [f"🚨 *미확인 알림* ({len(alerts)}건)\n"]
        for a in alerts[:10]:
            emoji = _SEVERITY_EMOJI.get(a.get("severity", ""), "⚪")
            lines.append(f"{emoji} [{a.get('severity','')}] {a.get('title','')} — {a.get('category','')}")
        return "\n".join(lines)

    # ─── 내부 발송 ────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        """실제 Telegram API 호출."""
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.debug("telegram_message_sent", length=len(text))
        except Exception as e:
            logger.warning("telegram_send_failed", error=str(e))


# ─── 싱글턴 ──────────────────────────────────────────────────────────────────

_telegram_bot: Optional[TelegramBot] = None


def init_telegram_bot() -> Optional[TelegramBot]:
    """
    TelegramBot 초기화.
    TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정 시 None 반환.
    """
    global _telegram_bot

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.info("telegram_bot_disabled: TELEGRAM_BOT_TOKEN/CHAT_ID 미설정")
        return None

    _telegram_bot = TelegramBot(token=token, chat_id=chat_id)
    return _telegram_bot


def get_telegram_bot() -> Optional[TelegramBot]:
    """싱글턴 TelegramBot 반환 (초기화 전이면 None)."""
    return _telegram_bot
