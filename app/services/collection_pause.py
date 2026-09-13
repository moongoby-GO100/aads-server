"""수집 작업을 배포 없이 즉시 멈추는 스위치.

2026-09-13 — 배달 수집을 잠시 멈추려 했는데 런타임 스위치가 없었다. 코드에
`_run_delivery_auto_collect` 가 스케줄로 돌 뿐, 켜고 끄는 자리가 어디에도
없어 멈추려면 배포가 필요했다.

수집이 잘못 돌고 있다는 것을 알았을 때 **즉시 멈출 수 없다**는 게 문제다.
최근 30일 배달 수집 실측이 그 상황이었다.

    COLLECTION_ALREADY_RUNNING   1,576건  (중복 실행)
    PC_AGENT_*_REQUIRED          1,462건  (세션 없음)
    가짜 성공(html_text)          136건   (파싱 없이 원문 저장)

잘못된 수집이 계속 돌면 쓰레기 데이터가 쌓이고, 그 사이 다른 작업까지
중복 차단으로 막힌다. 끄는 데 배포 20분이 걸리면 안 된다.

DB 한 줄로 제어한다. 앱 재시작도, 배포도 필요 없다.

    카테고리  ops
    키        collection_paused
    값        {"delivery": true, "reason": "...", "paused_at": "...", "by": "..."}

읽기 실패는 "멈추지 않음"으로 처리한다. 스위치 조회가 안 된다고 정상 수집을
막으면 더 나쁘다.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

_CATEGORY = "ops"
_KEY = "collection_paused"
_CACHE_TTL_SEC = 20.0
_cache: dict[str, Any] = {"at": 0.0, "value": {}}


async def _load(force: bool = False) -> dict:
    """스위치 상태를 읽는다. 20초 캐시 — 루프마다 DB 를 때리지 않는다."""
    now = time.monotonic()
    if not force and (now - float(_cache["at"])) < _CACHE_TTL_SEC:
        return dict(_cache["value"])
    try:
        from app.core.db_pool import get_pool

        raw = await get_pool().fetchval(
            "SELECT value FROM system_memory WHERE category=$1 AND key=$2",
            _CATEGORY, _KEY,
        )
    except Exception:
        # 조회 실패는 멈춤이 아니다. 스위치가 안 읽힌다고 정상 수집을 막으면
        # DB 일시 장애가 곧 수집 중단이 된다.
        return dict(_cache["value"])
    value: dict = {}
    if isinstance(raw, dict):
        value = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                value = parsed
        except Exception:
            value = {}
    _cache["at"] = now
    _cache["value"] = value
    return dict(value)


async def is_collection_paused(kind: str = "delivery") -> bool:
    """해당 수집이 멈춤 상태인지."""
    value = await _load()
    if bool(value.get("all")):
        return True
    return bool(value.get(str(kind)))


async def pause_reason(kind: str = "delivery") -> str:
    value = await _load()
    return str(value.get("reason") or "")


async def set_collection_paused(
    kind: str = "delivery",
    paused: bool = True,
    *,
    reason: str = "",
    by: str = "",
) -> dict:
    """스위치를 켜거나 끈다. 누가 왜 멈췄는지 같이 남긴다.

    이유가 없으면 다음 사람이 켜도 되는지 판단할 수 없다. 멈춘 채로 잊히는
    것이 계속 도는 것만큼 나쁘다.
    """
    from app.core.db_pool import get_pool

    value = await _load(force=True)
    value[str(kind)] = bool(paused)
    value["reason"] = reason or value.get("reason") or ""
    value["by"] = by or value.get("by") or ""
    value["updated_at"] = time.strftime("%FT%T+09:00")
    if paused:
        value["paused_at"] = value["updated_at"]
    await get_pool().execute(
        """
        INSERT INTO system_memory (category, key, value, updated_by)
        VALUES ($1, $2, $3::jsonb, 'collection_pause')
        ON CONFLICT (category, key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = NOW(), updated_by = 'collection_pause'
        """,
        _CATEGORY, _KEY, json.dumps(value, ensure_ascii=False),
    )
    _cache["at"] = 0.0  # 다음 조회에서 즉시 반영
    return value
