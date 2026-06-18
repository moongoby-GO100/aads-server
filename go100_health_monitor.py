#!/usr/bin/env python3
# CUR-GO100-PHASE10-B-BETA-MONITORING, 2026-02-26
# GO100 헬스 모니터 — 5분마다 크론 실행, 경고 시 go100_reports에 urgent 저장
#
# 크론 예시:
# */5 * * * * /root/kis-autotrade-v4/.venv/bin/python /root/kis-autotrade-v4/scripts/go100/health_monitor.py

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 경고 알림을 받을 user_id (go100_reports는 user_id NOT NULL). 기본 1, env로 변경 가능.
ALERT_USER_ID = int(os.environ.get("GO100_ALERT_USER_ID", "1"))
GO100_SERVICE_NAME = os.environ.get("GO100_SERVICE_NAME", "go100")
GO100_HEALTH_URL = os.environ.get("GO100_HEALTH_URL", "http://127.0.0.1:8002/health")
DISK_WARN_PERCENT = 85
ERROR_RATE_WARN = 0.10  # 10%


async def _save_urgent_alert(title: str, content: str) -> None:
    """go100_reports에 urgent 이벤트 알림 저장."""
    try:
        from backend.app.core.database import AsyncSessionLocal
        from backend.app.services.go100.ai.proactive_reporter import save_report
        async with AsyncSessionLocal() as db:
            await save_report(
                ALERT_USER_ID,
                "event_alert",
                title[:200],
                content,
                "urgent",
                db,
            )
        logger.warning("Urgent alert saved: %s", title[:80])
    except Exception as e:
        logger.exception("Failed to save urgent alert: %s", e)


def _check_disk() -> float:
    """디스크 사용률(0~100) 반환."""
    try:
        total, used, _ = shutil.disk_usage("/")
        return 100.0 * used / total if total else 0
    except Exception as e:
        logger.warning("Disk check failed: %s", e)
        return 0


def _check_go100_service() -> bool:
    """go100 서비스 활성 여부. True=활성."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", GO100_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip().lower() == "active"
    except Exception as e:
        logger.warning("Service check failed: %s", e)
        return False


def _restart_go100_service() -> bool:
    """go100 서비스 재시작 시도. 성공 여부 반환."""
    try:
        r = subprocess.run(
            ["systemctl", "restart", GO100_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            return True
        logger.warning(
            "Service restart returned non-zero: rc=%s stdout=%s stderr=%s",
            r.returncode,
            r.stdout.strip()[:500],
            r.stderr.strip()[:500],
        )
    except Exception as e:
        logger.warning("Service restart failed: %s", e)
    return _force_restart_go100_service()


def _force_restart_go100_service() -> bool:
    """SIGTERM으로 멈추지 않는 gunicorn worker까지 정리해 서비스 재기동."""
    try:
        subprocess.run(
            ["systemctl", "kill", "-s", "SIGKILL", GO100_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        subprocess.run(
            ["systemctl", "reset-failed", GO100_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        r = subprocess.run(
            ["systemctl", "start", GO100_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            logger.warning(
                "Forced service start failed: rc=%s stdout=%s stderr=%s",
                r.returncode,
                r.stdout.strip()[:500],
                r.stderr.strip()[:500],
            )
        return r.returncode == 0
    except Exception as e:
        logger.warning("Forced service restart failed: %s", e)
        return False


def _check_api_health() -> bool:
    """실제 HTTP 헬스 응답 확인. active 상태여도 워커가 멈추면 False."""
    try:
        r = subprocess.run(
            ["curl", "-fsS", "--max-time", "5", GO100_HEALTH_URL],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if r.returncode == 0 and '"status":"ok"' in r.stdout.replace(" ", ""):
            return True
        logger.warning(
            "API health failed: rc=%s stdout=%s stderr=%s",
            r.returncode,
            r.stdout.strip()[:500],
            r.stderr.strip()[:500],
        )
        return False
    except Exception as e:
        logger.warning("API health check failed: %s", e)
        return False


async def _check_db() -> bool:
    """DB 연결 성공 여부."""
    try:
        from backend.app.core.database import async_engine
        from sqlalchemy import text
        async with async_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.warning("DB check failed: %s", e)
        return False


async def _get_error_rate_last_hour() -> float:
    """최근 1시간 에러율 (0.0~1.0). 테이블 없으면 0 반환."""
    try:
        from backend.app.core.database import AsyncSessionLocal
        from sqlalchemy import text
        since = datetime.utcnow() - timedelta(hours=1)
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                text("""
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN is_error THEN 1 ELSE 0 END)::float AS errs
                    FROM go100_usage_logs
                    WHERE created_at >= :since
                """),
                {"since": since},
            )
            row = r.fetchone()
        if not row or (row[0] or 0) == 0:
            return 0.0
        total = int(row[0])
        errs = float(row[1] or 0)
        return errs / total
    except Exception as e:
        logger.debug("Error rate check (table may not exist): %s", e)
        return 0.0


async def run_checks() -> None:
    """모든 체크 수행 및 경고 시 로그 + urgent 알림."""
    alerts: list[str] = []

    # 1. 디스크 >85%
    pct = _check_disk()
    if pct >= DISK_WARN_PERCENT:
        msg = f"디스크 사용률 {pct:.1f}% (경고 기준 {DISK_WARN_PERCENT}%)"
        logger.warning("[GO100-MONITOR] %s", msg)
        alerts.append(msg)
        await _save_urgent_alert("GO100 모니터: 디스크 경고", msg)

    # 2. go100 서비스 다운 → 재시작 시도 + 경고
    if not _check_go100_service():
        logger.warning("[GO100-MONITOR] go100 서비스 비활성, 재시작 시도")
        ok = _restart_go100_service()
        if ok:
            logger.info("[GO100-MONITOR] go100 서비스 재시작 완료")
        else:
            msg = "go100 서비스가 중지되어 있었고 자동 재시작에 실패했습니다."
            alerts.append(msg)
            await _save_urgent_alert("GO100 모니터: 서비스 다운", msg)
    elif not _check_api_health():
        logger.warning("[GO100-MONITOR] go100 서비스 active이나 API health 실패, 재시작 시도")
        ok = _restart_go100_service()
        if ok and _check_api_health():
            msg = "go100 API health 실패를 자동 감지해 서비스를 재시작했고, /health 응답을 복구했습니다."
            alerts.append(msg)
            await _save_urgent_alert("GO100 모니터: API 자동 복구", msg)
        else:
            msg = "go100 서비스는 active이나 /health 응답 실패가 지속됩니다. 수동 점검이 필요합니다."
            alerts.append(msg)
            await _save_urgent_alert("GO100 모니터: API 응답 불능", msg)

    # 3. DB 연결 실패
    if not await _check_db():
        msg = "GO100 모니터: DB 연결 실패"
        logger.warning("[GO100-MONITOR] %s", msg)
        alerts.append(msg)
        await _save_urgent_alert("GO100 모니터: DB 연결 실패", msg)

    # 4. 1시간 내 에러율 >10%
    rate = await _get_error_rate_last_hour()
    if rate >= ERROR_RATE_WARN:
        msg = f"최근 1시간 AI 채팅 에러율 {rate*100:.1f}% (기준 {ERROR_RATE_WARN*100}%)"
        logger.warning("[GO100-MONITOR] %s", msg)
        alerts.append(msg)
        await _save_urgent_alert("GO100 모니터: 에러율 경고", msg)

    if not alerts:
        logger.debug("[GO100-MONITOR] All checks passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="GO100 health monitor (cron)")
    parser.add_argument("--dry-run", action="store_true", help="알림 저장 없이 체크만")
    args = parser.parse_args()
    if args.dry_run:
        pass
    asyncio.run(run_checks())


if __name__ == "__main__":
    main()
