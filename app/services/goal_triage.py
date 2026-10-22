"""목표 첫 정리 일괄 반영 — POST /api/v1/goals/triage/apply 의 본체.

정본: docs/prd/20261022_AADS_목표_첫정리_실행_PRD.md (document_key=goal-first-triage-execution-prd §3-1 TR-1~6).

한 트랜잭션 안에서 ① keep_p0 밖의 active P0 를 P1 로 내리고 ② actions 를 반영하고 ③ 반영 후 active P0 가
상한(3)을 넘으면 전부 롤백한다. 행은 지우지 않는다(close=cancelled, merge=archived).
권한(CEO)은 라우터가 먼저 판정한다 — 여기서는 이미 통과한 호출만 받는다.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional, Sequence

from fastapi import HTTPException

from app.services import goal_signals
from app.services.goal_grant_suspension import sync_goal_grants

OPEN_STATUSES = goal_signals.OPEN_STATUSES
CLOSED_STATUSES = ("completed", "cancelled", "archived")
ACTIONS = ("resume", "pause", "close", "merge", "unblock")
DEMOTE_TO = "P1"
AUDIT_SOURCE = "triage_apply"
DEFAULT_PAUSE_REASON = "첫 정리에서 일시정지"


def candidates_version(rows: Sequence[Any]) -> str:
    """정리 후보 집합의 지문. 열린 목표(draft/active/blocked)의 (id, status, priority, 일시정지)로 만든다.

    카드를 만든 뒤 누가 후보의 상태·우선순위를 바꾸면 값이 달라진다. 진행률·제목 같은 것은 후보를 바꾸지
    않으므로 넣지 않는다. 목표가 14일 휴면 선을 넘는 시간 경과도 넣지 않는다 — 시간만 지나도 값이 흔들리면
    카드가 늘 stale 이 된다.
    """
    lines = sorted(
        "%s|%s|%s|%d" % (
            str(r["id"]), str(r["status"] or "").lower(),
            str(r["priority"] or "").strip().upper(), 1 if r["paused_at"] is not None else 0,
        )
        for r in rows if str(r["status"] or "").lower() in OPEN_STATUSES
    )
    return "v1:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:32]


async def current_candidates_version(conn: Any, tenant_id: str) -> str:
    rows = await conn.fetch(
        "SELECT id, status, priority, paused_at FROM goals "
        "WHERE tenant_id = $1::uuid AND status = ANY($2::text[])",
        tenant_id, list(OPEN_STATUSES),
    )
    return candidates_version(rows)


def request_fingerprint(
    keep_p0: Sequence[str], actions: Sequence[dict[str, Any]], version: str,
) -> str:
    canon = {
        "keep_p0": sorted(keep_p0),
        "actions": sorted(
            ({"goal_id": a["goal_id"], "action": a["action"],
              "merge_into": a.get("merge_into"), "reason": a.get("reason")} for a in actions),
            key=lambda a: a["goal_id"],
        ),
        "candidates_version": version,
    }
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _candidate_view(row: Any) -> dict[str, Any]:
    return {
        "goal_id": str(row["id"]), "title": row["title"], "project": row["project"],
        "status": row["status"], "priority": row["priority"],
        "goal_paused": row["paused_at"] is not None,
    }


def _http(http_status: int, error: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"error": error, **extra})


def _jsonb(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


def _plan_actions(
    actions: Sequence[dict[str, Any]], by_id: dict[str, Any], tenant_goal_ids: set[str],
) -> None:
    """상태를 바꾸기 전에 요청 전체가 유효한지 본다. 하나라도 틀리면 아무것도 건드리지 않는다."""
    seen: set[str] = set()
    ending = {a["goal_id"] for a in actions if a["action"] in ("close", "merge")}
    for a in actions:
        gid = a["goal_id"]
        if gid in seen:
            raise _http(422, "duplicate_action", goal_id=gid)
        seen.add(gid)
        if gid not in tenant_goal_ids:
            raise _http(404, "goal_not_found", goal_id=gid)
        row = by_id[gid]
        status = str(row["status"] or "").lower()
        if status in CLOSED_STATUSES:
            raise _http(422, "goal_already_closed", goal_id=gid, status=status)
        if a["action"] == "unblock" and status != "blocked":
            raise _http(422, "not_blocked", goal_id=gid, status=status)
        if a["action"] == "merge":
            target = a.get("merge_into")
            if not target:
                raise _http(422, "merge_into_required", goal_id=gid)
            target_row = by_id.get(target)
            if target == gid or target not in tenant_goal_ids or target_row is None:
                raise _http(422, "invalid_merge_target", goal_id=gid, merge_into=target)
            if str(target_row["status"] or "").lower() in ("cancelled", "archived") or target in ending:
                raise _http(422, "invalid_merge_target", goal_id=gid, merge_into=target)


async def apply_triage(
    conn: Any, *, tenant_id: str, actor: str, keep_p0: Sequence[str],
    actions: Sequence[dict[str, Any]], client_version: str, idempotency_key: str,
) -> dict[str, Any]:
    """호출자가 연 트랜잭션 안에서 실행한다. HTTPException 이 나면 호출자 트랜잭션이 통째로 롤백된다."""
    keep = list(dict.fromkeys(keep_p0))
    acts = [dict(a) for a in actions]
    fingerprint = request_fingerprint(keep, acts, client_version)

    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "goal_triage:" + tenant_id)

    prior = await conn.fetchrow(
        "SELECT request_hash, response FROM goal_triage_runs "
        "WHERE tenant_id = $1::uuid AND idempotency_key = $2",
        tenant_id, idempotency_key,
    )
    if prior:
        if prior["request_hash"] != fingerprint:
            raise _http(422, "idempotency_key_reused",
                        message="같은 Idempotency-Key 로 다른 요청이 왔습니다. 새 키로 보내세요.")
        return _jsonb(prior["response"])

    referenced = set(keep) | {a["goal_id"] for a in acts} | {
        a["merge_into"] for a in acts if a.get("merge_into")
    }
    rows = await conn.fetch(
        "SELECT id, title, project, status, priority, paused_at FROM goals "
        "WHERE tenant_id = $1::uuid AND (status = ANY($2::text[]) OR id = ANY($3::uuid[])) "
        "ORDER BY id FOR UPDATE",
        tenant_id, list(OPEN_STATUSES), sorted(referenced),
    )
    by_id = {str(r["id"]): dict(r) for r in rows}
    latest = candidates_version(rows)
    if latest != client_version:
        raise _http(
            409, "stale_candidates", candidates_version=latest,
            candidates=[_candidate_view(r) for r in rows
                        if str(r["status"] or "").lower() in OPEN_STATUSES],
            message="카드를 만든 뒤 목표 상태가 바뀌었습니다. 최신 후보로 다시 고르세요.",
        )

    missing = sorted(referenced - set(by_id))
    if missing:
        raise _http(404, "goal_not_found", goal_ids=missing)
    bad_keep = [
        g for g in keep
        if str(by_id[g]["status"] or "").lower() != "active" or not goal_signals.is_p0(by_id[g]["priority"])
    ]
    if bad_keep:
        raise _http(422, "keep_p0_not_active_p0", goal_ids=bad_keep)
    _plan_actions(acts, by_id, set(by_id))

    run_id = str(uuid.uuid4())
    before = {g: {"status": r["status"], "priority": r["priority"], "paused": r["paused_at"] is not None}
              for g, r in by_id.items()}
    notes: dict[str, dict[str, Any]] = {}

    # ① keep_p0 밖의 active P0(P0-CRITICAL 포함) → P1
    downgraded: list[dict[str, Any]] = []
    for gid, row in by_id.items():
        if gid in keep or str(row["status"] or "").lower() != "active" or not goal_signals.is_p0(row["priority"]):
            continue
        await conn.execute(
            "UPDATE goals SET priority = $3, updated_at = NOW() WHERE id = $1::uuid AND tenant_id = $2::uuid",
            gid, tenant_id, DEMOTE_TO,
        )
        downgraded.append({"goal_id": gid, "from": row["priority"], "to": DEMOTE_TO})
        notes.setdefault(gid, {})["priority"] = {"from": row["priority"], "to": DEMOTE_TO}
        row["priority"] = DEMOTE_TO

    # ② actions
    applied: list[dict[str, Any]] = []
    for a in acts:
        gid, kind = a["goal_id"], a["action"]
        row = by_id[gid]
        reason = (a.get("reason") or "").strip()
        if kind in ("resume", "unblock"):
            if str(row["status"]).lower() != "active" or row["paused_at"] is not None:
                await conn.execute(
                    "UPDATE goals SET status = 'active', paused_at = NULL, paused_reason = NULL, "
                    "paused_by = NULL, updated_at = NOW() WHERE id = $1::uuid AND tenant_id = $2::uuid",
                    gid, tenant_id,
                )
                row["status"], row["paused_at"] = "active", None
        elif kind == "pause":
            if row["paused_at"] is None:
                await conn.execute(
                    "UPDATE goals SET paused_at = NOW(), paused_reason = $3, paused_by = $4, "
                    "updated_at = NOW() WHERE id = $1::uuid AND tenant_id = $2::uuid",
                    gid, tenant_id, reason or DEFAULT_PAUSE_REASON, actor,
                )
                row["paused_at"] = True
        elif kind == "close":
            await conn.execute(
                "UPDATE goals SET status = 'cancelled', updated_at = NOW() "
                "WHERE id = $1::uuid AND tenant_id = $2::uuid", gid, tenant_id,
            )
            row["status"] = "cancelled"
        elif kind == "merge":
            await conn.execute(
                "UPDATE goals SET status = 'archived', updated_at = NOW() "
                "WHERE id = $1::uuid AND tenant_id = $2::uuid", gid, tenant_id,
            )
            row["status"] = "archived"
            notes.setdefault(gid, {})["merged_into"] = a["merge_into"]
        note = notes.setdefault(gid, {})
        note["action"] = kind
        if reason:
            note["reason"] = reason
        applied.append({"goal_id": gid, "action": kind, "merge_into": a.get("merge_into")})

    # ③ 상한 — 반영 후 active P0 가 3 을 넘으면 전체 롤백
    active_p0 = await goal_signals.active_p0_goals(conn, tenant_id)
    if len(active_p0) > goal_signals.P0_CAP:
        raise _http(
            409, "p0_cap_exceeded", cap=goal_signals.P0_CAP,
            message="반영 후 진행 중 P0 가 %d개입니다(상한 %d). 아무것도 바뀌지 않았습니다."
            % (len(active_p0), goal_signals.P0_CAP),
            current_p0=[{"goal_id": o["goal_id"], "title": o["title"],
                         "project": o["project"], "priority": o["priority"]} for o in active_p0],
        )

    # 감사(TR-6)·자동승인 정합(AS-1·2) — 실제로 바뀐 목표마다 1행
    changed: list[str] = []
    suspended: list[str] = []
    restored: list[str] = []
    for gid, row in by_id.items():
        old = before[gid]
        now_paused = row["paused_at"] is not None
        status_changed = str(old["status"]).lower() != str(row["status"]).lower()
        if not (status_changed or old["priority"] != row["priority"] or old["paused"] != now_paused):
            continue
        changed.append(gid)
        note = {"triage_run_id": run_id, **notes.get(gid, {})}
        if old["paused"] != now_paused:
            note["paused"] = now_paused
        await conn.execute(
            "INSERT INTO goal_status_audit (goal_id, old_status, new_status, actor, source, tenant_id, note) "
            "VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7)",
            gid, old["status"], row["status"], actor, AUDIT_SOURCE, tenant_id,
            json.dumps(note, ensure_ascii=False),
        )
        synced = await sync_goal_grants(conn, gid, source=AUDIT_SOURCE)
        suspended += synced["suspended"]
        restored += synced["restored"]

    response = {
        "triage_run_id": run_id,
        "keep_p0": keep,
        "downgraded": downgraded,
        "applied": applied,
        "changed_goal_ids": changed,
        "audit_rows": len(changed),
        "grants": {"suspended": suspended, "restored": restored},
        "active_p0": len(active_p0),
        "candidates_version": await current_candidates_version(conn, tenant_id),
    }
    await conn.execute(
        "INSERT INTO goal_triage_runs (id, tenant_id, idempotency_key, request_hash, actor, response) "
        "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb)",
        run_id, tenant_id, idempotency_key, fingerprint, actor, json.dumps(response, ensure_ascii=False),
    )
    return response


async def triage_apply(
    pool: Any, *, tenant_id: str, actor: str, keep_p0: Sequence[str],
    actions: Sequence[dict[str, Any]], client_version: str, idempotency_key: Optional[str],
) -> dict[str, Any]:
    key = (idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise _http(400, "idempotency_key_required",
                    message="Idempotency-Key 헤더(1~200자)가 필요합니다.")
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await apply_triage(
                conn, tenant_id=tenant_id, actor=actor, keep_p0=keep_p0, actions=actions,
                client_version=client_version, idempotency_key=key,
            )
