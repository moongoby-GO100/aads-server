"""오비스 알림 — 대표님께 알리는 단일 창구.

2026-09-14 대표님 지시: "텔레그램 알림 사용안해 그냥 오비스알림으로
보내줘".

알림함은 `alert_history` 다. 이미 5,222건이 쌓여 있고 화면
(`/projects/dashboard/alerts`)이 그것을 읽는다. **새 저장소를 만들지
않는다.**

## 왜 헬퍼를 두나

알리는 코드가 여기저기에서 각자 INSERT 하면 심각도·분류가 제각각이 되고,
나중에 "무엇을 알렸나" 를 한 곳에서 볼 수 없다. 오늘 하루 고친 것이
대부분 그런 종류였다 — 같은 목록이 네 벌, 서버 지도가 세 벌.
"""
from __future__ import annotations

from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

# 심각도. 화면이 색으로 구분한다.
CRITICAL = "critical"
WARNING = "warning"
INFO = "info"


async def notify(
    title: str,
    message: str = "",
    *,
    severity: str = INFO,
    category: str = "general",
    project: Optional[str] = None,
    server: Optional[str] = None,
    dedupe_minutes: int = 0,
) -> bool:
    """대표님 알림함에 남긴다.

    `dedupe_minutes` 를 주면 같은 제목이 그 시간 안에 이미 있으면 건너뛴다.
    같은 사실을 반복해 쌓으면 정작 중요한 것이 묻힌다 — 오늘 감시기가
    10만 번 같은 거짓 경보를 올려 진짜 고장 4건을 가렸다.

    **알림 실패가 부르는 쪽을 막지 않는다.** 알리려다 본 작업이 죽으면
    본말전도다.
    """
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        if dedupe_minutes > 0:
            dup = await pool.fetchval(
                "SELECT 1 FROM alert_history WHERE title = $1 "
                "  AND created_at > NOW() - ($2 || ' minutes')::interval LIMIT 1",
                title, str(dedupe_minutes),
            )
            if dup:
                return False

        await pool.execute(
            "INSERT INTO alert_history (severity, category, title, message, "
            "       project, server, acknowledged) "
            "VALUES ($1, $2, $3, $4, $5, $6, false)",
            severity, category, title, message or "", project, server,
        )
        logger.info(
            "ohvis_alert", title=title[:60], severity=severity, category=category
        )
        return True
    except Exception as exc:
        logger.warning("ohvis_alert_failed title=%s error=%s", title[:40], str(exc)[:160])
        return False
