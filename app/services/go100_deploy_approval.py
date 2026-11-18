"""GO100 immutable approval authority. All authority comes from database rows."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

LANES = {"backend": "api_only", "frontend": "frontend"}
FIELDS = ("id", "run_id", "approved_by", "tenant_id", "source_job_id", "source_session_id",
          "source_tuple", "project", "component", "lane", "release_sha", "target_env",
          "issued_at", "expires_at", "schema_version")


class ApprovalDenied(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise ApprovalDenied(reason)


def snapshot_hash(snapshot):
    values = {key: snapshot[key] for key in FIELDS}
    for key, value in values.items():
        if isinstance(value, datetime):
            values[key] = value.astimezone(timezone.utc).isoformat()
        elif isinstance(value, uuid.UUID):
            values[key] = str(value)
    if isinstance(values["source_tuple"], str):
        values["source_tuple"] = json.loads(values["source_tuple"])
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


async def source_tuple(conn, run):
    _require(run.get("runner_job_id"), "legacy_unbound")
    job = await conn.fetchrow("SELECT * FROM pipeline_jobs WHERE job_id=$1 FOR SHARE", run["runner_job_id"])
    _require(job is not None, "source_job_missing")
    job = dict(job)
    _require(job.get("project") == run["project"] == "GO100", "source_project_mismatch")
    sha = run.get("release_sha") or ""
    _require(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid_release_sha")
    _require(job.get("commit_hash") == sha, "source_sha_mismatch")
    _require(job.get("tenant_id") and job.get("chat_session_id"), "source_provenance_missing")
    try:
        session_id = uuid.UUID(str(job["chat_session_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ApprovalDenied("source_session_invalid_uuid") from None
    _require(str(run.get("chat_session_id") or "") == str(session_id), "source_session_mismatch")
    session = await conn.fetchrow("SELECT id, tenant_id, user_id FROM chat_sessions WHERE id=$1::uuid FOR SHARE",
                                  str(session_id))
    _require(session is not None, "source_session_missing")
    _require(str(session["tenant_id"]) == str(job["tenant_id"]), "source_tenant_mismatch")
    _require(session["user_id"] and run.get("requested_by") == session["user_id"], "requester_provenance_mismatch")
    return {"job_id": job["job_id"], "project": job["project"], "tenant_id": str(job["tenant_id"]),
            "chat_session_id": str(job["chat_session_id"]), "commit_hash": job["commit_hash"],
            "requested_by": run["requested_by"], "session_user_id": session["user_id"]}


def _match_run_fields(run, snapshot):
    for field in ("project", "component", "release_sha", "target_env"):
        _require(run.get(field) == snapshot[field], "run_" + field + "_mismatch")
    _require(LANES.get(run["component"]) == snapshot["lane"], "lane_mismatch")


def _match_run(run, snapshot, source):
    _match_run_fields(run, snapshot)
    _require(str(snapshot["tenant_id"]) == source["tenant_id"], "tenant_mismatch")
    _require(snapshot["source_job_id"] == source["job_id"], "source_job_mismatch")
    _require(str(snapshot["source_session_id"]) == source["chat_session_id"], "source_session_mismatch")
    original = snapshot["source_tuple"]
    if isinstance(original, str):
        original = json.loads(original)
    _require(original == source, "source_tuple_mismatch")
    payload = run.get("request_payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    _require(payload.get("lane", snapshot["lane"]) == snapshot["lane"], "lane_mismatch")


def _valid_snapshot(snapshot, now):
    _require(snapshot["schema_version"] == 1, "schema_version_mismatch")
    _require(snapshot_hash(snapshot) == snapshot["snapshot_hash"], "snapshot_hash_mismatch")
    _require(snapshot["issued_at"] <= now < snapshot["expires_at"], "approval_expired_or_future")


async def _approval_not_revoked(conn, run):
    _require(run.get("approval_policy") == "manual_approved", "approval_policy_revoked")
    revoked = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM deploy_phase_events
        WHERE deploy_run_id=$1 AND phase IN ('approval_revoked','revoked'))""", run["id"])
    _require(not revoked, "approval_revoked")


async def approve(conn, run_id, principal):
    _require(os.getenv("AADS_GO100_DEPLOY_APPROVAL_ENABLED", "true").lower() not in ("false", "0", "off"),
             "go100_approval_disabled")
    _require(isinstance(principal, dict) and principal.get("is_internal_admin") and principal.get("user_id"),
             "authenticated_admin_required")
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM deploy_runs WHERE id=$1 FOR UPDATE", run_id)
        _require(row is not None, "run_not_found")
        run = dict(row)
        _require(run["project"] == "GO100", "project_mismatch")
        source = await source_tuple(conn, run)
        _require(source["session_user_id"] == principal["user_id"], "authenticated_requester_mismatch")
        lane = LANES.get(run["component"])
        _require(lane is not None, "component_not_allowed")
        now = await conn.fetchval("SELECT clock_timestamp()")
        existing = await conn.fetchrow("SELECT * FROM go100_deploy_approval_snapshots WHERE run_id=$1", run_id)
        if existing:
            snapshot = dict(existing)
            await _approval_not_revoked(conn, run)
            _require(run["status"] == "queued" and snapshot["approved_by"] == principal["user_id"], "approval_conflict")
            _valid_snapshot(snapshot, now)
            _match_run(run, snapshot, source)
        else:
            _require(run["status"] == "awaiting_approval", "not_awaiting_approval")
            snapshot = dict(id=uuid.uuid4(), run_id=run_id, approved_by=principal["user_id"],
                            tenant_id=uuid.UUID(source["tenant_id"]), source_job_id=source["job_id"],
                            source_session_id=uuid.UUID(source["chat_session_id"]), source_tuple=source,
                            project="GO100", component=run["component"], lane=lane,
                            release_sha=run["release_sha"], target_env=run["target_env"],
                            issued_at=now, expires_at=now + timedelta(hours=1), schema_version=1)
            _match_run(run, snapshot, source)
            snapshot["snapshot_hash"] = snapshot_hash(snapshot)
            await conn.execute("""INSERT INTO go100_deploy_approval_snapshots
                (id,run_id,approved_by,tenant_id,source_job_id,source_session_id,source_tuple,
                 project,component,lane,release_sha,target_env,issued_at,expires_at,schema_version,snapshot_hash)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
                *[json.dumps(snapshot[k]) if k == "source_tuple" else snapshot[k]
                  for k in (*FIELDS, "snapshot_hash")])
            await conn.execute("""UPDATE deploy_runs SET status='queued', phase='queued_for_deploy',
                approval_policy='manual_approved', updated_at=NOW() WHERE id=$1""", run_id)
            from app.services.deploy_control import _audit_phase_event
            await _audit_phase_event(conn, run_id, phase="approved", status="queued", detail="GO100 immutable approval",
                                     metadata={"actor": principal["user_id"], "approval_id": str(snapshot["id"]),
                                               "lane": lane, "component": run["component"],
                                               "scope": "backend API only; excludes cards and batch" if lane == "api_only" else "frontend"})
        return {"ok": True, "status": "queued", "deploy_run_id": run_id,
                "approval_id": str(snapshot["id"]), "approved_by": snapshot["approved_by"],
                "tenant_id": str(snapshot["tenant_id"]), "component": run["component"], "lane": lane,
                "scope": "backend API only; excludes cards and batch" if lane == "api_only" else "frontend"}


async def validate_contract(conn, run_id, owner_instance, owner_epoch):
    """Return a schema v1 contract only while DB approval and exact lease are live."""
    _require(os.getenv("AADS_GO100_DEPLOY_APPROVAL_ENABLED", "true").lower() not in ("false", "0", "off"),
             "go100_approval_disabled")
    _require(isinstance(owner_epoch, str) and bool(owner_epoch), "live_lease_required")
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM deploy_runs WHERE id=$1 FOR SHARE", run_id)
        _require(row is not None, "run_not_found")
        run = dict(row)
        _require(run["project"] == "GO100", "project_mismatch")
        _require(run["status"] in ("queued", "running", "verifying", "syncing_standby"), "run_not_executable")
        row = await conn.fetchrow("SELECT * FROM go100_deploy_approval_snapshots WHERE run_id=$1", run_id)
        _require(row is not None, "approval_missing")
        snapshot = dict(row)
        await _approval_not_revoked(conn, run)
        _match_run_fields(run, snapshot)
        source = await source_tuple(conn, run)
        _match_run(run, snapshot, source)
        lock = await conn.fetchrow("""SELECT * FROM deploy_locks WHERE project=$1 AND component=$2
            AND target_env=$3 AND deploy_run_id=$4 AND owner_instance=$5 AND owner_epoch=$6
            AND lease_expires_at > clock_timestamp() FOR SHARE""", run["project"], run["component"],
            run["target_env"], run_id, owner_instance, str(owner_epoch))
        _require(bool(owner_instance) and bool(str(owner_epoch)) and lock is not None, "live_lease_required")
        now = await conn.fetchval("SELECT clock_timestamp()")
        _require(lock["lease_expires_at"] > now, "live_lease_required")
        _valid_snapshot(snapshot, now)
        result = {key: snapshot[key] for key in ("schema_version", "run_id", "project", "component", "lane",
                  "release_sha", "target_env", "tenant_id", "approved_by", "issued_at", "expires_at", "snapshot_hash")}
        result.update(approval_id=snapshot["id"], owner_instance=owner_instance, owner_epoch=str(owner_epoch))
        return json.loads(json.dumps(result, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v)))
