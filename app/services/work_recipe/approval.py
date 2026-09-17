"""승인 게이트 — WRITE_EXTERNAL 이상 단계는 사람이 눌러야 지나간다 (FR-4).

흐름은 셋이다.

1. :func:`request_approval` — pending 행을 만들고 :class:`ApprovalRequired` 로
   **실행을 끊는다**. 같은 (run_id, step_seq) 가 이미 승인돼 있으면 그대로
   통과시킨다 — 승인 후 재개가 같은 함수로 돌아야 재개 경로가 따로 놀지 않는다.
2. :func:`resolve_approval` — approve/reject 를 기록한다. IRREVERSIBLE 은
   재확인 문구(confirm_text)가 맞아야 approve 된다(PRD 4절).
3. :func:`is_approved` — 실행 직전 최종 확인.

DB 스키마: scripts/sql/20260917_work_recipe_p1_guard_audit.sql
PRD 5절의 ``risk_level`` 은 P0 가 이미 만든 ``recipe_approvals.risk`` 컬럼이다.
같은 뜻의 컬럼을 하나 더 만들면 둘 중 하나가 반드시 낡는다 — 이름만 매핑한다.
"""
from __future__ import annotations

import uuid
from typing import Any

from app.core.db_pool import get_pool
from app.services.work_recipe.guard import RiskLevel, requires_confirmation

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISIONS: tuple[str, ...] = (DECISION_APPROVE, DECISION_REJECT)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

# decision → recipe_approvals.status
_DECISION_STATUS: dict[str, str] = {
    DECISION_APPROVE: STATUS_APPROVED,
    DECISION_REJECT: STATUS_REJECTED,
}


class ApprovalRequired(RuntimeError):
    """승인이 필요해 실행을 멈췄다. 호출자는 이 예외로 런을 중단한다."""

    def __init__(
        self,
        approval_id: Any,
        *,
        run_id: Any = None,
        step_seq: int | None = None,
        risk_level: str = "",
        summary: str = "",
        confirm_text: str = "",
    ) -> None:
        self.approval_id = str(approval_id) if approval_id is not None else ""
        self.run_id = run_id
        self.step_seq = step_seq
        self.risk_level = risk_level
        self.summary = summary
        self.confirm_text = confirm_text
        super().__init__(
            f"승인이 필요합니다: step {step_seq} risk={risk_level} "
            f"(approval_id={self.approval_id}) — {summary}"
        )


class ApprovalRejected(ApprovalRequired):
    """사람이 거부한 단계다. 재요청 없이 그대로 멈춘다."""


class ApprovalError(RuntimeError):
    """승인 처리 자체가 잘못됐다(알 수 없는 decision, 재확인 문구 불일치 등)."""


class ConfirmationRequired(ApprovalError):
    """IRREVERSIBLE 승인에 재확인 문구가 없거나 틀렸다 (PRD 4절)."""


# ------------------------------------------------------------ 순수 검증


def normalize_decision(decision: Any) -> str:
    """approve/reject 로 정규화. 그 밖의 값은 ApprovalError."""
    text = str(decision or "").strip().lower()
    aliases = {
        "approve": DECISION_APPROVE, "approved": DECISION_APPROVE,
        "ok": DECISION_APPROVE, "yes": DECISION_APPROVE, "승인": DECISION_APPROVE,
        "reject": DECISION_REJECT, "rejected": DECISION_REJECT,
        "deny": DECISION_REJECT, "no": DECISION_REJECT, "거부": DECISION_REJECT,
    }
    if text not in aliases:
        raise ApprovalError(
            f"알 수 없는 승인 결정입니다: {decision!r} (허용: {', '.join(DECISIONS)})"
        )
    return aliases[text]


def default_confirm_text(risk_level: Any, *, action: str = "", domain: str = "") -> str:
    """IRREVERSIBLE 단계에서 승인자가 그대로 입력해야 할 재확인 문구.

    되돌릴 수 없는 행위는 "예" 한 번으로 통과시키지 않는다 — 무엇을 승인하는지
    타이핑하게 만든다. 문구는 (행위, 도메인)으로 결정되므로 카드가 바뀌어도
    같은 단계에는 같은 문구가 나온다.
    """
    if not requires_confirmation(risk_level):
        return ""
    target = (domain or "").strip() or "대상"
    verb = (action or "실행").strip()
    return f"{target} {verb} 실행에 동의합니다"


def check_confirmation(risk_level: Any, expected: Any, provided: Any) -> None:
    """IRREVERSIBLE 승인의 재확인 문구를 검사한다. 틀리면 ConfirmationRequired."""
    if not requires_confirmation(risk_level):
        return
    want = str(expected or "").strip()
    got = str(provided or "").strip()
    if not got:
        raise ConfirmationRequired(
            f"IRREVERSIBLE 단계는 재확인 문구가 있어야 승인됩니다: {want!r}"
        )
    if want and got != want:
        raise ConfirmationRequired(
            f"재확인 문구가 일치하지 않습니다 (기대: {want!r})"
        )


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


# --------------------------------------------------------------- DB 경로


async def get_approval(approval_id: Any) -> dict[str, Any] | None:
    """승인 행 하나. 없으면 None."""
    if approval_id in (None, ""):
        return None
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM recipe_approvals WHERE id = $1::uuid",
            str(approval_id),
        )
    return _row_to_dict(row) if row else None


async def find_approval(run_id: Any, step_seq: int) -> dict[str, Any] | None:
    """(run_id, step_seq) 의 승인 행. UNIQUE 이므로 최대 1건."""
    if run_id in (None, ""):
        return None
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM recipe_approvals
             WHERE run_id = $1::uuid AND step_seq = $2
            """,
            str(run_id),
            int(step_seq),
        )
    return _row_to_dict(row) if row else None


async def request_approval(
    run_id: Any,
    step_seq: int,
    risk_level: Any,
    summary: str,
    *,
    action: str = "",
    domain: str = "",
    confirm_text: str | None = None,
    requested_by: str = "",
    tenant_id: Any = None,
) -> str:
    """승인 카드를 발행하고 실행을 끊는다 (FR-4).

    - 이미 approve 된 단계면 **그대로 승인 id 를 돌려준다**(재개 경로).
    - reject 된 단계면 :class:`ApprovalRejected`.
    - 그 밖에는 pending 행을 만들고 :class:`ApprovalRequired` 를 던진다.

    같은 단계를 두 번 요청해도 카드는 하나다 — UNIQUE(run_id, step_seq) 가
    보장하고, 여기서는 기존 행을 다시 읽어 같은 예외를 던진다. 승인 로그와
    실행 로그의 1:1 대응(NFR-2)이 깨지지 않아야 한다.
    """
    level = RiskLevel.from_any(risk_level)
    existing = await find_approval(run_id, step_seq)
    if existing is not None:
        status = str(existing.get("status") or "")
        if status == STATUS_APPROVED:
            return str(existing.get("id"))
        if status == STATUS_REJECTED:
            raise ApprovalRejected(
                existing.get("id"),
                run_id=run_id,
                step_seq=step_seq,
                risk_level=level.value,
                summary=str(existing.get("summary") or summary),
            )
        raise ApprovalRequired(
            existing.get("id"),
            run_id=run_id,
            step_seq=step_seq,
            risk_level=level.value,
            summary=str(existing.get("summary") or summary),
            confirm_text=str(existing.get("confirm_text") or ""),
        )

    phrase = confirm_text if confirm_text is not None else default_confirm_text(
        level, action=action, domain=domain
    )
    # 순환 import를 피하면서 승인 INSERT 역시 중앙 감사 마스킹을 통과시킨다.
    from app.services.work_recipe.audit import mask_secrets

    async with get_pool().acquire() as conn:
        approval_id = await conn.fetchval(
            """
            INSERT INTO recipe_approvals (
                tenant_id, run_id, step_seq, action, risk, status,
                summary, confirm_text, requested_by, requested_at
            ) VALUES ($1, $2::uuid, $3, $4, $5, 'pending', $6, $7, $8, NOW())
            RETURNING id
            """,
            _uuid_or_none(tenant_id),
            str(run_id),
            int(step_seq),
            mask_secrets(action or ""),
            level.value,
            mask_secrets(summary or ""),
            mask_secrets(phrase or ""),
            mask_secrets(requested_by or ""),
        )
    raise ApprovalRequired(
        approval_id,
        run_id=run_id,
        step_seq=step_seq,
        risk_level=level.value,
        summary=str(summary or ""),
        confirm_text=str(phrase or ""),
    )


async def resolve_approval(
    approval_id: Any,
    decision: Any,
    decided_by: str,
    reason: str = "",
    *,
    confirm_text: Any = None,
) -> dict[str, Any]:
    """승인/거부를 기록한다.

    IRREVERSIBLE 을 approve 하려면 재확인 문구가 저장된 값과 같아야 한다.
    이미 결정된 카드는 다시 바꾸지 않는다 — 결정은 감사 대상이다.
    """
    verdict = normalize_decision(decision)
    row = await get_approval(approval_id)
    if row is None:
        raise ApprovalError(f"승인 카드를 찾을 수 없습니다: {approval_id!r}")

    status = str(row.get("status") or "")
    if status in (STATUS_APPROVED, STATUS_REJECTED):
        raise ApprovalError(
            f"이미 결정된 승인입니다(status={status}) — 결정은 번복하지 않습니다: {approval_id}"
        )

    if verdict == DECISION_APPROVE:
        check_confirmation(row.get("risk"), row.get("confirm_text"), confirm_text)

    async with get_pool().acquire() as conn:
        updated = await conn.fetchrow(
            """
            UPDATE recipe_approvals
               SET status = $2,
                   decision = $3,
                   decided_by = $4,
                   reason = $5,
                   decision_note = $5,
                   decided_at = NOW()
             WHERE id = $1::uuid
               AND status = 'pending'
            RETURNING *
            """,
            str(approval_id),
            _DECISION_STATUS[verdict],
            verdict,
            str(decided_by or ""),
            str(reason or ""),
        )
    if updated is None:
        raise ApprovalError(f"승인 상태가 그 사이 바뀌었습니다: {approval_id}")
    return _row_to_dict(updated)


async def is_approved(approval_id: Any) -> bool:
    """이 카드가 승인된 상태인가."""
    row = await get_approval(approval_id)
    return bool(row) and str(row.get("status") or "") == STATUS_APPROVED


async def list_pending(*, limit: int = 50) -> list[dict[str, Any]]:
    """대기 중인 승인 카드 목록(오래된 것부터)."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM recipe_approvals
             WHERE status = 'pending'
             ORDER BY requested_at ASC
             LIMIT $1
            """,
            int(limit),
        )
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("id", "run_id", "tenant_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    # PRD 5절 이름으로도 읽을 수 있게 별칭을 얹는다(컬럼은 하나다).
    if "risk" in data:
        data.setdefault("risk_level", data["risk"])
    return data
