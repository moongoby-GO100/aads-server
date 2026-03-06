"""
AADS-132: 서킷브레이커 — 서버별 상태 관리 (closed/open/half_open).

threshold: 3회 연속 실패 → open
cooldown:  5분 → half_open (시험 1건)
half_open: 성공 → closed, 실패 → open 재진입
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import structlog

logger = structlog.get_logger()

FAILURE_THRESHOLD = 3
COOLDOWN_MINUTES = 5


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")


async def check_circuit(server: str) -> bool:
    """
    투입 허용 여부 반환.
    closed → True, open(쿨다운 중) → False, half_open → False(이미 시험 중)
    open(쿨다운 만료) → half_open으로 전환 후 True
    """
    db_url = _db_url()
    if not db_url:
        return True  # DB 없으면 허용

    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            row = await conn.fetchrow(
                "SELECT state, failure_count, cooldown_until FROM circuit_breaker_state WHERE server=$1",
                server,
            )
            if not row:
                return True

            state = row["state"]
            cooldown_until = row["cooldown_until"]
            now = datetime.now()

            if state == "closed":
                return True

            if state == "open":
                if cooldown_until and now > cooldown_until:
                    # 쿨다운 만료 → half_open 전환
                    await conn.execute(
                        """
                        UPDATE circuit_breaker_state
                        SET state='half_open', updated_at=NOW()
                        WHERE server=$1
                        """,
                        server,
                    )
                    logger.info("circuit_breaker_half_open", server=server)
                    return True
                logger.info("circuit_breaker_blocked", server=server, cooldown_until=str(cooldown_until))
                return False

            if state == "half_open":
                # 이미 시험 중 → 차단
                return False

            return True
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("circuit_breaker_check_failed", server=server, error=str(e))
        return True  # 장애 시 허용 (fail-open)


async def record_result(server: str, success: bool) -> None:
    """실행 결과를 circuit_breaker_state에 기록."""
    db_url = _db_url()
    if not db_url:
        return

    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            row = await conn.fetchrow(
                "SELECT state, failure_count FROM circuit_breaker_state WHERE server=$1",
                server,
            )
            if not row:
                return

            current_state = row["state"]
            failure_count = row["failure_count"] or 0

            if success:
                await conn.execute(
                    """
                    UPDATE circuit_breaker_state
                    SET state='closed', failure_count=0,
                        last_failure_at=NULL, cooldown_until=NULL,
                        updated_at=NOW()
                    WHERE server=$1
                    """,
                    server,
                )
                if current_state != "closed":
                    logger.info("circuit_breaker_closed", server=server)
            else:
                new_count = failure_count + 1
                if new_count >= FAILURE_THRESHOLD:
                    cooldown = datetime.now() + timedelta(minutes=COOLDOWN_MINUTES)
                    await conn.execute(
                        """
                        UPDATE circuit_breaker_state
                        SET state='open', failure_count=$2, last_failure_at=NOW(),
                            cooldown_until=$3, opened_at=NOW(), updated_at=NOW()
                        WHERE server=$1
                        """,
                        server, new_count, cooldown,
                    )
                    logger.warning(
                        "circuit_breaker_open",
                        server=server,
                        failure_count=new_count,
                        cooldown_until=str(cooldown),
                    )
                    await _send_alert(server, new_count)
                else:
                    await conn.execute(
                        """
                        UPDATE circuit_breaker_state
                        SET failure_count=$2, last_failure_at=NOW(), updated_at=NOW()
                        WHERE server=$1
                        """,
                        server, new_count,
                    )
                    logger.info("circuit_breaker_failure_recorded", server=server, count=new_count)
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("circuit_breaker_record_failed", server=server, error=str(e))


async def reset_circuit(server: str) -> bool:
    """서킷브레이커 수동 리셋 → closed 상태로 강제 전환."""
    db_url = _db_url()
    if not db_url:
        return False
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            await conn.execute(
                """
                UPDATE circuit_breaker_state
                SET state='closed', failure_count=0,
                    last_failure_at=NULL, cooldown_until=NULL, updated_at=NOW()
                WHERE server=$1
                """,
                server,
            )
            logger.info("circuit_breaker_manual_reset", server=server)
            return True
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("circuit_breaker_reset_failed", server=server, error=str(e))
        return False


async def get_all_states() -> list[dict]:
    """전체 서버 서킷브레이커 상태 조회."""
    db_url = _db_url()
    if not db_url:
        return []
    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            rows = await conn.fetch(
                """
                SELECT server, state, failure_count,
                       last_failure_at::text, cooldown_until::text,
                       opened_at::text, updated_at::text
                FROM circuit_breaker_state
                ORDER BY server
                """
            )
            return [dict(r) for r in rows]
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("circuit_breaker_get_all_failed", error=str(e))
        return []


async def _send_alert(server: str, failure_count: int) -> None:
    """서킷브레이커 OPEN 텔레그램 알림 (graceful degradation)."""
    try:
        from app.services.ceo_notify import send_telegram
        await send_telegram(
            f"🔴 서킷브레이커 OPEN: 서버 {server} | 연속 실패 {failure_count}회 → 작업 투입 {COOLDOWN_MINUTES}분 중단"
        )
    except Exception:
        pass
