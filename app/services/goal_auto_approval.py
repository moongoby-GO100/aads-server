"""목표 자동승인 판정·소비·기록 — live_trading_guard.goal_policy_allows 의 부품.

정본: docs/prd/20261015_AADS_목표_우선순위_휴면정리_자동승인표시.md §5 AA-1~AA-7.

판정 근거는 `goal_auto_approval_grants`(정본)다. 목표에 게이트용 grant 가 하나도
없으면(이관 전, 또는 이관 되돌림 후) `goals.approval_policy` 를 읽되 **같은 목표 조건**
을 건다 — 호환 경로가 더 느슨하면 이관을 미루는 쪽이 유리해진다.

    목표 status = active · 일시정지 아님 · 휴면 아님      (AA-2)
    grant status = active · 유효기간 안 · used < max      (AA-3·AA-4)
    위험 등급 ≤ grant 상한 (high=A1, critical=A2)

통과하면 그 자리에서 used 를 +1 한다(AA-7). 조건부 UPDATE 한 문장으로 세므로
동시 호출이 상한을 넘겨 쓰지 못한다. 판정은 통과·거부 모두 goal_policy_decisions
에 남긴다(AA-5).
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import structlog

from app.services import goal_signals as signals

logger = structlog.get_logger(__name__)

SOURCE_GRANT = "grant"
SOURCE_POLICY = "approval_policy"

# 이관 마이그레이션이 테넌트마다 만드는 다리 정책 버전의 프로젝트 표지.
# 실제 프로젝트 이름과 겹치지 않아 W-14 정책 조회(project=$2 OR NULL)에 잡히지 않는다.
BRIDGE_POLICY_PROJECT = "LEGACY_APPROVAL_POLICY"

_RISK_TIER_ORDER = {"A0": 0, "A1": 1, "A2": 2, "A3": 3}


def risk_tier(risk_level: str) -> str:
    """게이트 위험 등급 → grant 위험 상한. 주문·자금(critical)=A2, 그 외 고위험=A1."""
    return "A2" if (risk_level or "") == "critical" else "A1"


@dataclass
class Candidate:
    goal_id: str
    title: str
    status: str
    project: str
    tenant_id: str
    paused_at: Any
    policy: dict[str, Any]
    grant: Optional[dict[str, Any]] = None
    last_activity_at: Any = None
    dormant: bool = False
    dormant_reason: Optional[str] = None

    @property
    def source(self) -> str:
        return SOURCE_GRANT if self.grant else SOURCE_POLICY


@dataclass
class Verdict:
    allowed: bool
    source: str
    reasons: list[str] = field(default_factory=list)
    used: int = 0
    max: int = 0


def _int(value: Any) -> int:
    return signals._int(value)


def has_gate_policy(candidate: Candidate) -> bool:
    """판정할 거리가 있는가. grant 도 자동승인 키도 없으면 수동 목표라 기록하지 않는다."""
    if candidate.grant:
        return True
    return "auto_approve_high" in candidate.policy or "auto_approve_critical" in candidate.policy


def evaluate(candidate: Candidate, risk_level: str, *, now: Optional[datetime] = None) -> Verdict:
    """순수 판정. 거부 사유를 모두 모은다(첫 사유만 남기면 정리할 때 다시 조사해야 한다)."""
    current = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    status = str(candidate.status or "").lower()
    if status != "active":
        reasons.append(f"goal_status_{status or 'unknown'}")
    if candidate.paused_at is not None:
        reasons.append("goal_paused")
    if candidate.dormant:
        reasons.append(f"goal_dormant_{candidate.dormant_reason or 'unknown'}")

    if candidate.grant:
        grant = candidate.grant
        used, max_exec = _int(grant.get("used_executions")), _int(grant.get("max_executions"))
        grant_status = str(grant.get("status") or "").lower()
        if grant_status != "active":
            reasons.append(f"grant_{grant_status or 'unknown'}")
        valid_from = signals._aware(grant.get("valid_from"))
        expires_at = signals._aware(grant.get("expires_at"))
        if valid_from is not None and current < valid_from:
            reasons.append("grant_not_yet_valid")
        if expires_at is None or current >= expires_at:
            reasons.append("grant_expired")
        ceiling = _RISK_TIER_ORDER.get(str(grant.get("max_risk_tier") or ""), -1)
        if _RISK_TIER_ORDER[risk_tier(risk_level)] > ceiling:
            reasons.append("risk_tier_exceeds_grant")
        if used >= max_exec:
            reasons.append("grant_exhausted")
        return Verdict(not reasons, SOURCE_GRANT, reasons, used, max_exec)

    policy = candidate.policy
    field_name = "auto_approve_critical" if risk_level == "critical" else "auto_approve_high"
    used, max_exec = _int(policy.get("used")), _int(policy.get("max_executions"))
    if not signals._flag(policy.get(field_name)):
        reasons.append("policy_tier_not_enabled")
    expires_at = signals._aware(policy.get("expires_at"))
    if expires_at is not None and current >= expires_at:
        reasons.append("policy_expired")
    if used >= max_exec:
        reasons.append("policy_exhausted")
    return Verdict(not reasons, SOURCE_POLICY, reasons, used, max_exec)


async def attach_signals(conn: Any, candidates: list[Candidate]) -> None:
    """grant 와 휴면 근거를 붙인다. 실패하면 예외를 그대로 올린다 — 근거 없이 열지 않는다."""
    ids = [c.goal_id for c in candidates]
    grants = await signals.fetch_latest_gate_grants(conn, ids)
    activity = await signals.fetch_activity(
        conn, ids, scan_days=signals.GATE_INSTRUCTION_SCAN_DAYS,
    )
    for c in candidates:
        c.grant = grants.get(c.goal_id)
        act = activity.get(c.goal_id) or {}
        c.last_activity_at = act.get("last_activity_at")
        c.dormant, c.dormant_reason = signals.derive_dormancy(
            c.status, c.last_activity_at,
            has_block_reason=signals.has_block_reason(act.get("block_note"), None),
        )


async def consume(conn: Any, candidate: Candidate) -> Optional[tuple[int, int]]:
    """사용 1회를 센다. 센 뒤의 (used, max). 못 셌으면 None — 호출자는 거부로 다룬다.

    AA-7. 조건(상태·기간·상한)을 UPDATE 의 WHERE 에 같이 건다. 읽고 나서 따로 쓰면
    그 사이 다른 호출이 마지막 1회를 가져가도 둘 다 통과한다.
    """
    if candidate.grant:
        row = await conn.fetchrow(
            """
            UPDATE goal_auto_approval_grants
               SET used_executions = used_executions + 1,
                   last_used_at = clock_timestamp()
             WHERE id = $1::uuid AND status = 'active'
               AND used_executions < max_executions
               AND clock_timestamp() >= valid_from AND clock_timestamp() < expires_at
            RETURNING used_executions, max_executions
            """,
            candidate.grant["id"],
        )
        if not row:
            return None
        used, max_exec = int(row["used_executions"]), int(row["max_executions"])
        candidate.grant["used_executions"] = used
        # approval_policy 는 이관 후 읽기 전용 호환 뷰다. 예전 화면·제안 카드
        # (next_step_proposals)가 아직 이 값을 읽으므로 숫자만 맞춰 둔다.
        try:
            await conn.execute(
                "UPDATE goals SET approval_policy = jsonb_set(approval_policy, '{used}', "
                "       to_jsonb($2::int), true) "
                " WHERE id = $1::uuid "
                "   AND approval_policy ?| ARRAY['auto_approve_high','auto_approve_critical']",
                candidate.goal_id, used,
            )
        except Exception as exc:  # noqa: BLE001 — 호환 뷰 실패가 소비를 무르지 않는다
            logger.warning("goal_policy_mirror_failed", goal=candidate.goal_id[:8], error=str(exc)[:140])
        return used, max_exec

    row = await conn.fetchrow(
        """
        UPDATE goals
           SET approval_policy = jsonb_set(approval_policy, '{used}',
                   to_jsonb(COALESCE((approval_policy->>'used')::int, 0) + 1), true),
               updated_at = NOW()
         WHERE id = $1::uuid
           AND COALESCE((approval_policy->>'used')::int, 0)
               < COALESCE((approval_policy->>'max_executions')::int, 0)
        RETURNING COALESCE((approval_policy->>'used')::int, 0) AS used,
                  COALESCE((approval_policy->>'max_executions')::int, 0) AS max_exec
        """,
        candidate.goal_id,
    )
    if not row:
        return None
    candidate.policy["used"] = int(row["used"])
    return int(row["used"]), int(row["max_exec"])


# ── 판정 기록 (AA-5) ─────────────────────────────────────────────────────────
def _signing_key():
    """W-14F 서명 키. 없으면 None — 감사 전용 무서명 기록으로 남긴다.

    무서명 기록은 signature_algorithm 이 HMAC 이 아니므로
    verify_immediately_before_execution 이 실행 근거로 받아들이지 않는다.
    """
    from app.services.goal_policy_foundation import SigningKey

    secret = os.getenv("GOAL_POLICY_DECISION_SIGNING_SECRET", "").encode()
    if len(secret) < 32:
        return None
    try:
        return SigningKey(
            os.getenv("GOAL_POLICY_DECISION_SIGNING_KEY_ID", "live-trading-gate"),
            int(os.getenv("GOAL_POLICY_DECISION_SIGNING_KEY_VERSION", "1") or 1),
            secret,
        )
    except ValueError:
        return None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


async def _policy_version_for(conn: Any, candidate: Candidate) -> Optional[str]:
    if candidate.grant and candidate.grant.get("policy_version"):
        return str(candidate.grant["policy_version"])
    return await conn.fetchval(
        "SELECT id::text FROM goal_approval_policy_versions "
        "WHERE tenant_id = $1::uuid AND project = $2 ORDER BY created_at LIMIT 1",
        candidate.tenant_id, BRIDGE_POLICY_PROJECT,
    )


def build_decision(
    candidate: Candidate,
    verdict: Verdict,
    *,
    session_id: str,
    tool_name: str,
    risk_level: str,
    target_project: str,
    policy_version: str,
    decided_at: datetime,
    signing_key: Any = None,
) -> dict[str, Any]:
    """goal_policy_decisions 한 행. 순수 함수 — 테스트가 열(column) 계약을 직접 본다."""
    from app.services import goal_policy_foundation as foundation

    result = "AUTO" if verdict.allowed else "APPROVAL_REQUIRED"
    decision_id = str(uuid.uuid4())
    grant = candidate.grant or {}
    matched_grant_id = str(grant["id"]) if grant.get("id") else None
    grant_version = int(grant.get("grant_version") or 1) if matched_grant_id else None
    snapshot = {
        "goal_status": candidate.status, "paused": candidate.paused_at is not None,
        "dormant": candidate.dormant, "dormant_reason": candidate.dormant_reason,
        "last_activity_at": signals._iso(candidate.last_activity_at),
        "source": verdict.source, "used": verdict.used, "max": verdict.max,
        "grant_status": grant.get("status"),
        "grant_expires_at": signals._iso(grant.get("expires_at")),
    }
    decided_iso = decided_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    row: dict[str, Any] = {
        "decision_id": decision_id,
        "tenant_id": candidate.tenant_id,
        "project": candidate.project,
        "workspace_kind": "project",
        "principal_session_id": session_id,
        "assignment_id": None,
        "target_type": "goal",
        "target_id": candidate.goal_id,
        "action": f"{signals.GATE_GRANT_ACTION}:{tool_name}",
        "base_version": 1,
        "target_version": 1,
        # 게이트가 보는 도구는 운영 코드·운영 DB 를 바꾸는 것들이다.
        "environment": "production",
        "boundary_decision": "ALLOW",
        "approval_route": "NONE" if verdict.allowed else "CEO_APPROVAL",
        "automation_eligibility": "GRANT_REQUIRED",
        "risk_tier": risk_tier(risk_level),
        "result": result,
        "reason_codes": (["auto_approved"] if verdict.allowed else []) + list(verdict.reasons),
        "policy_version": policy_version,
        "matched_grant_id": matched_grant_id,
        "grant_version": grant_version,
        "remaining_uses": max(verdict.max - verdict.used, 0),
        "decision_input_hash": foundation.canonical_hash({
            "decision_id": decision_id, "goal_id": candidate.goal_id,
            "session_id": session_id, "tool": tool_name, "risk_level": risk_level,
            "target_project": target_project, "decided_at": decided_iso,
        }),
        "patch_hash": None,
        "precondition_snapshot_hash": foundation.canonical_hash(snapshot),
        "original_engine_result": "ALLOW",
        "effective_application_result": result,
        "kill_switch_epoch": 0, "deny_policy_epoch": 0, "assignment_epoch": 0,
        "grant_revocation_epoch": int(grant.get("revocation_epoch") or 0),
        "ancestor_revocation_epoch": 0,
        "decided_at": decided_iso,
        "canonicalization_version": foundation.CANONICALIZATION_VERSION,
        "hash_algorithm": foundation.HASH_ALGORITHM,
        "diagnostics": {
            "gate": "live_trading_guard", "tool": tool_name, "risk_level": risk_level,
            "target_project": target_project, "source": verdict.source,
            "goal_title": (candidate.title or "")[:80], **snapshot,
        },
    }
    if signing_key is not None:
        row["signature_algorithm"] = foundation.SIGNATURE_ALGORITHM
        row["signature_key_id"] = signing_key.key_id
        row["signature_key_version"] = signing_key.version
        row["signature"] = foundation.sign_envelope(row, signing_key)
    else:
        row["signature_algorithm"] = "NONE"
        row["signature_key_id"] = "live-trading-gate-unsigned"
        row["signature_key_version"] = 1
        row["signature"] = "unsigned"
    return row


_INSERT_DECISION = """
INSERT INTO goal_policy_decisions
  (id, tenant_id, project, workspace_kind, principal_session_id, assignment_id,
   target_type, target_id, action, base_version, environment, boundary_decision,
   approval_route, automation_eligibility, risk_tier, result, reason_codes,
   policy_version, matched_grant_id, grant_version, decision_input_hash, patch_hash,
   precondition_snapshot_hash, canonicalization_version, hash_algorithm, diagnostics,
   original_engine_result, effective_application_result, masking_policy_version,
   kill_switch_epoch, deny_policy_epoch, assignment_epoch, grant_revocation_epoch,
   ancestor_revocation_epoch, target_version, signature_algorithm, signature_key_id,
   signature_key_version, signature, decided_at)
VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, NULL, $6, $7::uuid, $8, $9, $10, $11,
        $12, $13, $14, $15, $16::text[], $17::uuid, $18::uuid, $19, $20, NULL,
        $21, $22, $23, $24::jsonb, $25, $26, 1, $27, $28, $29, $30, $31, $32,
        $33, $34, $35, $36, $37)
"""


async def record_decision(
    pool: Any,
    candidate: Candidate,
    verdict: Verdict,
    *,
    session_id: str,
    tool_name: str,
    risk_level: str,
    target_project: str,
) -> Optional[str]:
    """판정 1건을 남긴다. 기록 실패는 게이트 결과를 바꾸지 않는다(경고만)."""
    if not _is_uuid(session_id) or not _is_uuid(candidate.tenant_id):
        logger.warning("goal_policy_decision_skipped", reason="non_uuid_principal",
                       goal=candidate.goal_id[:8])
        return None
    try:
        async with pool.acquire() as conn:
            policy_version = await _policy_version_for(conn, candidate)
            if not policy_version:
                # 이관 전에는 정책 버전이 없어 FK 를 채울 수 없다.
                logger.info("goal_policy_decision_skipped", reason="no_policy_version",
                            goal=candidate.goal_id[:8], result=verdict.allowed)
                return None
            now = datetime.now(timezone.utc)
            row = build_decision(
                candidate, verdict, session_id=session_id, tool_name=tool_name,
                risk_level=risk_level, target_project=target_project,
                policy_version=policy_version, decided_at=now, signing_key=_signing_key(),
            )
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_tenant_id', $1, true)", candidate.tenant_id,
                )
                await conn.execute(
                    _INSERT_DECISION,
                    row["decision_id"], row["tenant_id"], row["project"], row["workspace_kind"],
                    row["principal_session_id"], row["target_type"], row["target_id"],
                    row["action"], row["base_version"], row["environment"],
                    row["boundary_decision"], row["approval_route"],
                    row["automation_eligibility"], row["risk_tier"], row["result"],
                    row["reason_codes"], row["policy_version"], row["matched_grant_id"],
                    row["grant_version"], row["decision_input_hash"],
                    row["precondition_snapshot_hash"], row["canonicalization_version"],
                    row["hash_algorithm"], json.dumps(row["diagnostics"], ensure_ascii=False),
                    row["original_engine_result"], row["effective_application_result"],
                    row["kill_switch_epoch"], row["deny_policy_epoch"],
                    row["assignment_epoch"], row["grant_revocation_epoch"],
                    row["ancestor_revocation_epoch"], row["target_version"],
                    row["signature_algorithm"], row["signature_key_id"],
                    row["signature_key_version"], row["signature"], now,
                )
            return row["decision_id"]
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 도구를 막지 않는다
        logger.warning("goal_policy_decision_record_failed", goal=candidate.goal_id[:8],
                       error=str(exc)[:200])
        return None


def candidate_from_row(row: Mapping[str, Any]) -> Candidate:
    return Candidate(
        goal_id=str(row["id"]),
        title=str(row.get("title") or ""),
        status=str(row.get("status") or ""),
        project=str(row.get("project") or ""),
        tenant_id=str(row.get("tenant_id") or ""),
        paused_at=row.get("paused_at"),
        policy=signals._policy(row.get("approval_policy")),
    )
