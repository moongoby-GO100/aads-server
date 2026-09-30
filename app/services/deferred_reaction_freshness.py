"""지연 배달 직전, 본문이 가리키는 러너 job 의 현재 상태로 본문을 재검증한다.

next_step_proposals 는 제안 본문을 문자열로 확정해 chat_deferred_reactions 에 넣고,
배달은 나중에 그 문자열을 그대로 세션에 넣는다. 그 사이 job 이 종결돼도 본문은
"승인하세요" 로 남는다(2026-10-01 같은 세션에서 두 번 재현).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_JOB_ID_RE = re.compile(r"runner-[0-9a-f]{6,}")
_TERMINAL = {"done", "error", "rejected_done", "cancelled"}
_KST = ZoneInfo("Asia/Seoul")


def extract_job_ids(message: str) -> list[str]:
    """본문의 runner job id 를 중복 없이 등장 순서대로 돌려준다."""
    return list(dict.fromkeys(_JOB_ID_RE.findall(message or "")))


def _format_kst(value: Optional[datetime]) -> str:
    if not isinstance(value, datetime):
        return "시각 미상"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_KST).strftime("%m-%d %H:%M") + " KST"


async def annotate_or_skip(conn, message: str) -> tuple[str, Optional[str]]:
    """(본문, 건너뛸 사유) 를 돌려준다. 어떤 예외도 밖으로 던지지 않는다.

    - job id 없음 / 조회 실패: (원본, None)
    - 가리키는 job 이 전부 종결: (원본, "stale_jobs: ...")
    - 그 외: (상태 갱신 블록을 덧붙인 본문, None)
    """
    try:
        job_ids = extract_job_ids(message)
        if not job_ids:
            return message, None

        rows = await conn.fetch(
            "SELECT job_id, status, updated_at FROM pipeline_jobs "
            "WHERE job_id = ANY($1::text[])",
            job_ids,
        )
        found = {r["job_id"]: r for r in rows}
        statuses = {
            jid: (found[jid]["status"] if jid in found else "unknown")
            for jid in job_ids
        }

        if all(s in _TERMINAL for s in statuses.values()):
            reason = "stale_jobs: " + ", ".join(
                f"{jid}={statuses[jid]}" for jid in job_ids
            )
            return message, reason

        lines = []
        for jid in job_ids:
            if jid in found:
                lines.append(
                    f"- {jid}: {statuses[jid]} ({_format_kst(found[jid]['updated_at'])})"
                )
            else:
                lines.append(f"- {jid}: unknown")
        block = "\n\n[상태 갱신 — 배달 시각 기준]\n" + "\n".join(lines)
        return message + block, None
    except Exception as exc:  # noqa: BLE001 — 신선도 검사가 배달을 막으면 안 된다
        logger.warning(
            "deferred_reaction_freshness_failed error=%s", str(exc)[:160]
        )
        return message, None
