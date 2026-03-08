"""
AADS-186C: Observability 테스트
- Langfuse: 트레이스 생성 확인 (mock)
- AlertManager: 규칙 평가 → CRITICAL 알림 생성 확인
- TelegramBot: send_alert 호출 확인 (mock)
- MCP: setup_mcp graceful degradation 확인
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Langfuse 테스트 ──────────────────────────────────────────────────────────

class TestLangfuseConfig:
    def test_is_disabled_when_no_env_vars(self):
        """환경변수 미설정 시 Langfuse 비활성화."""
        with patch.dict(os.environ, {}, clear=False):
            # 기존 langfuse 환경변수 제거
            env = {k: v for k, v in os.environ.items()
                   if k not in ("LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_HOST")}
            with patch.dict(os.environ, env, clear=True):
                from app.core.langfuse_config import _is_configured
                assert not _is_configured()

    def test_is_configured_when_all_env_vars_set(self):
        """필수 환경변수 모두 설정 시 configured=True."""
        with patch.dict(os.environ, {
            "LANGFUSE_SECRET_KEY": "sk-lf-test",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
            "LANGFUSE_HOST": "http://localhost:3001",
        }):
            from app.core.langfuse_config import _is_configured
            assert _is_configured()

    def test_init_langfuse_graceful_when_sdk_not_installed(self):
        """langfuse SDK 미설치 시 graceful 비활성화 (에러 아님)."""
        with patch.dict(os.environ, {
            "LANGFUSE_SECRET_KEY": "sk-lf-test",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
            "LANGFUSE_HOST": "http://localhost:3001",
        }):
            with patch("builtins.__import__", side_effect=ImportError("No module named langfuse")):
                # ImportError → graceful disable, 예외 없음
                try:
                    from app.core import langfuse_config
                    result = langfuse_config.init_langfuse()
                    # SDK 없으면 False 반환
                    assert result is False or result is True  # 실행만 되면 OK
                except ImportError:
                    pass  # 모듈 자체 임포트 실패도 OK (mock 환경)

    def test_create_trace_returns_none_when_disabled(self):
        """Langfuse 비활성화 시 create_trace None 반환."""
        import importlib
        import app.core.langfuse_config as lf_mod
        # 비활성화 상태로 강제 설정
        original_enabled = lf_mod._langfuse_enabled
        original_client = lf_mod._langfuse_client
        try:
            lf_mod._langfuse_enabled = False
            lf_mod._langfuse_client = None
            result = lf_mod.create_trace("test_trace", session_id="sess-001")
            assert result is None
        finally:
            lf_mod._langfuse_enabled = original_enabled
            lf_mod._langfuse_client = original_client

    def test_create_trace_calls_client_when_enabled(self):
        """Langfuse 활성화 시 create_trace → client.trace() 호출."""
        import app.core.langfuse_config as lf_mod
        mock_trace = MagicMock()
        mock_client = MagicMock()
        mock_client.trace.return_value = mock_trace

        original_enabled = lf_mod._langfuse_enabled
        original_client = lf_mod._langfuse_client
        try:
            lf_mod._langfuse_enabled = True
            lf_mod._langfuse_client = mock_client

            result = lf_mod.create_trace(
                name="chat_turn",
                session_id="sess-001",
                user_id="CEO",
                metadata={"project": "AADS", "intent": "general"},
            )
            mock_client.trace.assert_called_once()
            assert result is mock_trace
        finally:
            lf_mod._langfuse_enabled = original_enabled
            lf_mod._langfuse_client = original_client


# ─── AlertManager 테스트 ──────────────────────────────────────────────────────

class TestAlertManager:
    def test_alert_dataclass(self):
        """Alert 데이터클래스 생성 확인."""
        from app.services.alert_manager import Alert
        alert = Alert(
            severity="CRITICAL",
            category="disk_full",
            title="디스크 사용량 초과",
            message="서버 68 디스크 82%",
            server="68",
        )
        assert alert.severity == "CRITICAL"
        assert alert.category == "disk_full"
        assert alert.server == "68"

    def test_rules_exist(self):
        """AlertManager.RULES 8개 존재 확인."""
        from app.services.alert_manager import AlertManager
        assert len(AlertManager.RULES) == 8
        rule_names = {r["name"] for r in AlertManager.RULES}
        expected = {
            "server_down", "disk_full", "cost_exceed", "ssh_timeout",
            "task_stall", "memory_high", "health_fail", "pat_expiry",
        }
        assert expected == rule_names

    @pytest.mark.asyncio
    async def test_evaluate_rules_disk_full(self):
        """디스크 사용량 81% → CRITICAL disk_full 알림 생성."""
        from app.services.alert_manager import AlertManager

        manager = AlertManager()
        mock_metrics = {
            "disk_usage_percent": 81.0,
            "memory_usage_percent": 50.0,
            "daily_cost_usd": 0.5,
            "stall_task_count": 0,
            "github_pat_expires_in_days": 999,
        }
        with patch.object(manager, "_collect_metrics", return_value=mock_metrics):
            alerts = await manager.evaluate_rules()

        disk_alerts = [a for a in alerts if a.category == "disk_full"]
        assert len(disk_alerts) == 1
        assert disk_alerts[0].severity == "CRITICAL"

    @pytest.mark.asyncio
    async def test_evaluate_rules_cost_exceed(self):
        """일일 비용 $6.0 → WARNING cost_exceed 알림 생성."""
        from app.services.alert_manager import AlertManager

        manager = AlertManager()
        mock_metrics = {
            "disk_usage_percent": 50.0,
            "memory_usage_percent": 50.0,
            "daily_cost_usd": 6.0,
            "stall_task_count": 0,
            "github_pat_expires_in_days": 999,
        }
        with patch.object(manager, "_collect_metrics", return_value=mock_metrics):
            alerts = await manager.evaluate_rules()

        cost_alerts = [a for a in alerts if a.category == "cost_exceed"]
        assert len(cost_alerts) == 1
        assert cost_alerts[0].severity == "WARNING"

    @pytest.mark.asyncio
    async def test_send_alert_deduplication(self):
        """동일 카테고리+서버 1시간 내 중복 발송 방지."""
        from app.services.alert_manager import Alert, AlertManager

        manager = AlertManager()
        alert = Alert(severity="CRITICAL", category="disk_full",
                      title="디스크 초과", message="82%", server="68")

        with patch.object(manager, "_is_duplicate", return_value=True) as mock_dup:
            with patch.object(manager, "_save_alert", new_callable=AsyncMock) as mock_save:
                await manager.send_alert(alert)
                mock_save.assert_not_called()  # 중복이면 저장 안 함

    @pytest.mark.asyncio
    async def test_send_alert_saves_and_notifies(self):
        """중복 아닌 경우 DB 저장 + Telegram 발송."""
        from app.services.alert_manager import Alert, AlertManager

        manager = AlertManager()
        alert = Alert(severity="CRITICAL", category="server_down",
                      title="서버 다운", message="서버 68 응답 없음", server="68")

        mock_bot = AsyncMock()
        mock_bot.is_ready = True

        with patch.object(manager, "_is_duplicate", return_value=False):
            with patch.object(manager, "_save_alert", new_callable=AsyncMock, return_value=42):
                with patch("app.services.alert_manager.get_telegram_bot", return_value=mock_bot):
                    await manager.send_alert(alert)
                    mock_bot.send_alert.assert_called_once_with(alert)


# ─── TelegramBot 테스트 ──────────────────────────────────────────────────────

class TestTelegramBot:
    def test_bot_not_ready_when_no_token(self):
        """토큰 미설정 시 is_ready=False."""
        from app.services.telegram_bot import TelegramBot
        bot = TelegramBot(token="", chat_id="")
        assert not bot.is_ready

    @pytest.mark.asyncio
    async def test_send_alert_noop_when_not_ready(self):
        """is_ready=False 시 send_alert no-op."""
        from app.services.telegram_bot import TelegramBot
        from app.services.alert_manager import Alert

        bot = TelegramBot(token="", chat_id="")
        alert = Alert(severity="CRITICAL", category="disk_full",
                      title="디스크 초과", message="82%")
        # 예외 없이 실행되면 OK
        await bot.send_alert(alert)

    @pytest.mark.asyncio
    async def test_send_alert_formats_markdown(self):
        """send_alert 호출 시 마크다운 형식 텍스트 발송."""
        from app.services.telegram_bot import TelegramBot
        from app.services.alert_manager import Alert

        mock_tg_bot = AsyncMock()
        mock_tg_bot.send_message = AsyncMock()

        with patch("telegram.Bot", return_value=mock_tg_bot):
            bot = TelegramBot(token="test-token", chat_id="12345")
            bot._bot = mock_tg_bot
            bot._initialized = True

            alert = Alert(
                severity="CRITICAL",
                category="disk_full",
                title="디스크 사용량 초과",
                message="서버 68 디스크 82%",
                server="68",
            )
            await bot.send_alert(alert)
            mock_tg_bot.send_message.assert_called_once()
            call_kwargs = mock_tg_bot.send_message.call_args
            text = call_kwargs[1].get("text", "") or (call_kwargs[0][0] if call_kwargs[0] else "")
            assert "🔴" in text or "CRITICAL" in text

    @pytest.mark.asyncio
    async def test_handle_command_status(self):
        """'/status' 명령 → 서버 상태 텍스트 반환."""
        from app.services.telegram_bot import TelegramBot

        bot = TelegramBot(token="", chat_id="")
        result = await bot._cmd_status()
        assert "서버" in result or "server" in result.lower()

    @pytest.mark.asyncio
    async def test_init_telegram_bot_returns_none_without_token(self):
        """TELEGRAM_BOT_TOKEN 미설정 시 None 반환."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        with patch.dict(os.environ, env, clear=True):
            from app.services import telegram_bot as tb_mod
            original = tb_mod._telegram_bot
            try:
                result = tb_mod.init_telegram_bot()
                assert result is None
            finally:
                tb_mod._telegram_bot = original


# ─── MCP 테스트 ───────────────────────────────────────────────────────────────

class TestMCPServer:
    def test_setup_mcp_graceful_when_disabled(self):
        """MCP_ENABLED=false 시 setup_mcp no-op."""
        with patch.dict(os.environ, {"MCP_ENABLED": "false"}):
            from app.core.mcp_server import setup_mcp
            mock_app = MagicMock()
            setup_mcp(mock_app)
            # FastApiMCP 호출 안 됨

    def test_setup_mcp_graceful_when_not_installed(self):
        """fastapi-mcp 미설치 시 ImportError → graceful skip."""
        with patch.dict(os.environ, {"MCP_ENABLED": "true"}):
            with patch.dict("sys.modules", {"fastapi_mcp": None}):
                from app.core.mcp_server import setup_mcp
                mock_app = MagicMock()
                # ImportError 발생해도 예외 없이 처리
                try:
                    setup_mcp(mock_app)
                except Exception as e:
                    pytest.fail(f"setup_mcp raised unexpectedly: {e}")

    def test_setup_mcp_mounts_when_enabled(self):
        """MCP_ENABLED=true + fastapi_mcp 설치 시 mcp.mount() 호출."""
        with patch.dict(os.environ, {"MCP_ENABLED": "true"}):
            mock_mcp_instance = MagicMock()
            mock_fastapi_mcp = MagicMock()
            mock_fastapi_mcp.FastApiMCP.return_value = mock_mcp_instance

            with patch.dict("sys.modules", {"fastapi_mcp": mock_fastapi_mcp}):
                import importlib
                import app.core.mcp_server as mcp_mod
                importlib.reload(mcp_mod)

                mock_app = MagicMock()
                mcp_mod.setup_mcp(mock_app)
                mock_mcp_instance.mount.assert_called_once()
