"""OHVIS main chat domain logic: route, durable inbox, evidence review, grants and the effect outbox.

Invariants this module keeps:
- Everything is tenant scoped; the tenant always comes from the authenticated context, never from a payload.
- A report is stored before anything reacts to it; redelivery of the same event is a no-op, a changed payload is quarantined.
- Review decisions come from deterministic evidence checks. The authorization gate is also deterministic and fail-closed.
- An effect needs an unexpired, unrevoked execute_followup grant bound to the exact commit/generation and job hash.
- Network I/O for an effect happens outside any DB transaction; an ambiguous outcome is `unknown`, never a retry.
- Nothing here talks to anything outside OHVIS. The default adapter refuses to dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID

from app.models.ohvis_main_chat import (
    ACTION_REQUIRES_GRANT, CARD_LABELS, IMPLEMENTATION_EVENT_TYPES, SUPPORTED_ACTIONS, ControlRequest, GrantCreate,
    ReportEvent, RouteUpsert, canonical_json, effect_key, sha256_hex,
)

logger = logging.getLogger(__name__)

LEASE_SECONDS = 120
MAX_GRANT_TTL = timedelta(hours=72)
DISPATCH_TIMEOUT_SECONDS = 30.0
STALE_DISPATCHING = timedelta(minutes=5)
EFFECT_COUNTED_STATES = ("dispatching", "confirmed", "verified", "unknown")


class MainChatError(Exception):
    def __init__(self, status: int, code: str, **extra: Any):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


@dataclass(frozen=True)
class Flags:
    enabled: bool
    auto_effect: bool
    model_call_cap: int | None
    max_effects_per_root: int | None


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _int_or_none(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def load_flags() -> Flags:
    """Everything defaults to off; an unset or invalid cap means "no budget", which the gates treat as a refusal."""
    return Flags(
        enabled=_truthy("OHVIS_MAIN_CHAT_ENABLED"),
        auto_effect=_truthy("OHVIS_MAIN_CHAT_AUTO_EFFECT"),
        model_call_cap=_int_or_none("OHVIS_MAIN_CHAT_MODEL_CALL_CAP"),
        max_effects_per_root=_int_or_none("OHVIS_MAIN_CHAT_MAX_EFFECTS_PER_ROOT"),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return canonical_json(value)


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def audit(conn: Any, tenant: str, actor: str, action: str, ref: dict[str, Any] | None = None,
                reason: str = "", owner_epoch: int | None = None) -> None:
    await conn.execute(
        "INSERT INTO ohvis_main_chat_audit (tenant_id, actor, action, ref, reason, owner_epoch) "
        "VALUES ($1::uuid, $2, $3, $4::jsonb, $5, $6)",
        tenant, actor, action, _json(ref or {}), reason[:500], owner_epoch)


async def notice(conn: Any, tenant: str, project: str, user: str | None, kind: str, inbox_id: Any, title: str,
                 body: str, dedupe_key: str) -> None:
    """In-app notice only. The link is always an OHVIS-internal path."""
    if not user:
        return
    link = f"/projects/{project}/main-chat?inbox={inbox_id}" if inbox_id else f"/projects/{project}/main-chat"
    await conn.execute(
        "INSERT INTO ohvis_main_chat_notices (tenant_id, project_key, recipient_user_id, kind, inbox_id, title, body, "
        "link, dedupe_key) VALUES ($1::uuid, $2, $3, $4, $5::uuid, $6, $7, $8, $9) "
        "ON CONFLICT (tenant_id, recipient_user_id, dedupe_key) DO NOTHING",
        tenant, project, user, kind, str(inbox_id) if inbox_id else None, title[:200], body[:1000], link, dedupe_key)


# --------------------------------------------------------------------------------------------- routes

async def upsert_route(conn: Any, tenant: str, actor: str, project: str, body: RouteUpsert) -> dict[str, Any]:
    async with conn.transaction():
        in_tenant = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM chat_sessions WHERE id=$1::uuid AND tenant_id=$2::uuid)",
            str(body.main_session_id), tenant)
        if not in_tenant:
            raise MainChatError(422, "session_not_in_tenant")
        if body.goal_id is not None and body.accepts_goal_events:
            raise MainChatError(422, "goal_route_cannot_accept_goal_events")
        row = await conn.fetchrow(
            "SELECT * FROM ohvis_main_chat_routes WHERE tenant_id=$1::uuid AND project_key=$2 "
            "AND COALESCE(goal_id, '00000000-0000-0000-0000-000000000000'::uuid) = "
            "COALESCE($3::uuid, '00000000-0000-0000-0000-000000000000'::uuid) FOR UPDATE",
            tenant, project, str(body.goal_id) if body.goal_id else None)
        if row is None:
            if body.expected_revision != 0:
                raise MainChatError(409, "revision_conflict", current_revision=0)
            row = await conn.fetchrow(
                "INSERT INTO ohvis_main_chat_routes (tenant_id, project_key, goal_id, main_session_id, "
                "accepts_goal_events, created_by, updated_by) VALUES ($1::uuid,$2,$3::uuid,$4::uuid,$5,$6,$6) "
                "RETURNING *",
                tenant, project, str(body.goal_id) if body.goal_id else None, str(body.main_session_id),
                body.accepts_goal_events, actor)
        else:
            if body.expected_revision != row["revision"]:
                raise MainChatError(409, "revision_conflict", current_revision=row["revision"])
            row = await conn.fetchrow(
                "UPDATE ohvis_main_chat_routes SET main_session_id=$2::uuid, accepts_goal_events=$3, enabled=true, "
                "revision=revision+1, updated_by=$4, updated_at=now() WHERE id=$1 RETURNING *",
                row["id"], str(body.main_session_id), body.accepts_goal_events, actor)
        await audit(conn, tenant, actor, "route_upsert",
                    {"project": project, "goal_id": str(body.goal_id) if body.goal_id else None,
                     "main_session_id": str(body.main_session_id), "revision": row["revision"]})
        view = _route_view(row)
        view["released_waiting_reports"] = await release_waiting_route(conn, tenant, project)
        return view


def _route_view(row: Any) -> dict[str, Any]:
    return {"project_key": row["project_key"], "goal_id": str(row["goal_id"]) if row["goal_id"] else None,
            "main_session_id": str(row["main_session_id"]), "accepts_goal_events": row["accepts_goal_events"],
            "revision": row["revision"], "enabled": row["enabled"], "updated_by": row["updated_by"]}


async def list_routes(conn: Any, tenant: str, project: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT * FROM ohvis_main_chat_routes WHERE tenant_id=$1::uuid AND project_key=$2 ORDER BY goal_id NULLS FIRST",
        tenant, project)
    return [_route_view(r) for r in rows]


async def resolve_route(conn: Any, tenant: str, project: str, goal_id: UUID | None) -> Any | None:
    """Exact goal route first; the project route only serves goal events when it opted in. No other fallback."""
    if goal_id is not None:
        row = await conn.fetchrow(
            "SELECT * FROM ohvis_main_chat_routes WHERE tenant_id=$1::uuid AND project_key=$2 AND goal_id=$3::uuid "
            "AND enabled", tenant, project, str(goal_id))
        if row is not None:
            return row
        return await conn.fetchrow(
            "SELECT * FROM ohvis_main_chat_routes WHERE tenant_id=$1::uuid AND project_key=$2 AND goal_id IS NULL "
            "AND enabled AND accepts_goal_events", tenant, project)
    return await conn.fetchrow(
        "SELECT * FROM ohvis_main_chat_routes WHERE tenant_id=$1::uuid AND project_key=$2 AND goal_id IS NULL "
        "AND enabled", tenant, project)


# --------------------------------------------------------------------------------------------- inbox

async def ingest(conn: Any, tenant: str, actor: str, event: ReportEvent) -> dict[str, Any]:
    """Durably record a report. Returns {"id", "state", "duplicate"}; a changed payload under the same id is quarantined.

    The conflict is raised only after the transaction that recorded its audit row has committed."""
    if _secret_pattern().search(canonical_json(event.model_dump(mode="json"))):
        raise MainChatError(422, "secret_in_report")
    result = await _ingest_tx(conn, tenant, actor, event)
    if result.pop("conflict", False):
        raise MainChatError(409, "payload_conflict", inbox_id=result["id"])
    return result


async def _ingest_tx(conn: Any, tenant: str, actor: str, event: ReportEvent) -> dict[str, Any]:
    digest = event.payload_hash()
    async with conn.transaction():
        route = await resolve_route(conn, tenant, event.project_key, event.goal_id)
        newer = await conn.fetchval(
            "SELECT max(source_revision) FROM ohvis_report_inbox WHERE tenant_id=$1::uuid AND project_key=$2 "
            "AND root_task_id=$3 AND source_kind=$4", tenant, event.project_key, event.root_task_id,
            event.source_kind)
        state = "waiting_route" if route is None else "pending_review"
        if newer is not None and newer > event.source_revision:
            state = "obsolete"
        row = await conn.fetchrow(
            "INSERT INTO ohvis_report_inbox (tenant_id, source_kind, source_event_id, source_revision, schema_version, "
            "event_type, project_key, goal_id, root_task_id, correlation_id, causation_id, runner_job_id, "
            "source_session_id, main_session_id, route_revision, generation_id, commit_sha, diff_sha256, payload, "
            "payload_sha256, occurred_at, state) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8::uuid,$9,$10,$11,$12,$13::uuid,"
            "$14::uuid,$15,$16,$17,$18,$19::jsonb,$20,$21,$22) "
            "ON CONFLICT (tenant_id, source_kind, source_event_id) DO NOTHING RETURNING id, state",
            tenant, event.source_kind, event.source_event_id, event.source_revision, event.schema_version,
            event.event_type, event.project_key, str(event.goal_id) if event.goal_id else None, event.root_task_id,
            event.correlation_id, event.causation_id, event.runner_job_id,
            str(event.source_session_id) if event.source_session_id else None,
            str(route["main_session_id"]) if route else None, route["revision"] if route else None,
            event.generation_id, event.commit_sha, event.diff_sha256, _json(event.model_dump(mode="json")), digest,
            event.occurred_at, state)
        if row is None:
            existing = await conn.fetchrow(
                "SELECT id, state, payload_sha256 FROM ohvis_report_inbox "
                "WHERE tenant_id=$1::uuid AND source_kind=$2 AND source_event_id=$3 FOR UPDATE",
                tenant, event.source_kind, event.source_event_id)
            await conn.execute(
                "UPDATE ohvis_report_inbox SET delivery_attempts = delivery_attempts + 1, updated_at=now() WHERE id=$1",
                existing["id"])
            if existing["payload_sha256"] == digest:
                return {"id": str(existing["id"]), "state": existing["state"], "duplicate": True}
            await audit(conn, tenant, actor, "payload_conflict",
                        {"inbox_id": str(existing["id"]), "source_event_id": event.source_event_id,
                         "stored_sha256": existing["payload_sha256"], "received_sha256": digest},
                        "same source_event_id arrived with a different payload; stored row left untouched")
            return {"id": str(existing["id"]), "state": existing["state"], "duplicate": False, "conflict": True}
        await conn.execute(
            "UPDATE ohvis_report_inbox SET state='obsolete', updated_at=now() WHERE tenant_id=$1::uuid "
            "AND project_key=$2 AND root_task_id=$3 AND source_kind=$4 AND source_revision < $5 AND id <> $6 "
            "AND state IN ('received','pending_review','waiting_session','waiting_route','waiting_evidence')",
            tenant, event.project_key, event.root_task_id, event.source_kind, event.source_revision, row["id"])
        await audit(conn, tenant, actor, "report_ingested",
                    {"inbox_id": str(row["id"]), "source_event_id": event.source_event_id, "state": row["state"]})
        if route is not None and row["state"] == "pending_review":
            await notice(conn, tenant, event.project_key, route["updated_by"], "report_arrived", row["id"],
                         f"보고 도착: {event.root_task_id}", event.summary or event.event_type,
                         f"arrived:{row['id']}")
        return {"id": str(row["id"]), "state": row["state"], "duplicate": False}


async def release_waiting_route(conn: Any, tenant: str, project: str) -> int:
    """Reports parked in waiting_route move on once a route exists; they keep their original ordering."""
    rows = await conn.fetch(
        "SELECT id, goal_id FROM ohvis_report_inbox WHERE tenant_id=$1::uuid AND project_key=$2 "
        "AND state='waiting_route' FOR UPDATE", tenant, project)
    moved = 0
    for r in rows:
        route = await resolve_route(conn, tenant, project, r["goal_id"])
        if route is None:
            continue
        await conn.execute(
            "UPDATE ohvis_report_inbox SET main_session_id=$2::uuid, route_revision=$3, state='pending_review', "
            "updated_at=now() WHERE id=$1", r["id"], str(route["main_session_id"]), route["revision"])
        moved += 1
    return moved


async def requeue_waiting_evidence(conn: Any, tenant: str, actor: str, project: str) -> int:
    """Evidence providers are external to this module; an operator re-queues once one is available."""
    result = await conn.execute(
        "UPDATE ohvis_report_inbox SET state='pending_review', updated_at=now() WHERE tenant_id=$1::uuid "
        "AND project_key=$2 AND state='waiting_evidence'", tenant, project)
    count = int(result.rsplit(" ", 1)[-1])
    if count:
        await audit(conn, tenant, actor, "evidence_requeued", {"project": project, "count": count})
    return count


_BUSY_SQL = """EXISTS (
    SELECT 1 FROM chat_sessions s JOIN chat_turn_executions te ON te.id = s.current_execution_id
    WHERE s.id = i.main_session_id AND te.status IN ('running','retrying') AND te.completed_at IS NULL
      AND te.lease_expires_at IS NOT NULL AND te.lease_expires_at > NOW())"""


def default_slot_active() -> bool:
    from app.services.chat_service import _is_local_active_api_slot
    return _is_local_active_api_slot()


async def claim_next(conn: Any, instance: str, *, slot_active: Callable[[], bool] | None = None,
                     lease_seconds: int = LEASE_SECONDS) -> dict[str, Any] | None:
    """Lease one report for review. Fenced by owner_epoch; a busy main session parks the report, it is never dropped."""
    flags = load_flags()
    if not flags.enabled:
        return None
    if not (slot_active or default_slot_active)():
        return None
    async with conn.transaction():
        await conn.execute(
            f"UPDATE ohvis_report_inbox i SET state='waiting_session', updated_at=now() WHERE i.state='pending_review' "
            f"AND {_BUSY_SQL}")
        row = await conn.fetchrow(
            f"SELECT i.* FROM ohvis_report_inbox i WHERE (i.state IN ('pending_review','waiting_session') "
            f"OR (i.state='reviewing' AND i.lease_expires_at < now())) AND NOT {_BUSY_SQL} "
            "AND NOT EXISTS (SELECT 1 FROM ohvis_main_chat_control c WHERE c.tenant_id=i.tenant_id "
            "AND c.project_key=i.project_key AND c.paused) "
            "ORDER BY i.received_at FOR UPDATE SKIP LOCKED LIMIT 1")
        if row is None:
            return None
        claimed = await conn.fetchrow(
            "UPDATE ohvis_report_inbox SET state='reviewing', owner_instance=$2, owner_epoch=owner_epoch+1, "
            "claim_count=claim_count+1, lease_expires_at=now() + make_interval(secs => $3), updated_at=now() "
            "WHERE id=$1 RETURNING *", row["id"], instance, float(lease_seconds))
        return dict(claimed)


async def reserve_model_call(conn: Any, inbox_id: str, owner_epoch: int) -> bool:
    """The only place model_call_attempts moves. Refuses without a configured cap or once the cap is spent."""
    cap = load_flags().model_call_cap
    if cap is None:
        return False
    row = await conn.fetchrow(
        "UPDATE ohvis_report_inbox SET model_call_attempts = model_call_attempts + 1, updated_at=now() "
        "WHERE id=$1::uuid AND owner_epoch=$2 AND state='reviewing' AND model_call_attempts < $3 RETURNING id",
        inbox_id, owner_epoch, cap)
    return row is not None


# --------------------------------------------------------------------------------------------- review

def decide_review(event: dict[str, Any], evidence: dict[str, Any] | None, *, superseded: bool = False
                  ) -> tuple[str, bool, list[str]]:
    """Deterministic verdict -> (decision, code_verified, reasons). tests_rechecked is never claimed here."""
    if superseded:
        return "obsolete", False, ["superseded_by_newer_revision"]
    kind = event["event_type"]
    if kind == "runner_failed":
        return "needs_rework", False, ["runner_reported_failure"]
    if kind not in IMPLEMENTATION_EVENT_TYPES:
        return "needs_decision", False, ["non_implementation_report_needs_human_read"]
    missing = [name for name in ("commit_sha", "diff_sha256") if not event.get(name)]
    if missing:
        return "unverifiable", False, [f"missing_{name}" for name in missing]
    if evidence is None:
        return "unverifiable", False, ["no_evidence_available"]
    reasons: list[str] = []
    if not evidence.get("commit_found"):
        return "unverifiable", False, ["commit_not_found"]
    if evidence.get("diff_sha256") != event["diff_sha256"]:
        reasons.append("diff_hash_mismatch")
    outside = [p for p in (evidence.get("changed_files") or []) if p not in set(evidence.get("allowed_files") or [])]
    if outside:
        reasons.append("files_outside_scope")
    if reasons:
        return "needs_rework", False, reasons
    return "verified", True, ["commit_and_diff_match_declared_scope"]


EvidenceProvider = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


async def record_review(conn: Any, inbox_id: str, owner_epoch: int, evidence: dict[str, Any] | None,
                        actor: str = "ohvis-main-chat") -> dict[str, Any]:
    """Finish a leased review. A stale epoch (another instance reclaimed the row) writes nothing."""
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM ohvis_report_inbox WHERE id=$1::uuid FOR UPDATE", inbox_id)
        if row is None:
            raise MainChatError(404, "report_not_found")
        tenant = str(row["tenant_id"])
        if row["state"] != "reviewing" or row["owner_epoch"] != owner_epoch:
            await audit(conn, tenant, actor, "stale_epoch_rejected",
                        {"inbox_id": inbox_id, "claimed_epoch": owner_epoch, "current_epoch": row["owner_epoch"],
                         "state": row["state"]})
            raise MainChatError(409, "stale_epoch")
        event = _loads(row["payload"])
        if evidence is None and row["event_type"] in IMPLEMENTATION_EVENT_TYPES and row["commit_sha"]:
            await conn.execute(
                "UPDATE ohvis_report_inbox SET state='waiting_evidence', owner_instance=NULL, lease_expires_at=NULL, "
                "updated_at=now() WHERE id=$1", row["id"])
            return {"decision": None, "state": "waiting_evidence"}
        newer = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM ohvis_report_inbox WHERE tenant_id=$1 AND project_key=$2 AND root_task_id=$3 "
            "AND source_kind=$4 AND source_revision > $5)", row["tenant_id"], row["project_key"], row["root_task_id"],
            row["source_kind"], row["source_revision"])
        decision, code_verified, reasons = decide_review(event, evidence, superseded=bool(newer))
        revision = (await conn.fetchval(
            "SELECT coalesce(max(review_revision),0)+1 FROM ohvis_report_reviews WHERE inbox_id=$1", row["id"]))
        review_id = await conn.fetchval(
            "INSERT INTO ohvis_report_reviews (tenant_id, inbox_id, review_revision, decision, code_verified, reasons, "
            "evidence, evidence_sha256, owner_epoch) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9) RETURNING id",
            row["tenant_id"], row["id"], revision, decision, code_verified, _json(reasons), _json(evidence or {}),
            sha256_hex(evidence or {}), owner_epoch)
        await conn.execute(
            "UPDATE ohvis_report_inbox SET state=$2, owner_instance=NULL, lease_expires_at=NULL, updated_at=now() "
            "WHERE id=$1", row["id"], "obsolete" if decision == "obsolete" else "reviewed")
        await audit(conn, tenant, actor, "review_recorded",
                    {"inbox_id": inbox_id, "decision": decision, "code_verified": code_verified, "reasons": reasons},
                    owner_epoch=owner_epoch)
        route = await conn.fetchrow(
            "SELECT updated_by FROM ohvis_main_chat_routes WHERE tenant_id=$1 AND project_key=$2 "
            "AND COALESCE(goal_id,'00000000-0000-0000-0000-000000000000'::uuid)="
            "COALESCE($3::uuid,'00000000-0000-0000-0000-000000000000'::uuid)",
            row["tenant_id"], row["project_key"], str(row["goal_id"]) if row["goal_id"] else None) or \
            await conn.fetchrow(
                "SELECT updated_by FROM ohvis_main_chat_routes WHERE tenant_id=$1 AND project_key=$2 AND goal_id IS NULL",
                row["tenant_id"], row["project_key"])
        recipient = route["updated_by"] if route else None
        kind = {"verified": "verified", "needs_decision": "decision_needed"}.get(decision, "blocked")
        if decision != "obsolete":
            await notice(conn, tenant, row["project_key"], recipient, kind, row["id"],
                         f"{CARD_LABELS.get(decision, decision)}: {row['root_task_id']}", ", ".join(reasons),
                         f"review:{row['id']}:{revision}")
        planned = []
        if decision == "verified" and code_verified:
            planned = await plan_effects(conn, dict(row), review_id)
        return {"decision": decision, "code_verified": code_verified, "reasons": reasons, "review_id": review_id,
                "planned_effects": planned, "state": "reviewed"}


# --------------------------------------------------------------------------------------------- grants

def _secret_pattern():
    from app.api.canonical_documents import SECRET
    return SECRET


async def create_grant(conn: Any, tenant: str, actor: str, body: GrantCreate) -> dict[str, Any]:
    now = _now()
    expires = body.expires_at if body.expires_at.tzinfo else body.expires_at.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise MainChatError(422, "grant_already_expired")
    if expires - now > MAX_GRANT_TTL:
        raise MainChatError(422, "grant_ttl_too_long", max_hours=int(MAX_GRANT_TTL.total_seconds() // 3600))
    if _secret_pattern().search(canonical_json(body.scope.model_dump(mode="json"))):
        raise MainChatError(422, "secret_in_grant_scope")
    target_hash = body.target_hash()
    async with conn.transaction():
        existing = await conn.fetchrow(
            "SELECT id, revision FROM ohvis_action_grants WHERE tenant_id=$1::uuid AND project_key=$2 "
            "AND root_task_id=$3 AND grant_type=$4 AND target_hash=$5 AND revoked_at IS NULL AND expires_at > now() "
            "AND COALESCE(generation_id,'')=COALESCE($6,'') AND COALESCE(commit_sha,'')=COALESCE($7,'') LIMIT 1",
            tenant, body.project_key, body.root_task_id, body.grant_type, target_hash, body.generation_id,
            body.commit_sha)
        if existing is not None:
            return {"id": str(existing["id"]), "revision": existing["revision"], "created": False}
        grant_id = await conn.fetchval(
            "INSERT INTO ohvis_action_grants (tenant_id, project_key, goal_id, root_task_id, grant_type, scope, "
            "target_hash, generation_id, commit_sha, expires_at, issuer, approval_ref) "
            "VALUES ($1::uuid,$2,$3::uuid,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12) RETURNING id",
            tenant, body.project_key, str(body.goal_id) if body.goal_id else None, body.root_task_id, body.grant_type,
            _json(body.scope.model_dump(mode="json")), target_hash, body.generation_id, body.commit_sha, expires,
            actor, body.approval_ref)
        await audit(conn, tenant, actor, "grant_created",
                    {"grant_id": str(grant_id), "type": body.grant_type, "root_task_id": body.root_task_id,
                     "target_hash": target_hash, "expires_at": expires.isoformat()})
        return {"id": str(grant_id), "revision": 1, "created": True}


async def revoke_grant(conn: Any, tenant: str, actor: str, grant_id: str, reason: str) -> dict[str, Any]:
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM ohvis_action_grants WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE", grant_id, tenant)
        if row is None:
            raise MainChatError(404, "grant_not_found")
        if row["revoked_at"] is not None:
            return {"id": grant_id, "revoked": True, "changed": False}
        await conn.execute(
            "UPDATE ohvis_action_grants SET revoked_at=now(), revoked_reason=$2, revision=revision+1 WHERE id=$1",
            row["id"], reason)
        blocked = await conn.execute(
            "UPDATE ohvis_action_outbox SET state='blocked', last_error='grant_revoked', updated_at=now() "
            "WHERE grant_id=$1 AND state IN ('pending','authorized')", row["id"])
        await audit(conn, tenant, actor, "grant_revoked",
                    {"grant_id": grant_id, "effects_blocked": blocked}, reason)
        return {"id": grant_id, "revoked": True, "changed": True}


async def list_grants(conn: Any, tenant: str, project: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, grant_type, root_task_id, generation_id, commit_sha, target_hash, expires_at, revoked_at, "
        "revision, issuer FROM ohvis_action_grants WHERE tenant_id=$1::uuid AND project_key=$2 "
        "ORDER BY created_at DESC LIMIT 100", tenant, project)
    return [{**{k: v for k, v in dict(r).items() if k not in ("id", "expires_at", "revoked_at")},
             "id": str(r["id"]), "expires_at": r["expires_at"].isoformat(),
             "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None} for r in rows]


# --------------------------------------------------------------------------------------------- effects

def authorize_effect(*, action: str, grant: dict[str, Any] | None, inbox: dict[str, Any], review: dict[str, Any],
                     effect: dict[str, Any], paused: bool, flags: Flags, counted_effects: int,
                     adapter_idempotent: bool, now: datetime) -> tuple[bool, str, bool]:
    """Deterministic, fail-closed gate -> (allowed, reason, permanent). `permanent` denials block the effect;
    transient ones (paused, flag off, budget) leave it pending so a later pass can proceed once lifted."""
    if action not in SUPPORTED_ACTIONS:
        return False, "action_has_no_adapter", True
    if not flags.enabled:
        return False, "feature_disabled", False
    if not flags.auto_effect:
        return False, "auto_effect_off", False
    if paused:
        return False, "project_paused", False
    if flags.max_effects_per_root is None:
        return False, "effect_budget_unset", False
    if counted_effects >= flags.max_effects_per_root:
        return False, "effect_budget_exhausted", True
    if not adapter_idempotent:
        return False, "adapter_without_idempotent_lookup", True
    if review["decision"] != "verified" or not review["code_verified"]:
        return False, "review_not_verified", True
    if effect["owner_epoch"] != inbox["owner_epoch"]:
        return False, "stale_epoch", True
    if inbox["state"] not in ("reviewed",):
        return False, "inbox_not_reviewed", True
    if grant is None:
        return False, "grant_missing", True
    if grant["grant_type"] != ACTION_REQUIRES_GRANT.get(action):
        return False, "grant_type_mismatch", True
    if grant["revoked_at"] is not None:
        return False, "grant_revoked", True
    if grant["expires_at"] <= now:
        return False, "grant_expired", True
    if grant["revision"] != effect["grant_revision"]:
        return False, "grant_revision_changed", True
    if str(grant["tenant_id"]) != str(effect["tenant_id"]) or grant["project_key"] != effect["project_key"] \
            or grant["root_task_id"] != effect["root_task_id"]:
        return False, "grant_scope_mismatch", True
    if grant["commit_sha"] and grant["commit_sha"] != inbox["commit_sha"]:
        return False, "commit_mismatch", True
    if grant["generation_id"] and grant["generation_id"] != inbox["generation_id"]:
        return False, "generation_mismatch", True
    if not grant["commit_sha"] and not grant["generation_id"]:
        return False, "grant_unbound", True
    if sha256_hex(_loads(grant["scope"])["job_spec"]) != grant["target_hash"] \
            or grant["target_hash"] != sha256_hex(_loads(effect["payload"])["job_spec"]):
        return False, "target_hash_mismatch", True
    return True, "ok", False


async def plan_effects(conn: Any, inbox: dict[str, Any], review_id: int) -> list[str]:
    """Create outbox rows for matching grants. Planning authorizes nothing; the gate runs again at dispatch."""
    grants = await conn.fetch(
        "SELECT * FROM ohvis_action_grants WHERE tenant_id=$1 AND project_key=$2 AND root_task_id=$3 "
        "AND grant_type='execute_followup' AND revoked_at IS NULL AND expires_at > now()",
        inbox["tenant_id"], inbox["project_key"], inbox["root_task_id"])
    planned: list[str] = []
    for g in grants:
        scope = _loads(g["scope"])
        if scope["after_event_type"] != inbox["event_type"]:
            continue
        if g["commit_sha"] and g["commit_sha"] != inbox["commit_sha"]:
            continue
        if g["generation_id"] and g["generation_id"] != inbox["generation_id"]:
            continue
        payload = {"job_spec": scope["job_spec"], "after_inbox_id": str(inbox["id"]),
                   "allowed_files": scope["allowed_files"]}
        key = effect_key(str(inbox["tenant_id"]), inbox["project_key"], inbox["root_task_id"], "submit_followup_job",
                         g["target_hash"], g["generation_id"] or g["commit_sha"])
        row = await conn.fetchrow(
            "INSERT INTO ohvis_action_outbox (tenant_id, inbox_id, review_id, project_key, root_task_id, effect_key, "
            "action, payload, payload_sha256, grant_id, grant_revision, owner_epoch, state) "
            "VALUES ($1,$2,$3,$4,$5,$6,'submit_followup_job',$7::jsonb,$8,$9,$10,$11,'pending') "
            "ON CONFLICT (tenant_id, effect_key) DO NOTHING RETURNING id",
            inbox["tenant_id"], inbox["id"], review_id, inbox["project_key"], inbox["root_task_id"], key,
            _json(payload), sha256_hex(payload), g["id"], g["revision"], inbox["owner_epoch"])
        if row is not None:
            planned.append(str(row["id"]))
    return planned


class EffectAdapter(Protocol):
    supports_idempotent_lookup: bool

    async def submit(self, effect_key: str, spec: dict[str, Any]) -> dict[str, Any]: ...

    async def lookup(self, effect_key: str) -> dict[str, Any]: ...


class NullAdapter:
    """Default: refuses everything. A real runner adapter needs an approved integration with the shared submit path."""

    supports_idempotent_lookup = False

    async def submit(self, effect_key: str, spec: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("no adapter configured")

    async def lookup(self, effect_key: str) -> dict[str, Any]:
        return {"status": "unavailable"}


async def dispatch_effect(pool: Any, effect_id: str, adapter: EffectAdapter | None = None, *,
                          slot_active: Callable[[], bool] | None = None) -> dict[str, Any]:
    adapter = adapter or NullAdapter()
    if not (slot_active or default_slot_active)():
        return {"state": "pending", "dispatched": False, "reason": "standby_slot"}
    async with pool.acquire() as conn:
        async with conn.transaction():
            eff = await conn.fetchrow(
                "SELECT * FROM ohvis_action_outbox WHERE id=$1::uuid FOR UPDATE", effect_id)
            if eff is None:
                raise MainChatError(404, "effect_not_found")
            if eff["state"] not in ("pending", "authorized"):
                return {"state": eff["state"], "dispatched": False, "reason": "not_dispatchable"}
            tenant = str(eff["tenant_id"])
            inbox = await conn.fetchrow("SELECT * FROM ohvis_report_inbox WHERE id=$1 FOR SHARE", eff["inbox_id"])
            review = await conn.fetchrow("SELECT * FROM ohvis_report_reviews WHERE id=$1", eff["review_id"])
            grant = await conn.fetchrow("SELECT * FROM ohvis_action_grants WHERE id=$1 FOR SHARE", eff["grant_id"])
            paused = bool(await conn.fetchval(
                "SELECT paused FROM ohvis_main_chat_control WHERE tenant_id=$1 AND project_key=$2",
                eff["tenant_id"], eff["project_key"]))
            counted = await conn.fetchval(
                "SELECT count(*) FROM ohvis_action_outbox WHERE tenant_id=$1 AND project_key=$2 AND root_task_id=$3 "
                "AND state = ANY($4::text[])", eff["tenant_id"], eff["project_key"], eff["root_task_id"],
                list(EFFECT_COUNTED_STATES))
            allowed, reason, permanent = authorize_effect(
                action=eff["action"], grant=dict(grant) if grant else None, inbox=dict(inbox), review=dict(review),
                effect=dict(eff), paused=paused, flags=load_flags(), counted_effects=counted,
                adapter_idempotent=bool(getattr(adapter, "supports_idempotent_lookup", False)), now=_now())
            if not allowed:
                if permanent:
                    await conn.execute(
                        "UPDATE ohvis_action_outbox SET state='blocked', last_error=$2, updated_at=now() WHERE id=$1",
                        eff["id"], reason)
                    await audit(conn, tenant, "ohvis-main-chat", "effect_blocked",
                                {"effect_id": effect_id, "reason": reason})
                    await notice(conn, tenant, eff["project_key"],
                                 await _route_owner(conn, eff), "blocked", eff["inbox_id"],
                                 f"자동 실행 차단: {eff['root_task_id']}", reason, f"effect_blocked:{eff['id']}")
                    return {"state": "blocked", "dispatched": False, "reason": reason}
                return {"state": eff["state"], "dispatched": False, "reason": reason}
            await conn.execute(
                "UPDATE ohvis_action_outbox SET state='dispatching', dispatch_attempts=dispatch_attempts+1, "
                "updated_at=now() WHERE id=$1", eff["id"])
            spec = _loads(eff["payload"])["job_spec"]
            key = eff["effect_key"]
    # network happens outside the transaction; nothing is held open while the adapter runs
    try:
        result = await asyncio.wait_for(adapter.submit(key, spec), timeout=DISPATCH_TIMEOUT_SECONDS)
        remote = str(result.get("remote_ref") or "")
        outcome, error = ("confirmed", None) if remote else ("unknown", "adapter_returned_no_remote_ref")
    except Exception as exc:  # timeout and transport failures are ambiguous: the job may exist remotely
        outcome, remote, error = "unknown", "", f"{type(exc).__name__}"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE ohvis_action_outbox SET state=$2, remote_ref=NULLIF($3,''), last_error=$4, updated_at=now() "
                "WHERE id=$1 AND state='dispatching'", eff["id"], outcome, remote, error)
            await audit(conn, tenant, "ohvis-main-chat", f"effect_{outcome}",
                        {"effect_id": effect_id, "effect_key": key, "remote_ref": remote or None})
            await notice(conn, tenant, eff["project_key"], await _route_owner(conn, eff), "effect_result",
                         eff["inbox_id"], f"후속 작업 {outcome}: {eff['root_task_id']}", remote or (error or ""),
                         f"effect:{eff['id']}:{outcome}")
    return {"state": outcome, "dispatched": True, "remote_ref": remote or None}


async def _route_owner(conn: Any, eff: Any) -> str | None:
    return await conn.fetchval(
        "SELECT updated_by FROM ohvis_main_chat_routes WHERE tenant_id=$1 AND project_key=$2 "
        "ORDER BY goal_id NULLS LAST LIMIT 1", eff["tenant_id"], eff["project_key"])


async def recover_unknown(pool: Any, adapter: EffectAdapter | None = None, *,
                          slot_active: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    """Resolve effects whose outcome is ambiguous by asking the adapter; never by sending again blindly."""
    adapter = adapter or NullAdapter()
    if not getattr(adapter, "supports_idempotent_lookup", False) or not (slot_active or default_slot_active)():
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, tenant_id, effect_key FROM ohvis_action_outbox WHERE state='unknown' "
            "OR (state='dispatching' AND updated_at < $1)", _now() - STALE_DISPATCHING)
    results = []
    for r in rows:
        try:
            found = await asyncio.wait_for(adapter.lookup(r["effect_key"]), timeout=DISPATCH_TIMEOUT_SECONDS)
            status = found.get("status")
        except Exception:
            status = "unavailable"
        if status == "found" and found.get("remote_ref"):
            new_state, remote = "confirmed", str(found["remote_ref"])
        elif status == "not_found":
            new_state, remote = "authorized", None
        else:
            results.append({"id": str(r["id"]), "state": "unknown", "lookup": "unavailable"})
            continue
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE ohvis_action_outbox SET state=$2, remote_ref=coalesce($3, remote_ref), updated_at=now() "
                    "WHERE id=$1 AND state IN ('unknown','dispatching')", r["id"], new_state, remote)
                await audit(conn, str(r["tenant_id"]), "ohvis-main-chat", f"effect_recovered_{new_state}",
                            {"effect_id": str(r["id"]), "lookup": status})
        results.append({"id": str(r["id"]), "state": new_state, "lookup": status})
    return results


# --------------------------------------------------------------------------------------------- control

async def get_control(conn: Any, tenant: str, project: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT paused, revision, reason, updated_by FROM ohvis_main_chat_control WHERE tenant_id=$1::uuid "
        "AND project_key=$2", tenant, project)
    if row is None:
        return {"project_key": project, "paused": False, "revision": 0, "reason": "", "updated_by": None}
    return {"project_key": project, **dict(row)}


async def set_control(conn: Any, tenant: str, actor: str, body: ControlRequest) -> dict[str, Any]:
    """Pause stops claims and new dispatches; reports are still stored. Resume never replays anything by itself."""
    paused = body.action == "pause"
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM ohvis_main_chat_control WHERE tenant_id=$1::uuid AND project_key=$2 FOR UPDATE",
            tenant, body.project_key)
        if row is None:
            if body.expected_revision != 0:
                raise MainChatError(409, "revision_conflict", current_revision=0)
            await conn.execute(
                "INSERT INTO ohvis_main_chat_control (tenant_id, project_key, paused, reason, updated_by) "
                "VALUES ($1::uuid,$2,$3,$4,$5)", tenant, body.project_key, paused, body.reason, actor)
        else:
            if body.expected_revision != row["revision"]:
                raise MainChatError(409, "revision_conflict", current_revision=row["revision"])
            await conn.execute(
                "UPDATE ohvis_main_chat_control SET paused=$3, reason=$4, revision=revision+1, updated_by=$5, "
                "updated_at=now() WHERE tenant_id=$1::uuid AND project_key=$2",
                tenant, body.project_key, paused, body.reason, actor)
        await audit(conn, tenant, actor, f"control_{body.action}", {"project": body.project_key}, body.reason)
    return await get_control(conn, tenant, body.project_key)


# --------------------------------------------------------------------------------------------- views

def card_label(state: str, decision: str | None) -> str:
    key = decision if state == "reviewed" and decision else state
    if state == "pending_review":
        key = "report_arrived"
    return CARD_LABELS.get(key, "보고 도착")


async def list_cards(conn: Any, tenant: str, project: str, limit: int = 50) -> dict[str, Any]:
    """Cards are derived from the ledger on every read. They report what was received and checked, never "done"."""
    rows = await conn.fetch(
        "SELECT i.id, i.state, i.event_type, i.root_task_id, i.source_revision, i.runner_job_id, i.commit_sha, "
        "i.received_at, r.decision, r.code_verified, r.reasons "
        "FROM ohvis_report_inbox i LEFT JOIN LATERAL (SELECT * FROM ohvis_report_reviews x WHERE x.inbox_id=i.id "
        "ORDER BY review_revision DESC LIMIT 1) r ON true "
        "WHERE i.tenant_id=$1::uuid AND i.project_key=$2 ORDER BY i.received_at DESC LIMIT $3",
        tenant, project, max(1, min(limit, 200)))
    effects = await conn.fetch(
        "SELECT inbox_id, id, state, remote_ref, last_error FROM ohvis_action_outbox WHERE tenant_id=$1::uuid "
        "AND project_key=$2 AND inbox_id = ANY($3::uuid[])", tenant, project, [r["id"] for r in rows])
    by_inbox: dict[Any, list[dict[str, Any]]] = {}
    for e in effects:
        by_inbox.setdefault(e["inbox_id"], []).append(
            {"id": str(e["id"]), "state": e["state"], "last_error": e["last_error"],
             "result_link": f"/pipeline/jobs/{e['remote_ref']}" if e["remote_ref"] else None})
    cards = [{
        "id": str(r["id"]), "state": r["state"], "label": card_label(r["state"], r["decision"]),
        "event_type": r["event_type"], "root_task_id": r["root_task_id"], "source_revision": r["source_revision"],
        "decision": r["decision"], "reasons": _loads(r["reasons"]) if r["reasons"] else [],
        "badges": {"report_received": True, "code_verified": bool(r["code_verified"]), "tests_rechecked": False},
        "received_at": r["received_at"].isoformat(), "effects": by_inbox.get(r["id"], []),
        "link": f"/projects/{project}/main-chat?inbox={r['id']}",
    } for r in rows]
    return {"project_key": project, "control": await get_control(conn, tenant, project),
            "routes": await list_routes(conn, tenant, project), "cards": cards}


async def list_notices(conn: Any, tenant: str, user: str, unread_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, project_key, kind, title, body, link, created_at, read_at FROM ohvis_main_chat_notices "
        "WHERE tenant_id=$1::uuid AND recipient_user_id=$2 AND ($3 = false OR read_at IS NULL) "
        "ORDER BY created_at DESC LIMIT $4", tenant, user, unread_only, max(1, min(limit, 200)))
    return [{**dict(r), "created_at": r["created_at"].isoformat(),
             "read_at": r["read_at"].isoformat() if r["read_at"] else None} for r in rows]


async def mark_notice_read(conn: Any, tenant: str, user: str, notice_id: int) -> bool:
    result = await conn.execute(
        "UPDATE ohvis_main_chat_notices SET read_at=coalesce(read_at, now()) WHERE id=$1 AND tenant_id=$2::uuid "
        "AND recipient_user_id=$3", notice_id, tenant, user)
    return result.endswith(" 1")


async def run_once(pool: Any, instance: str, *, adapter: EffectAdapter | None = None,
                   evidence: EvidenceProvider | None = None,
                   slot_active: Callable[[], bool] | None = None) -> dict[str, Any]:
    """One worker pass: lease a report, review it, then offer its pending effects to the gate. Not scheduled anywhere."""
    async with pool.acquire() as conn:
        claimed = await claim_next(conn, instance, slot_active=slot_active)
    if claimed is None:
        return {"claimed": False}
    data = None
    if evidence is not None:
        data = await evidence(claimed)
    async with pool.acquire() as conn:
        outcome = await record_review(conn, str(claimed["id"]), claimed["owner_epoch"], data)
    dispatched = [await dispatch_effect(pool, effect_id, adapter, slot_active=slot_active) for effect_id in outcome.get("planned_effects", [])]
    return {"claimed": True, "inbox_id": str(claimed["id"]), "review": outcome, "effects": dispatched}
