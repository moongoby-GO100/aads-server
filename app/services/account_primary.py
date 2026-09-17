"""주계정을 누가 정하는가 — 수동(대표님이 고른 계정) / 자동(규칙).

2026-09-17 대표님 지시.

  "수동일때고 자동일때는 계정 한도남은것중 갱신일이 빠른걸 우선 사용하게 해줘"
  "'곧 리셋될 한도부터 태운다' 가 맞아"

곧 리셋될 한도는 안 쓰면 그냥 사라진다. 그러니 리셋이 임박한 계정부터 태우고
리셋이 먼 계정은 아껴 둔다. 그래서 정렬 기준이 잔량이 아니라 **갱신 시각**이다.

저장 구조를 새로 만들지 않는다. 선택 경로(릴레이 current · 채팅 슬롯 순서 ·
코덱스 state.json)가 전부 이미 `llm_api_keys.priority` 를 보므로, 여기서는
priority 를 다시 매기는 것으로 끝낸다. 화면의 "주계정" 표시도 같은 값을 본다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

_CATEGORY = "oauth_slot"
PROVIDERS = ("anthropic", "codex")
AUTO = "auto"
MANUAL = "manual"

# 잔량이 이만큼 아래로 내려가면 "한도 남음" 으로 보지 않는다. 0% 로 두면 99.9%
# 쓴 계정을 주계정으로 올려놓고 다음 호출에서 바로 429 를 맞는다.
_MIN_HEADROOM_PCT = 2.0


def _mode_key(provider: str) -> str:
    return "%s_primary_mode" % provider


async def get_mode(provider: str) -> str:
    """기본은 수동이다. 못 읽으면 수동으로 본다 — 자동이 조용히 계정을 바꾸는
    것보다, 대표님이 고른 것이 그대로 있는 편이 안전하다."""
    try:
        from app.core.db_pool import get_pool

        raw = await get_pool().fetchval(
            "SELECT value FROM system_memory WHERE category = $1 AND key = $2",
            _CATEGORY, _mode_key(provider),
        )
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict) and str(raw.get("mode")) == AUTO:
            return AUTO
    except Exception as exc:
        logger.warning("account_primary_mode_read_failed",
                       provider=provider, error=str(exc)[:160])
    return MANUAL


async def set_mode(provider: str, mode: str, by: str = "CEO") -> None:
    from app.core.db_pool import get_pool

    value = AUTO if mode == AUTO else MANUAL
    payload = json.dumps({"mode": value, "by": by,
                          "at": datetime.now(timezone.utc).isoformat()})
    await get_pool().execute(
        """
        INSERT INTO system_memory (category, key, value)
        VALUES ($1, $2, $3::jsonb)
        ON CONFLICT (category, key) DO UPDATE SET value = EXCLUDED.value
        """,
        _CATEGORY, _mode_key(provider), payload,
    )
    logger.info("account_primary_mode_set", provider=provider, mode=value, by=by)


def _earliest_reset(*pairs) -> Optional[datetime]:
    """(잔량%, 리셋시각) 쌍들 중 아직 여유가 있는 창의 가장 이른 리셋."""
    candidates = [at for headroom, at in pairs
                  if at is not None and headroom is not None and headroom > _MIN_HEADROOM_PCT]
    return min(candidates) if candidates else None


async def usage_view(provider: str) -> List[Dict[str, Any]]:
    """계정별 (key_name, 한도남음, 가장 이른 갱신시각). 정렬 판단의 원재료다."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []

    rows = await pool.fetch(
        "SELECT key_name, label, priority, is_active, rate_limited_until "
        "FROM llm_api_keys WHERE provider = $1 ORDER BY priority, id",
        provider,
    )

    snaps: Dict[str, Dict[str, Any]] = {}
    if provider == "codex":
        for r in await pool.fetch(
            "SELECT key_name, used_percent, resets_at FROM codex_usage_snapshots"
        ):
            snaps[r["key_name"]] = {
                "headroom": 100.0 - float(r["used_percent"] or 0),
                "reset": r["resets_at"],
            }
    else:
        # 클로드는 5시간 창과 주간 창을 따로 쓴다. 둘 중 여유가 있는 창의
        # 이른 리셋을 본다 — 이미 다 쓴 창의 리셋은 태울 한도가 없다.
        slot_of = await _anthropic_slot_map()
        for r in await pool.fetch(
            """
            SELECT DISTINCT ON (account_slot) account_slot,
                   five_hour_utilization, five_hour_resets_at,
                   seven_day_utilization, seven_day_resets_at
            FROM claude_max_usage_snapshot
            WHERE account_slot <> ''
            ORDER BY account_slot, fetched_at DESC
            """
        ):
            key_name = slot_of.get(str(r["account_slot"]))
            if not key_name:
                continue
            five = 100.0 - float(r["five_hour_utilization"] or 0)
            week = 100.0 - float(r["seven_day_utilization"] or 0)
            snaps[key_name] = {
                "headroom": min(five, week),
                "reset": _earliest_reset((five, r["five_hour_resets_at"]),
                                         (week, r["seven_day_resets_at"])),
            }

    for row in rows:
        snap = snaps.get(row["key_name"], {})
        limited_until = row["rate_limited_until"]
        headroom = snap.get("headroom")
        has_quota = bool(row["is_active"]) and not (limited_until and limited_until > now)
        if has_quota and headroom is not None and headroom <= _MIN_HEADROOM_PCT:
            has_quota = False
        out.append({
            "key_name": row["key_name"],
            "label": row["label"] or row["key_name"],
            "priority": row["priority"],
            "is_active": bool(row["is_active"]),
            "rate_limited_until": limited_until,
            "headroom_pct": headroom,
            "resets_at": snap.get("reset"),
            "has_quota": has_quota,
        })
    return out


async def _anthropic_slot_map() -> Dict[str, str]:
    """slot -> key_name. 표시용 순번이 아니라 이름 기준 매핑을 쓴다."""
    from app.core.auth_provider import get_oauth_key_records_async

    out: Dict[str, str] = {}
    try:
        for record in await get_oauth_key_records_async(include_rate_limited=True):
            slot = str(record.get("slot", "") or "")
            if slot:
                out[slot] = record.get("key_name", "")
    except Exception as exc:
        logger.warning("anthropic_slot_map_failed", error=str(exc)[:160])
    return out


def auto_order(accounts: List[Dict[str, Any]]) -> List[str]:
    """곧 리셋될 한도부터 태운다.

      1) 한도 남은 계정 먼저
      2) 그중 갱신 시각이 이른 것 먼저 (갱신 정보가 없으면 뒤로)
      3) 동률이면 잔량 많은 것, 그래도 동률이면 기존 우선순위
    """
    far_future = datetime.max.replace(tzinfo=timezone.utc)

    def key(a: Dict[str, Any]):
        return (
            0 if a["has_quota"] else 1,
            a["resets_at"] or far_future,
            -(a["headroom_pct"] if a["headroom_pct"] is not None else -1),
            a["priority"],
        )

    return [a["key_name"] for a in sorted(accounts, key=key)]


async def apply_order(provider: str, ordered: List[str]) -> List[str]:
    """priority 를 1..N 으로 다시 매긴다. 이것이 모든 선택 경로의 입력이다."""
    from app.core.db_pool import get_pool
    from app.core.llm_key_provider import invalidate_key_cache

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for index, key_name in enumerate(ordered, start=1):
                await conn.execute(
                    "UPDATE llm_api_keys SET priority = $2, updated_at = NOW() "
                    "WHERE key_name = $1 AND provider = $3",
                    key_name, index, provider,
                )
    try:
        invalidate_key_cache()
    except Exception:
        pass
    logger.info("account_primary_order_applied", provider=provider, order=ordered)
    return ordered


async def reconcile(provider: str) -> Dict[str, Any]:
    """자동 모드면 규칙대로 다시 세운다. 수동이면 손대지 않는다."""
    mode = await get_mode(provider)
    accounts = await usage_view(provider)
    if mode != AUTO:
        return {"provider": provider, "mode": mode, "changed": False,
                "primary": accounts[0]["key_name"] if accounts else ""}

    ordered = auto_order(accounts)
    current = [a["key_name"] for a in accounts]
    if ordered == current:
        return {"provider": provider, "mode": mode, "changed": False,
                "primary": ordered[0] if ordered else ""}

    await apply_order(provider, ordered)
    return {"provider": provider, "mode": mode, "changed": True,
            "primary": ordered[0] if ordered else "", "order": ordered}


async def set_manual(provider: str, key_name: str, by: str = "CEO") -> Dict[str, Any]:
    """대표님이 고른 계정을 1순위로 박고 수동으로 잠근다."""
    accounts = await usage_view(provider)
    names = [a["key_name"] for a in accounts]
    if key_name not in names:
        return {"ok": False, "error": "unknown_account"}
    ordered = [key_name] + [n for n in names if n != key_name]
    await set_mode(provider, MANUAL, by=by)
    await apply_order(provider, ordered)
    return {"ok": True, "provider": provider, "mode": MANUAL,
            "primary": key_name, "order": ordered}
