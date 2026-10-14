"""'내 자동화' 통합 목록 (설계서 6절 GET /automations).

work_recipes + ovis_recipes + browser_recipes + automation_drafts 를 한 모양으로 합친다.
- 상태는 네 가지로만 보인다: 사용 가능 / 확인 기다리는 중 / 고장 / 만료.
- 출처(work/ovis/browser/draft)는 관리자 상세에서만 내보낸다.
- 마지막 실행은 상태와 시각만 — 오류 문장은 싣지 않는다(비밀값이 섞일 수 있다).
- ovis_recipes 중 'work:'/'browser:' 로 시작하는 정본 키는 같은 레시피의 정규화 사본이라 건너뛴다(중복 방지).
- 어느 쪽 표가 아직 없으면 그 출처만 건너뛰고 응답의 unavailable_sources 에 적는다.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.core.db_pool import get_pool
from app.services import automation_draft_service as drafts
from app.services import automation_schedule as schedule
from app.services.work_recipe.store import list_recipes

logger = logging.getLogger(__name__)

STATUS_LABELS: dict[str, str] = {
    "available": "사용 가능",
    "waiting": "확인 기다리는 중",
    "broken": "고장",
    "expired": "만료",
}

_DRAFT_STATUS: dict[str, str] = {
    "active": "available",
    "paused": "available",
    "pending_approval": "waiting",
    "broken": "broken",
    "failed": "broken",
    "expired": "expired",
    "rejected": "expired",
}
_HIDDEN_DRAFT_STATES = frozenset({"cancelled"})
_OVIS_STATUS: dict[str, str] = {
    "active": "available",
    "draft": "waiting", "candidate": "waiting", "shadow": "waiting",
    "quarantined": "broken", "deprecated": "broken",
    "archived": "expired",
}
_RUN_FAILED = frozenset({"failed", "error", "blocked"})

_WORK_RUNS_SQL = """
SELECT DISTINCT ON (domain, recipe_name) domain, recipe_name, status, finished_at, started_at, created_at
  FROM recipe_runs WHERE tenant_id = $1::uuid
 ORDER BY domain, recipe_name, created_at DESC
"""
_BROWSER_SQL = """
SELECT r.id, r.recipe_id, r.title, r.enabled, r.updated_at
  FROM browser_recipes r
 WHERE r.tenant_id = $1::uuid
   AND r.version = (SELECT b.version FROM browser_recipes b
                     WHERE b.tenant_id = r.tenant_id AND b.recipe_id = r.recipe_id
                     ORDER BY b.created_at DESC LIMIT 1)
"""
_BROWSER_RUNS_SQL = """
SELECT DISTINCT ON (recipe_id) recipe_id, status, completed_at, started_at, created_at
  FROM browser_recipe_runs WHERE tenant_id = $1::uuid
 ORDER BY recipe_id, created_at DESC
"""
_OVIS_SQL = """
SELECT o.id, o.canonical_key, v.status
  FROM ovis_recipes o
  JOIN LATERAL (SELECT status FROM ovis_recipe_versions
                 WHERE recipe_id = o.id ORDER BY created_at DESC LIMIT 1) v ON TRUE
 WHERE o.tenant_id = $1::uuid
   AND o.canonical_key NOT LIKE 'work:%' AND o.canonical_key NOT LIKE 'browser:%'
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _last_run(row: dict[str, Any] | None, *time_columns: str) -> dict[str, Any] | None:
    if not row:
        return None
    at = next((_iso(row.get(col)) for col in time_columns if row.get(col)), None)
    return {"status": str(row.get("status") or ""), "at": at}


def _item(
    key: str, name: str, status: str, *, source: str, last_run: dict[str, Any] | None = None,
    next_run: str | None = None, draft_id: str | None = None, owner: str | None = None,
    detail_state: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key, "name": name, "status": status, "status_label": STATUS_LABELS[status],
        "last_run": last_run, "next_run": next_run, "draft_id": draft_id,
        "_source": source, "_owner": owner, "_detail_state": detail_state,
    }


def draft_item(draft: dict[str, Any]) -> dict[str, Any] | None:
    state = str(draft["state"])
    if state in _HIDDEN_DRAFT_STATES:
        return None
    status = _DRAFT_STATUS.get(state, "waiting")
    job_id = draft.get("schedule_job_id")
    next_run = schedule.next_run_at(job_id) if state == "active" else None
    return _item(
        f"draft:{draft['id']}", str(draft.get("title") or ""), status, source="draft",
        next_run=next_run, draft_id=str(draft["id"]), owner=str(draft.get("owner_user") or ""),
        detail_state=state,
    )


async def _work_items(tenant_id: str, taken_names: set[str]) -> list[dict[str, Any]]:
    rows = await list_recipes(tenant_id=tenant_id)
    async with get_pool().acquire() as conn:
        runs = {(r["domain"], r["recipe_name"]): dict(r) for r in await conn.fetch(_WORK_RUNS_SQL, str(tenant_id))}
    items = []
    for row in rows:
        name = str(row.get("name") or "")
        if name in taken_names:
            continue
        last = _last_run(runs.get((row.get("domain"), name)), "finished_at", "started_at", "created_at")
        status = "broken" if last and last["status"] in _RUN_FAILED else "available"
        items.append(_item(f"work:{row['id']}", name, status, source="work_recipe", last_run=last))
    return items


async def _browser_items(tenant_id: str) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_BROWSER_SQL, str(tenant_id))]
        runs = {r["recipe_id"]: dict(r) for r in await conn.fetch(_BROWSER_RUNS_SQL, str(tenant_id))}
    items = []
    for row in rows:
        last = _last_run(runs.get(row["recipe_id"]), "completed_at", "started_at", "created_at")
        if not row["enabled"]:
            status = "expired"
        else:
            status = "broken" if last and last["status"] in _RUN_FAILED else "available"
        items.append(_item(
            f"browser:{row['id']}", str(row.get("title") or row["recipe_id"]), status,
            source="browser_recipe", last_run=last,
        ))
    return items


async def _ovis_items(tenant_id: str) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_OVIS_SQL, str(tenant_id))]
    return [
        _item(
            f"ovis:{row['id']}", str(row["canonical_key"]).split(":", 1)[-1],
            _OVIS_STATUS.get(str(row["status"]), "waiting"), source="ovis_recipe",
        )
        for row in rows
    ]


async def list_automations(
    *, tenant_id: str, user: str, is_admin: bool, status: str | None = None,
) -> dict[str, Any]:
    """통합 목록. 일반 사용자는 자기 초안만, 관리자는 전체 초안과 출처·소유자를 본다."""
    draft_rows = await drafts.list_drafts(tenant_id=tenant_id, owner_user=None if is_admin else user)
    items = [it for it in (draft_item(d) for d in draft_rows) if it]
    taken = {name for d in draft_rows for name in (d.get("recipe_names") or [])}
    unavailable: list[str] = []
    for label, loader in (
        ("work_recipe", lambda: _work_items(tenant_id, taken)),
        ("browser_recipe", lambda: _browser_items(tenant_id)),
        ("ovis_recipe", lambda: _ovis_items(tenant_id)),
    ):
        try:
            items.extend(await loader())
        except asyncpg.UndefinedTableError:
            logger.warning("automation_catalog_source_missing: %s", label)
            unavailable.append(label)
    if status:
        items = [it for it in items if it["status"] == status]
    for it in items:
        source, owner, detail_state = it.pop("_source"), it.pop("_owner"), it.pop("_detail_state")
        if is_admin:
            it["detail"] = {"source": source, "owner": owner, "state": detail_state}
    counts = {code: sum(1 for it in items if it["status"] == code) for code in STATUS_LABELS}
    result: dict[str, Any] = {"automations": items, "counts": counts, "total": len(items)}
    if is_admin and unavailable:
        result["unavailable_sources"] = unavailable
    return result
