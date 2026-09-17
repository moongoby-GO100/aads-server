"""슬롯별 프로젝트 배정 — 어느 프로젝트가 어느 계정을 쓰는가.

2026-09-15 대표님 지시: "슬롯에 복수의 프로젝트를 지정하고 지정된 프로젝트는
해당 슬롯을 우선 사용 하게 가능하지? 미설정시 전체 프로젝트에서 사용가능하게
하고".

## 규칙 셋

1. 배정이 **없는** 슬롯 → 모든 프로젝트가 쓴다. 지금까지의 동작 그대로다.
2. 배정이 **있는** 슬롯 → 그 프로젝트들만 쓰고, 그 프로젝트는 이 슬롯을
   **먼저** 집는다.
3. 배정된 프로젝트 안에서는 최후 수단 스위치(`slot_gate`)를 묻지 않는다.
   배정한 것 자체가 허락이다. 배정 밖 프로젝트에서는 스위치와 무관하게
   아예 후보가 아니다.

## 왜 "우선" 이고 "전용" 이 아닌가

대표님이 우선으로 정하셨다. 즉 배정 슬롯이 막히면 **배정 없는 슬롯으로
내려간다** — 일이 멈추지 않는 쪽을 택하신 것이다. 대신 그 순간 남의 계정
대신 우리 계정을 쓰게 되므로, 내려갈 때 로그를 남긴다.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Set

import structlog

logger = structlog.get_logger(__name__)

_CACHE_TTL_SEC = 30.0
_cache: Dict[str, object] = {"at": 0.0, "map": {}}


async def slot_project_map(force: bool = False) -> Dict[str, Set[str]]:
    """{슬롯: {프로젝트…}}. 배정이 없는 슬롯은 아예 키가 없다."""
    now = time.time()
    if not force and now - float(_cache["at"] or 0) < _CACHE_TTL_SEC:
        return dict(_cache["map"])  # type: ignore[arg-type]

    mapping: Dict[str, Set[str]] = {}
    try:
        from app.core.db_pool import get_pool

        rows = await get_pool().fetch(
            "SELECT slot, project_key FROM oauth_slot_projects"
        )
        for r in rows:
            mapping.setdefault(str(r["slot"]), set()).add(str(r["project_key"]).upper())
    except Exception as exc:
        # 못 읽으면 **배정이 없는 것으로 본다.** 그러면 종전 동작(전역 폴백)이
        # 되어 일이 멈추지 않는다. 배정이 있는 것으로 잘못 보면 엉뚱한
        # 프로젝트가 슬롯을 통째로 잃는다.
        logger.warning("slot_project_map_failed error=%s", str(exc)[:160])
        return {}

    _cache["map"] = mapping
    _cache["at"] = now
    return dict(mapping)


def invalidate() -> None:
    _cache["at"] = 0.0


async def project_of_session(session_id: str) -> str:
    """이 세션이 속한 프로젝트 키. 못 찾으면 빈 문자열."""
    if not session_id:
        return ""
    try:
        from app.core.db_pool import get_pool

        value = await get_pool().fetchval(
            "SELECT COALESCE(w.project_key, '') FROM chat_sessions s "
            "JOIN chat_workspaces w ON w.id = s.workspace_id "
            "WHERE s.id = $1::uuid",
            session_id,
        )
        return str(value or "").upper()
    except Exception as exc:
        logger.debug("project_of_session_failed session=%s error=%s",
                     session_id[:8], str(exc)[:120])
        return ""


async def order_slots_for_project(slots: List[str], project: str) -> List[str]:
    """이 프로젝트가 쓸 슬롯을, 쓸 순서대로.

    배정 슬롯이 앞, 배정 없는 슬롯이 뒤. 남의 프로젝트에 배정된 슬롯은 뺀다.
    """
    mapping = await slot_project_map()
    if not mapping:
        return list(slots)

    project = (project or "").upper()
    mine: List[str] = []
    free: List[str] = []
    dropped: List[str] = []
    for slot in slots:
        assigned = mapping.get(slot)
        if not assigned:
            free.append(slot)
        elif project and project in assigned:
            mine.append(slot)
        else:
            dropped.append(slot)

    if dropped:
        logger.info(
            "slot_project_filter project=%s 제외=%s (다른 프로젝트 배정)",
            project or "-", ",".join(dropped),
        )
    if mine and free:
        logger.debug("slot_project_order project=%s 우선=%s 예비=%s",
                     project, ",".join(mine), ",".join(free))
    return mine + free


async def is_assigned(slot: str, project: str) -> bool:
    """이 프로젝트에 이 슬롯이 배정돼 있나 (최후 수단 스위치 면제 판정)."""
    mapping = await slot_project_map()
    assigned = mapping.get(str(slot))
    return bool(assigned and (project or "").upper() in assigned)


async def set_projects(slot: str, projects: List[str], by: str = "CEO") -> Dict[str, object]:
    """이 슬롯의 배정을 통째로 바꾼다. 빈 목록이면 배정 해제(= 전체 사용)."""
    from app.core.db_pool import get_pool

    slot = str(slot or "").strip()
    if not slot:
        return {"ok": False, "error": "slot_required"}
    keys = sorted({str(p).strip().upper() for p in (projects or []) if str(p).strip()})

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM oauth_slot_projects WHERE slot = $1", slot)
            for key in keys:
                await conn.execute(
                    "INSERT INTO oauth_slot_projects (slot, project_key, created_by) "
                    "VALUES ($1, $2, $3)",
                    slot, key, by[:100],
                )
    invalidate()

    # 어느 계정을 어느 프로젝트가 쓰는지는 돈과 직결된다. 바뀐 사실을 남긴다.
    try:
        from app.services import ohvis_alert

        await ohvis_alert.notify(
            "슬롯 %s 프로젝트 배정 %s" % (slot, "변경" if keys else "해제"),
            ("배정: %s — 이 프로젝트들이 슬롯 %s 를 먼저 씁니다."
             % (", ".join(keys), slot)) if keys
            else "배정을 없앴습니다. 슬롯 %s 는 다시 모든 프로젝트가 씁니다." % slot,
            severity=ohvis_alert.INFO, category="oauth_slot", project="AADS",
        )
    except Exception:
        pass

    logger.info("slot_projects_set slot=%s projects=%s by=%s", slot, keys, by)
    return {"ok": True, "slot": slot, "projects": keys}


async def set_company_slot(project: str, slot: Optional[str], by: str = "CEO") -> Dict[str, object]:
    """이 회사가 쓸 계정을 정한다. slot 이 비면 배정 해제(= 자동 순서).

    화면은 슬롯이 아니라 회사를 기준으로 고른다 — "이 회사는 내 계정" 이
    대표님이 실제로 하시는 판단이기 때문이다. 저장 구조는 슬롯→회사 이므로
    여기서 뒤집는다. 한 회사가 두 슬롯에 걸리면 둘 다 '우선' 이 되어 순서가
    흔들리므로, 새로 배정하기 전에 다른 슬롯에서 먼저 뺀다.
    """
    from app.core.db_pool import get_pool

    key = str(project or "").strip().upper()
    if not key:
        return {"ok": False, "error": "project_required"}
    target = str(slot or "").strip()

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM oauth_slot_projects WHERE project_key = $1", key)
            if target:
                await conn.execute(
                    "INSERT INTO oauth_slot_projects (slot, project_key, created_by) "
                    "VALUES ($1, $2, $3)",
                    target, key, by[:100],
                )
    invalidate()

    try:
        from app.services import ohvis_alert

        await ohvis_alert.notify(
            "%s 계정 배정 %s" % (key, "변경" if target else "해제"),
            ("%s 는 이제 슬롯 %s 를 먼저 씁니다." % (key, target)) if target
            else "%s 의 배정을 없앴습니다 — 다시 자동 순서로 돕니다." % key,
            severity=ohvis_alert.INFO, category="oauth_slot", project="AADS",
        )
    except Exception:
        pass

    logger.info("slot_company_set project=%s slot=%s by=%s", key, target or "-", by)
    return {"ok": True, "project": key, "slot": target}
