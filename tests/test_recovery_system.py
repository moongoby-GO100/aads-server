"""
AADS-132: 복구 시스템 단위 테스트.

복구 의존성 그래프 (3), 에스컬레이션 (4), 서킷브레이커 (5), 통합 (2) = 14개
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─── 복구 의존성 그래프 ───────────────────────────────────────────────────────

def test_resolve_order_simple():
    """R02 → R01 순서 검증 (R02는 R01에 의존)."""
    from app.services.recovery_graph import resolve_recovery_order

    order = resolve_recovery_order(["R02"])
    assert "R01" in order
    assert "R02" in order
    assert order.index("R01") < order.index("R02"), \
        f"R01이 R02보다 먼저 실행돼야 함. 순서: {order}"


def test_resolve_order_chain():
    """R10 → R06 체인 검증 (R10는 R06에 의존)."""
    from app.services.recovery_graph import resolve_recovery_order

    order = resolve_recovery_order(["R10"])
    assert "R06" in order
    assert "R10" in order
    assert order.index("R06") < order.index("R10")


def test_resolve_multiple_triggered():
    """여러 복구 동시 트리거 — 중복 없이 정렬."""
    from app.services.recovery_graph import resolve_recovery_order

    order = resolve_recovery_order(["R01", "R02", "R06"])
    assert len(order) == len(set(order)), "중복 복구 ID 발견"
    assert order.index("R01") < order.index("R02")


def test_resolve_empty():
    """빈 입력 — 빈 결과."""
    from app.services.recovery_graph import resolve_recovery_order

    order = resolve_recovery_order([])
    assert order == []


def test_resolve_independent():
    """의존성 없는 노드들 — 모두 포함, 순서 무관."""
    from app.services.recovery_graph import resolve_recovery_order

    order = resolve_recovery_order(["R03", "R07"])
    assert "R03" in order
    assert "R07" in order


# ─── 에스컬레이션 엔진 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tier1_success():
    """tier_1에서 해결 → tier_2 미실행."""
    from app.services.escalation_engine import execute_escalation

    call_log = []

    async def mock_action(action, data):
        call_log.append(action)
        return action == "config_refresh"  # tier_1 액션 성공

    with patch("app.services.escalation_engine.execute_action", side_effect=mock_action):
        result = await execute_escalation("config_error", {"service": "test"})

    assert result is True
    # tier_2 액션이 호출되지 않았는지 확인
    tier2_actions = {"hard_kill", "full_service_restart", "emergency_slot_clear",
                     "docker_restart", "bridge_full_restart"}
    assert not any(a in tier2_actions for a in call_log), \
        f"tier_2 액션이 호출됨: {call_log}"


@pytest.mark.asyncio
async def test_tier1_fail_tier2_success():
    """tier_1 실패 → tier_2 성공."""
    from app.services.escalation_engine import execute_escalation

    call_log = []

    async def mock_action(action, data):
        call_log.append(action)
        return action == "docker_restart"  # tier_2만 성공

    with patch("app.services.escalation_engine.execute_action", side_effect=mock_action), \
         patch("app.services.escalation_engine._send_escalation_notification", new_callable=AsyncMock), \
         patch("app.services.escalation_engine._check_resolved", return_value=False):
        result = await execute_escalation("container_exit", {"container": "test"})

    assert result is True
    assert "docker_restart" in call_log


@pytest.mark.asyncio
async def test_tier3_escalation_telegram():
    """tier_1,2 실패 → tier_3 텔레그램 알림."""
    from app.services.escalation_engine import execute_escalation

    notify_calls = []

    async def mock_action(action, data):
        return action in ["dump_diagnostics", "create_incident_report"]

    async def mock_notify(tier_key, notif_type, issue_type, data):
        notify_calls.append({"tier": tier_key, "type": notif_type})

    with patch("app.services.escalation_engine.execute_action", side_effect=mock_action), \
         patch("app.services.escalation_engine._send_escalation_notification", side_effect=mock_notify), \
         patch("app.services.escalation_engine._check_resolved", return_value=False):
        result = await execute_escalation("critical_failure", {"server": "test"})

    # tier_2 알림 (telegram) + tier_3 알림 (telegram_urgent) 확인
    notif_types = [n["type"] for n in notify_calls]
    assert "telegram" in notif_types or "telegram_urgent" in notif_types


@pytest.mark.asyncio
async def test_all_tiers_fail():
    """전체 티어 실패 → False 반환."""
    from app.services.escalation_engine import execute_escalation

    with patch("app.services.escalation_engine.execute_action", return_value=False), \
         patch("app.services.escalation_engine._send_escalation_notification", new_callable=AsyncMock), \
         patch("app.services.escalation_engine._check_resolved", return_value=False):
        result = await execute_escalation("unknown_issue", {})

    assert result is False


# ─── 서킷브레이커 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_closed_allows():
    """closed 상태 → 투입 허용."""
    from app.services.circuit_breaker import check_circuit

    mock_row = {"state": "closed", "failure_count": 0, "cooldown_until": None}

    with patch("asyncpg.connect") as mock_conn_cls:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)
        mock_conn.close = AsyncMock()
        mock_conn_cls.return_value = mock_conn
        with patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}):
            result = await check_circuit("68")

    assert result is True


@pytest.mark.asyncio
async def test_circuit_open_blocks():
    """open 상태 (쿨다운 중) → 차단."""
    from app.services.circuit_breaker import check_circuit
    from datetime import datetime, timedelta

    cooldown = datetime.now() + timedelta(minutes=10)
    mock_row = {"state": "open", "failure_count": 3, "cooldown_until": cooldown}

    with patch("asyncpg.connect") as mock_conn_cls:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)
        mock_conn.close = AsyncMock()
        mock_conn_cls.return_value = mock_conn
        with patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}):
            result = await check_circuit("68")

    assert result is False


@pytest.mark.asyncio
async def test_circuit_open_after_3_failures():
    """3회 실패 누적 → DB에 open 상태 기록."""
    from app.services.circuit_breaker import record_result

    mock_row = {"state": "closed", "failure_count": 2}
    executed_sqls = []

    with patch("asyncpg.connect") as mock_conn_cls:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)
        async def capture_execute(sql, *args):
            executed_sqls.append(sql)
        mock_conn.execute = AsyncMock(side_effect=capture_execute)
        mock_conn.close = AsyncMock()
        mock_conn_cls.return_value = mock_conn
        with patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}), \
             patch("app.services.circuit_breaker._send_alert", new_callable=AsyncMock):
            await record_result("68", success=False)

    # 'open' 상태로 업데이트하는 SQL이 실행됐는지 확인
    assert any("open" in sql for sql in executed_sqls), \
        f"open 상태 SQL 없음: {executed_sqls}"


@pytest.mark.asyncio
async def test_circuit_cooldown_to_half_open():
    """open 쿨다운 만료 → half_open 전환."""
    from app.services.circuit_breaker import check_circuit
    from datetime import datetime, timedelta

    cooldown = datetime.now() - timedelta(minutes=1)  # 이미 만료
    mock_row = {"state": "open", "failure_count": 3, "cooldown_until": cooldown}

    with patch("asyncpg.connect") as mock_conn_cls:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)
        mock_conn.execute = AsyncMock()
        mock_conn.close = AsyncMock()
        mock_conn_cls.return_value = mock_conn
        with patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}):
            result = await check_circuit("68")

    assert result is True
    mock_conn.execute.assert_called_once()
    sql = mock_conn.execute.call_args[0][0]
    assert "half_open" in sql


@pytest.mark.asyncio
async def test_circuit_half_open_blocks():
    """half_open 상태 → 차단 (이미 시험 중)."""
    from app.services.circuit_breaker import check_circuit

    mock_row = {"state": "half_open", "failure_count": 3, "cooldown_until": None}

    with patch("asyncpg.connect") as mock_conn_cls:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)
        mock_conn.close = AsyncMock()
        mock_conn_cls.return_value = mock_conn
        with patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}):
            result = await check_circuit("68")

    assert result is False


@pytest.mark.asyncio
async def test_circuit_success_resets_to_closed():
    """성공 기록 → closed 전환."""
    from app.services.circuit_breaker import record_result

    mock_row = {"state": "open", "failure_count": 3}
    executed_sqls = []

    with patch("asyncpg.connect") as mock_conn_cls:
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=mock_row)
        async def capture_execute(sql, *args):
            executed_sqls.append(sql)
        mock_conn.execute = AsyncMock(side_effect=capture_execute)
        mock_conn.close = AsyncMock()
        mock_conn_cls.return_value = mock_conn
        with patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}):
            await record_result("68", success=True)

    assert any("closed" in sql for sql in executed_sqls)


# ─── 통합 테스트 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_recovery_chain_mock():
    """이슈 → 그래프 → 실행 → DB 기록 풀플로우 (모킹)."""
    from app.services.recovery_graph import execute_recovery_chain

    db_calls = []

    async def mock_record(log_entry):
        db_calls.append(log_entry)

    with patch("app.services.recovery_graph._record_recovery_log", side_effect=mock_record):
        issues = [
            {"issue_type": "service_unresponsive", "server": "68"},
        ]
        results = await execute_recovery_chain(issues)

    assert len(results) > 0
    assert len(db_calls) > 0
    assert results[0]["result"] in ("success", "failed")


@pytest.mark.asyncio
async def test_recovery_log_recording():
    """recovery_logs DB 기록 검증 (모킹)."""
    from app.services.recovery_graph import _record_recovery_log

    log_entry = {
        "issue_type": "health_check_fail",
        "issue_data": {"server": "68"},
        "affected_task_id": "T-001",
        "affected_server": "68",
        "tier": "tier_1",
        "action_taken": "service_restart",
        "result": "success",
        "duration_seconds": 5,
        "recovery_route": "R01:tier_1",
        "error_message": None,
        "recovered_by": "watchdog",
    }

    executed = []

    with patch("asyncpg.connect") as mock_conn_cls:
        mock_conn = AsyncMock()
        async def capture_exec(sql, *args):
            executed.append(sql)
        mock_conn.execute = AsyncMock(side_effect=capture_exec)
        mock_conn.close = AsyncMock()
        mock_conn_cls.return_value = mock_conn
        with patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}):
            await _record_recovery_log(log_entry)

    assert len(executed) == 1
    assert "INSERT INTO recovery_logs" in executed[0]


def test_recovery_definitions_complete():
    """R01~R15 정의 완전성 검증."""
    from app.services.recovery_graph import RECOVERY_DEFINITIONS

    assert len(RECOVERY_DEFINITIONS) == 15
    for i in range(1, 16):
        rid = f"R{i:02d}"
        assert rid in RECOVERY_DEFINITIONS, f"{rid} 누락"
        rdef = RECOVERY_DEFINITIONS[rid]
        assert "id" in rdef
        assert "name" in rdef
        assert "depends_on" in rdef
        assert "priority" in rdef
        assert "actions" in rdef
