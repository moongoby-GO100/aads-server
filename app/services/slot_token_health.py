"""슬롯 토큰이 살아 있나 — 죽기 전에 알기 위해.

2026-09-15 대표님 확인 요청: "진아 토큰 자동리프레시반영했잖아 우리 aads
반영하면서 안되었나?"

**안 되어 있다. 그리고 지금 구조로는 할 수 없다.** 근거는 이렇다.

    · 자동 갱신은 `scripts/claude_token_keeper.sh` 가 한다. 그것이 갱신하는
      것은 `/root/.claude-relay-slots/slot{1,2}/.claude/.credentials.json`
      인데, 이 파일에는 **리프레시 토큰**이 들어 있다.
    · 슬롯 3(진아)에는 그런 파일이 없다. 값이 `.env`/DB 에 **액세스 토큰만**
      들어 있다. 액세스 토큰은 스스로를 갱신하지 못한다.
    · 진아 서버(244)에도 없다 — `/root/.claude/.credentials.json` 부재,
      크론 0건, `/root/.claude-lease/slot{1,2}` 의 파일은 리프레시 토큰이
      없고 이미 만료됐다(2026-09-14 22:50).

리프레시 토큰을 얻는 길은 그 계정으로 `/login` 하는 것뿐이고, 그것은 진아
쪽에서만 할 수 있다.

## 그래서 대신 하는 일

죽는 것을 막을 수 없으면, **죽는 순간을 즉시 아는** 것이 다음으로 좋다.
지금은 담당이 실패한 뒤에야 알았다.

토큰 만료 시각은 값만 봐서는 알 수 없다(불투명 문자열). 그래서 주기적으로
**한도를 쓰지 않는 호출**(`GET /v1/models`)로 살아 있는지만 본다. 죽으면
그 즉시 오비스 알림을 올린다.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict

import structlog

logger = structlog.get_logger(__name__)

_CHECK_INTERVAL_SEC = int(os.getenv("SLOT_TOKEN_HEALTH_INTERVAL_SEC", "600"))

# slot -> {"alive": bool, "checked_at": epoch, "status": int}
_state: Dict[str, Dict[str, Any]] = {}


def snapshot() -> Dict[str, Dict[str, Any]]:
    """화면이 쓰는 마지막 확인 결과."""
    return {k: dict(v) for k, v in _state.items()}


async def check_slot(slot: str) -> Dict[str, Any]:
    """그 슬롯 토큰이 지금 살아 있나. **한도를 쓰지 않는다.**"""
    from app.core.auth_provider import get_oauth_key_records_async

    result: Dict[str, Any] = {"slot": slot, "alive": False, "status": 0,
                              "checked_at": time.time()}
    try:
        records = await get_oauth_key_records_async(include_rate_limited=True)
        token = ""
        label = ""
        for record in records:
            if str(record.get("slot", "")) == str(slot):
                token = record.get("value", "") or ""
                label = record.get("label", "") or record.get("key_name", "") or ""
                break
        if not token:
            result["error"] = "token_missing"
            _state[str(slot)] = result
            return result

        import httpx

        headers = {
            "Authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models?limit=1", headers=headers)
        result["status"] = resp.status_code
        result["alive"] = resp.status_code == 200
        result["label"] = label
    except Exception as exc:
        # 네트워크 문제와 토큰 죽음을 구분한다. 네트워크면 다음 회차에 다시 본다.
        result["error"] = str(exc)[:120]
        logger.warning("slot_token_check_failed slot=%s error=%s", slot, str(exc)[:140])
        previous = _state.get(str(slot))
        if previous:
            result["alive"] = bool(previous.get("alive"))
        _state[str(slot)] = result
        return result

    was_alive = bool((_state.get(str(slot)) or {}).get("alive", True))
    _state[str(slot)] = result

    if was_alive and not result["alive"]:
        # 살아 있다가 죽은 순간. 이 한 번만 알린다 — 10분마다 같은 알림이
        # 쌓이면 정작 중요한 것이 묻힌다.
        try:
            from app.services import ohvis_alert

            await ohvis_alert.notify(
                "슬롯 %s 토큰 만료 — %s" % (slot, label or "계정"),
                "이 계정으로는 더 이상 응답할 수 없습니다(HTTP %s). 리프레시 "
                "토큰이 없어 자동 갱신이 불가능합니다 — 계정 소유자에게 재발급을 "
                "요청해야 합니다." % result["status"],
                severity=ohvis_alert.CRITICAL,
                category="oauth_slot", project="AADS",
                dedupe_minutes=120,
            )
        except Exception:
            pass
    return result


async def slot_token_health_poller(interval_sec: int = 0) -> None:
    """살아 있는지만 주기적으로 본다. 한도를 쓰지 않으므로 자주 봐도 된다."""
    import asyncio

    from app.core.auth_provider import LAST_RESORT_SLOTS

    interval = interval_sec or _CHECK_INTERVAL_SEC
    while True:
        try:
            # 리프레시가 되는 슬롯(1·2)은 keeper 가 지킨다. 여기서는 스스로
            # 갱신하지 못하는 슬롯만 본다.
            for slot in sorted(LAST_RESORT_SLOTS):
                await check_slot(slot)
        except Exception as exc:
            logger.warning("slot_token_health_poller_error error=%s", str(exc)[:160])
        await asyncio.sleep(interval)
