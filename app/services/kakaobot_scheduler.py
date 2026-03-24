"""KakaoBot 예약 발송 스케줄러 — 매분 체크 + 매일 새벽 기념일 스케줄 생성."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


async def check_and_send_scheduled() -> int:
    """예약 발송 체크 — pending 상태이고 scheduled_at이 지난 건 발송.

    Returns:
        발송 처리한 건수
    """
    from app.core.db_pool import get_pool
    from app.services.aligo_client import send_sms, is_available

    pool = get_pool()
    if pool is None:
        return 0

    if not is_available():
        logger.debug("kakaobot_scheduler: 알리고 미설정 — 스킵")
        return 0

    sent_count = 0

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT s.id, s.message, s.contact_id, s.user_id,
                          c.phone, c.name
                   FROM kakaobot_scheduled s
                   JOIN kakaobot_contacts c ON c.id = s.contact_id
                   WHERE s.status = 'pending'
                     AND s.scheduled_at <= NOW()
                   ORDER BY s.scheduled_at ASC
                   LIMIT 20""",
            )

            for row in rows:
                try:
                    result = await send_sms(
                        receiver=row["phone"],
                        msg=row["message"],
                    )
                    result_code = int(result.get("result_code", -999))
                    success = result_code == 1

                    await conn.execute(
                        """UPDATE kakaobot_scheduled
                           SET status = $1,
                               sent_at = NOW(),
                               send_result = $2::jsonb,
                               updated_at = NOW()
                           WHERE id = $3""",
                        "sent" if success else "failed",
                        __import__("json").dumps(result, ensure_ascii=False),
                        row["id"],
                    )

                    if success:
                        sent_count += 1
                        logger.info(
                            "kakaobot_scheduled_sent: id=%d to=%s(%s)",
                            row["id"], row["name"], row["phone"],
                        )
                    else:
                        logger.warning(
                            "kakaobot_scheduled_failed: id=%d code=%d",
                            row["id"], result_code,
                        )
                except Exception as e:
                    logger.error("kakaobot_scheduled_error: id=%d err=%s", row["id"], e)
                    await conn.execute(
                        """UPDATE kakaobot_scheduled
                           SET retry_count = retry_count + 1, updated_at = NOW()
                           WHERE id = $1""",
                        row["id"],
                    )
    except Exception as e:
        logger.error("kakaobot_scheduler check_and_send 실패: %s", e)

    return sent_count


async def generate_anniversary_schedules() -> int:
    """기념일 기반 예약 발송 자동 생성 — auto_send=True인 기념일 대상.

    음력 변환은 korean_lunar_calendar 패키지가 있으면 사용, 없으면 스킵.

    Returns:
        생성된 예약 건수
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    if pool is None:
        return 0

    created = 0
    today = date.today()

    try:
        async with pool.acquire() as conn:
            # auto_send=True인 기념일 조회
            rows = await conn.fetch(
                """SELECT a.id, a.contact_id, a.user_id, a.title, a.date,
                          a.is_lunar, a.remind_days_before, a.template_id,
                          a.custom_message, a.recurrence,
                          c.name AS contact_name
                   FROM kakaobot_anniversaries a
                   JOIN kakaobot_contacts c ON c.id = a.contact_id
                   WHERE a.auto_send = TRUE""",
            )

            for row in rows:
                target_date = _resolve_anniversary_date(row["date"], row["is_lunar"], today.year)
                if target_date is None:
                    continue

                send_date = target_date - timedelta(days=row["remind_days_before"])
                if send_date < today or send_date > today + timedelta(days=1):
                    continue

                # 이미 같은 기념일에 대해 올해 예약이 있는지 확인
                existing = await conn.fetchval(
                    """SELECT COUNT(*) FROM kakaobot_scheduled
                       WHERE anniversary_id = $1
                         AND scheduled_at::date = $2
                         AND status != 'cancelled'""",
                    row["id"], send_date,
                )
                if existing and existing > 0:
                    continue

                # 메시지 결정: custom_message > template > AI 생성
                message = row["custom_message"] or ""
                if not message and row["template_id"]:
                    tpl = await conn.fetchrow(
                        "SELECT content FROM kakaobot_templates WHERE id = $1",
                        row["template_id"],
                    )
                    if tpl:
                        message = tpl["content"].replace("{name}", row["contact_name"]).replace("{occasion}", row["title"])

                if not message:
                    # AI 생성
                    try:
                        from app.services.kakaobot_ai import generate_messages
                        candidates = await generate_messages(
                            occasion=row["title"],
                            recipient_name=row["contact_name"],
                            count=1,
                        )
                        if candidates:
                            message = candidates[0]
                    except Exception as e:
                        logger.warning("kakaobot_anniv_ai_generate 실패: %s", e)

                if not message:
                    message = f"{row['contact_name']}님, {row['title']} 축하드립니다! 🎉"

                # 예약 생성 (오전 9시에 발송)
                scheduled_at = datetime.combine(send_date, datetime.min.time().replace(hour=9))
                await conn.execute(
                    """INSERT INTO kakaobot_scheduled
                       (user_id, contact_id, anniversary_id, template_id, message, scheduled_at, status)
                       VALUES ($1, $2, $3, $4, $5, $6, 'pending')""",
                    row["user_id"], row["contact_id"], row["id"],
                    row["template_id"], message, scheduled_at,
                )
                created += 1
                logger.info(
                    "kakaobot_anniv_schedule_created: anniv=%d contact=%s date=%s",
                    row["id"], row["contact_name"], send_date,
                )
    except Exception as e:
        logger.error("kakaobot_scheduler generate_anniversary 실패: %s", e)

    return created


def _resolve_anniversary_date(
    base_date: date, is_lunar: bool, year: int
) -> Optional[date]:
    """기념일 날짜를 올해 양력으로 변환."""
    if not is_lunar:
        return base_date.replace(year=year)

    # 음력 → 양력 변환 시도
    try:
        from korean_lunar_calendar import KoreanLunarCalendar
        cal = KoreanLunarCalendar()
        cal.setLunarDate(year, base_date.month, base_date.day, False)
        sol = cal.SolarIsoFormat().split("-")
        return date(int(sol[0]), int(sol[1]), int(sol[2]))
    except ImportError:
        logger.debug("korean_lunar_calendar 미설치 — 음력 변환 스킵")
        return base_date.replace(year=year)
    except Exception as e:
        logger.warning("음력 변환 실패: %s — 양력으로 대체", e)
        return base_date.replace(year=year)


async def _scheduler_loop_send():
    """매분 예약 발송 체크 루프."""
    while True:
        try:
            count = await check_and_send_scheduled()
            if count > 0:
                logger.info("kakaobot_scheduler_sent: %d건", count)
        except Exception as e:
            logger.error("kakaobot_scheduler_loop_error: %s", e)
        await asyncio.sleep(60)


async def _scheduler_loop_anniversary():
    """매일 새벽 2시 기념일 스케줄 생성 루프."""
    while True:
        try:
            now = datetime.now()
            # 다음 새벽 2시까지 대기
            target = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_secs = (target - now).total_seconds()
            await asyncio.sleep(wait_secs)

            count = await generate_anniversary_schedules()
            logger.info("kakaobot_anniversary_generated: %d건", count)
        except Exception as e:
            logger.error("kakaobot_anniversary_loop_error: %s", e)
            await asyncio.sleep(3600)  # 에러 시 1시간 대기


def start_scheduler_tasks():
    """스케줄러 태스크 시작 — main.py lifespan에서 호출."""
    asyncio.create_task(_scheduler_loop_send())
    asyncio.create_task(_scheduler_loop_anniversary())
    logger.info("kakaobot_scheduler_started: send_loop + anniversary_loop")
