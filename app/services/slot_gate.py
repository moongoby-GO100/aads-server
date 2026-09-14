"""최후 수단 OAuth 슬롯의 수동 스위치.

2026-09-15 대표님 지시: "내가 슬롯3을 켜면 사용가능한거지 그렇게 적용해줘".

슬롯 3(진아 계정)은 AADS 소유가 아니다. 쓰이면 남의 주간 한도가 줄어든다.
순서를 최하로 두는 것만으로는 부족하다 — 슬롯 1·2 가 동시에 막히는 날이
오면 아무도 켜지 않았는데 진아 한도가 새어나간다. 그래서 **기본은 꺼짐**,
대표님이 켠 동안에만 폴백 후보가 된다.

## 설계

- 저장소는 `system_memory (category='oauth_slot', key='slot{N}_enabled')` 다.
  새 테이블을 만들지 않는다 — 켜짐/꺼짐 한 줄을 위해 스키마를 늘릴 이유가 없다.
- **기본값은 꺼짐이고, 조회 실패도 꺼짐이다.** DB 를 못 읽었을 때 켜진 것으로
  보면, 장애 순간에 남의 계정을 쓰기 시작한다. 모르면 안 쓰는 쪽이 맞다.
- 최후 수단이 아닌 슬롯(1·2)은 이 스위치의 대상이 아니다. 항상 켜짐이다.
- 캐시는 짧게 둔다(10초). 대표님이 끄면 바로 멈춰야 한다.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import structlog

from app.core.auth_provider import LAST_RESORT_SLOTS

logger = structlog.get_logger(__name__)

_CATEGORY = "oauth_slot"
_CACHE_TTL_SEC = 10.0

# slot -> (value, fetched_at)
_cache: Dict[str, Tuple[bool, float]] = {}


def _key(slot: str) -> str:
    return "slot%s_enabled" % slot


def invalidate(slot: Optional[str] = None) -> None:
    if slot is None:
        _cache.clear()
    else:
        _cache.pop(str(slot), None)


async def is_enabled(slot: str) -> bool:
    """이 슬롯을 지금 써도 되나."""
    slot = str(slot or "")
    if slot not in LAST_RESORT_SLOTS:
        return True

    hit = _cache.get(slot)
    now = time.time()
    if hit and now - hit[1] < _CACHE_TTL_SEC:
        return hit[0]

    enabled = False
    try:
        from app.core.db_pool import get_pool

        row = await get_pool().fetchval(
            "SELECT value FROM system_memory WHERE category = $1 AND key = $2",
            _CATEGORY, _key(slot),
        )
        if isinstance(row, dict):
            enabled = bool(row.get("enabled"))
        elif row is not None:
            import json as _json

            enabled = bool(_json.loads(row).get("enabled"))
    except Exception as exc:
        # 못 읽으면 꺼진 것으로 본다. 장애 순간에 남의 한도를 쓰기 시작하는
        # 것보다, 폴백 하나가 줄어드는 편이 낫다.
        logger.warning("slot_gate_read_failed slot=%s error=%s", slot, str(exc)[:160])
        return False

    _cache[slot] = (enabled, now)
    return enabled


async def state(slot: str) -> Dict[str, Any]:
    """화면에 보여줄 스위치 상태 (누가 언제 켰나 포함)."""
    slot = str(slot or "")
    if slot not in LAST_RESORT_SLOTS:
        return {"slot": slot, "gated": False, "enabled": True}
    try:
        from app.core.db_pool import get_pool

        row = await get_pool().fetchrow(
            "SELECT value, updated_at, updated_by FROM system_memory "
            "WHERE category = $1 AND key = $2",
            _CATEGORY, _key(slot),
        )
    except Exception as exc:
        logger.warning("slot_gate_state_failed slot=%s error=%s", slot, str(exc)[:160])
        return {"slot": slot, "gated": True, "enabled": False}

    value = (row["value"] if row else None) or {}
    if not isinstance(value, dict):
        import json as _json

        try:
            value = _json.loads(value)
        except Exception:
            value = {}
    return {
        "slot": slot,
        "gated": True,
        "enabled": bool(value.get("enabled")),
        "changed_at": row["updated_at"].isoformat() if row and row["updated_at"] else None,
        "changed_by": (row["updated_by"] if row else None) or "",
    }


# Claude Code OAuth 토큰은 이 시스템 프롬프트가 첫 블록으로 있어야 /v1/messages
# 를 받아준다. 없으면 403 이다.
_OAUTH_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


async def probe_usage(slot: str) -> Dict[str, Any]:
    """그 계정의 한도를 **실제로 물어본다**.

    Anthropic 은 잔량 조회 API 를 주지 않는다. 유일한 출처가 응답 헤더
    (`anthropic-ratelimit-unified-*`)라서, 한 번도 안 쓴 계정은 잴 값 자체가
    없다. 그래서 `max_tokens=1` 짜리 최소 호출을 한 번 보내 헤더만 받는다.

    **남의 계정을 건드리는 호출이다.** 대표님이 스위치를 켜는 순간처럼
    의도가 분명할 때만 부른다 — 주기적으로 돌리지 않는다.
    """
    # `llm_key_provider` 의 원본 레코드에는 slot 이 없다 — 슬롯은
    # `auth_provider._assign_slots` 가 키 이름으로 붙인다. 원본을 보면
    # slot 이 항상 None 이라 무조건 token_missing 이 된다(실제로 그랬다).
    from app.core.auth_provider import get_oauth_key_records_async

    records = await get_oauth_key_records_async(include_rate_limited=True)
    token = ""
    label = ""
    for record in records:
        if str(record.get("slot", "")) == str(slot):
            token = record.get("value", "") or ""
            label = record.get("label", "") or record.get("key_name", "") or ""
            break
    if not token:
        return {"ok": False, "error": "token_missing", "slot": slot}

    import httpx

    headers = {
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "system": [{"type": "text", "text": _OAUTH_SYSTEM}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages", headers=headers, json=body)
    except Exception as exc:
        logger.warning("slot_probe_failed slot=%s error=%s", slot, str(exc)[:160])
        return {"ok": False, "error": "request_failed", "slot": slot}

    def _num(name: str):
        raw = resp.headers.get("anthropic-ratelimit-unified-%s" % name)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    five_util, seven_util = _num("5h-utilization"), _num("7d-utilization")
    if five_util is None and seven_util is None:
        # 401/403 이면 토큰이 죽은 것이다. 응답 본문은 찍지 않는다.
        logger.warning("slot_probe_no_headers slot=%s status=%s", slot, resp.status_code)
        return {"ok": False, "error": "no_rate_limit_headers",
                "status": resp.status_code, "slot": slot}

    info = {
        "unifiedWindows": {
            "five_hour": {"utilization": five_util, "resetsAt": _num("5h-reset")},
            "seven_day": {"utilization": seven_util, "resetsAt": _num("7d-reset")},
        },
        "source": "slot_gate_probe",
        "status": resp.headers.get("anthropic-ratelimit-unified-status", ""),
    }
    from app.services.oauth_usage_tracker import record_slot_rate_limit

    await record_slot_rate_limit(str(slot), label, info)
    logger.info("slot_probe_ok slot=%s 5h=%s 7d=%s", slot, five_util, seven_util)
    return {
        "ok": True, "slot": slot, "label": label,
        "five_hour_percent": None if five_util is None else round(five_util * 100, 1),
        "seven_day_percent": None if seven_util is None else round(seven_util * 100, 1),
    }


async def set_enabled(slot: str, enabled: bool, *, by: str = "CEO") -> Dict[str, Any]:
    """스위치를 돌린다. 켜고 끈 사실은 알림함에도 남긴다."""
    slot = str(slot or "")
    if slot not in LAST_RESORT_SLOTS:
        return {"ok": False, "error": "not_gated",
                "message": "슬롯 %s 는 수동 스위치 대상이 아닙니다." % slot}

    from app.core.db_pool import get_pool

    await get_pool().execute(
        "INSERT INTO system_memory (category, key, value, updated_by, updated_at) "
        "VALUES ($1, $2, $3::jsonb, $4, now()) "
        "ON CONFLICT (category, key) DO UPDATE "
        "SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()",
        _CATEGORY, _key(slot),
        '{"enabled": %s}' % ("true" if enabled else "false"),
        by[:100],
    )
    invalidate(slot)

    # 켜고 끈 것 자체가 기록으로 남아야 한다. 나중에 "언제부터 진아 한도를
    # 썼나" 를 묻게 되는데, 그때 답이 없으면 아무도 설명하지 못한다.
    try:
        from app.services import ohvis_alert

        await ohvis_alert.notify(
            "최후 수단 슬롯 %s %s" % (slot, "켜짐" if enabled else "꺼짐"),
            "슬롯 %s 를 %s. 켜져 있는 동안 AADS 슬롯 1·2 가 모두 불가능하면 "
            "이 계정으로 응답합니다." % (slot, "사용 가능으로 바꿨습니다" if enabled else "사용 불가로 되돌렸습니다"),
            severity=ohvis_alert.WARNING if enabled else ohvis_alert.INFO,
            category="oauth_slot",
            project="AADS",
        )
    except Exception as exc:
        logger.warning("slot_gate_alert_failed slot=%s error=%s", slot, str(exc)[:120])

    logger.info("slot_gate_set slot=%s enabled=%s by=%s", slot, enabled, by)

    # 켠 직후에 한 번 재본다. 켜 두고 잔량을 모르면 스위치가 반쪽이다 —
    # 끌 때는 재지 않는다. 안 쓰기로 한 계정을 굳이 또 건드릴 이유가 없다.
    probe: Dict[str, Any] = {}
    if enabled:
        try:
            probe = await probe_usage(slot)
        except Exception as exc:
            logger.warning("slot_gate_probe_failed slot=%s error=%s", slot, str(exc)[:120])
    return {"ok": True, "slot": slot, "enabled": enabled, "probe": probe}
