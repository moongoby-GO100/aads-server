"""마일스톤 완료 체크리스트 판정과 비파괴 정정.

`goal_manager.check_milestone_completion` 은 링크 상태만으로 마일스톤을 완료시켰다.
그래서 `completion_checklist` 4건이 전부 pending 인 M4 가 completed 가 됐다.
여기서는 (1) 체크리스트 충족 판정(순수 함수)과 (2) 항목 갱신·잘못된 완료 정정을 맡는다.

규칙
- 체크리스트가 없거나 비어 있으면 "명시 체크리스트 없음" — 기존 목표 호환을 위해 판정하지 않는다.
- 명시 체크리스트가 있으면 모든 항목이 충족돼야 한다. 모양을 해석할 수 없으면 미충족이다.
- 항목 상태 `done`/`completed` 는 같은 뜻이다(저장은 `done`).
- 승인권자 항목(owner 가 CEO/대표/승인 계열)은 status 만으로 충족되지 않는다.
  사람 승인권자의 `approved_by` 기록이 있어야 하며, 러너·시스템 주체는 기록할 수 없다.
- 승인권자 항목은 승인·철회·근거 교체 모두 사람 승인권자만 바꿀 수 있다(비승인자·러너는 403, 무변경).
- 항목 갱신은 해당 항목만 바꾸고 이전 상태·근거·승인자·승인시각을 항목 `history` 에 남긴다. 삭제하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

ITEM_DONE_STATUSES = frozenset({"done", "completed", "complete", "passed"})
WRITABLE_STATUSES = frozenset({"pending", "in_progress", "blocked", "done"})
_APPROVER_OWNER_MARKERS = ("ceo", "대표", "승인")
_NON_HUMAN_PREFIXES = ("system", "runner", "pipeline", "auto", "bot")
MIN_EVIDENCE_CHARS = 10
MIN_REASON_CHARS = 10


class ChecklistError(Exception):
    def __init__(self, code: str, status_code: int = 400, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, **self.extra}


def normalize_item_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "pending"
    return "done" if text in ITEM_DONE_STATUSES else text


def is_human_principal(actor: Any) -> bool:
    text = str(actor or "").strip().lower()
    return bool(text) and not text.startswith(_NON_HUMAN_PREFIXES)


def is_approver_item(item: dict[str, Any]) -> bool:
    if item.get("approval_required") or item.get("approver"):
        return True
    owner = str(item.get("owner") or "").lower()
    return any(marker in owner for marker in _APPROVER_OWNER_MARKERS)


def _parse(raw: Any) -> tuple[bool, Optional[list[dict[str, Any]]]]:
    """(명시 체크리스트 여부, 항목 목록 | 해석 불가면 None)."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return False, []
        try:
            raw = json.loads(text)
        except ValueError:
            return True, None
    if raw is None or raw == [] or raw == {}:
        return False, []
    if isinstance(raw, dict):
        items = []
        for key, value in raw.items():
            if isinstance(value, dict):
                items.append({"step": str(key), **value})
            else:
                items.append({"step": str(key), "status": value})
        return True, items
    if isinstance(raw, list):
        items = []
        for entry in raw:
            if isinstance(entry, dict):
                items.append(entry)
            elif isinstance(entry, str) and entry.strip():
                items.append({"title": entry.strip(), "status": "pending"})
            else:
                return True, None
        return True, items
    return True, None


def _item_unmet_reason(item: dict[str, Any]) -> Optional[str]:
    if normalize_item_status(item.get("status")) != "done":
        return "not_done"
    if is_approver_item(item):
        approver = item.get("approved_by")
        if not isinstance(approver, str) or not is_human_principal(approver):
            return "approval_missing"
    return None


def evaluate_checklist(raw: Any) -> dict[str, Any]:
    """체크리스트 충족 여부. `explicit=False` 면 판정 대상이 아니다(`satisfied=True`)."""
    explicit, items = _parse(raw)
    if not explicit:
        return {"explicit": False, "satisfied": True, "total": 0, "unmet": []}
    if items is None:
        return {
            "explicit": True, "satisfied": False, "total": 0,
            "unmet": [{"step": None, "title": None, "owner": None, "reason": "unparseable"}],
        }
    unmet = []
    for index, item in enumerate(items):
        reason = _item_unmet_reason(item)
        if reason:
            unmet.append({
                "step": item.get("step") if item.get("step") is not None else f"#{index + 1}",
                "title": item.get("title"),
                "owner": item.get("owner"),
                "reason": reason,
            })
    return {
        "explicit": True, "satisfied": not unmet, "total": len(items), "unmet": unmet,
    }


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


_MILESTONE_FOR_UPDATE = """
    SELECT m.status, m.version, m.completed_at, m.review_note,
           NULLIF(to_jsonb(m)->'completion_checklist', 'null'::jsonb) AS completion_checklist
    FROM milestones m
    WHERE m.id = $1::uuid AND m.goal_id = $2::uuid AND m.tenant_id = $3::uuid
    FOR UPDATE
"""


def _evidence_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def update_checklist_item(
    conn: Any, *, goal_id: str, milestone_id: str, tenant_id: str, step: str,
    status: str, evidence: Optional[str], expected_version: int, request_id: str,
    actor: str, actor_is_approver: bool, now_iso: str,
) -> dict[str, Any]:
    """체크리스트 항목 1개를 갱신한다. 마일스톤 상태는 건드리지 않는다."""
    target = normalize_item_status(status)
    if target not in WRITABLE_STATUSES:
        raise ChecklistError("invalid_status", 422, allowed=sorted(WRITABLE_STATUSES))
    evidence_text = (evidence or "").strip()
    if target == "done" and len(evidence_text) < MIN_EVIDENCE_CHARS:
        raise ChecklistError("evidence_required", 422, min_chars=MIN_EVIDENCE_CHARS)

    async with conn.transaction():
        row = await conn.fetchrow(_MILESTONE_FOR_UPDATE, milestone_id, goal_id, tenant_id)
        if not row:
            raise ChecklistError("milestone_not_found", 404)
        checklist = _jsonb(row["completion_checklist"])
        if not isinstance(checklist, list) or not all(isinstance(i, dict) for i in checklist):
            raise ChecklistError("checklist_not_updatable", 409)
        index = next(
            (i for i, item in enumerate(checklist) if str(item.get("step")) == str(step)),
            None,
        )
        if index is None:
            raise ChecklistError("checklist_step_not_found", 404)
        item = dict(checklist[index])
        history = list(item.get("history") or [])

        replay = next(
            (h for h in history if isinstance(h, dict) and h.get("request_id") == request_id), None,
        )
        if replay is not None:
            # 같은 request_id 의 재전송만 멱등이다. 다른 주체·다른 목표 상태로 재사용하면 거절한다 —
            # 그렇지 않으면 권한 없는 주체가 남의 request_id 로 성공 응답을 얻는다.
            if replay.get("actor") != actor or replay.get("to") != target:
                raise ChecklistError("request_id_reused", 409)
            return {
                "milestone_id": milestone_id, "step": str(step), "changed": False,
                "idempotent": True, "version": row["version"],
                "status": normalize_item_status(item.get("status")),
            }
        if int(row["version"]) != int(expected_version):
            raise ChecklistError(
                "version_conflict", 409, current_version=int(row["version"]),
            )

        previous = normalize_item_status(item.get("status"))
        if previous == target and (target != "done" or evidence_text == str(item.get("evidence") or "")):
            return {
                "milestone_id": milestone_id, "step": str(step), "changed": False,
                "idempotent": False, "version": row["version"], "status": previous,
            }
        # 승인항목은 승인(done)뿐 아니라 철회·근거 교체 등 모든 변경이 사람 승인권자의 몫이다.
        if is_approver_item(item) and not (actor_is_approver and is_human_principal(actor)):
            raise ChecklistError("approver_required", 403)

        entry: dict[str, Any] = {
            "at": now_iso, "request_id": request_id, "actor": actor,
            "actor_is_approver": bool(actor_is_approver),
            "from": previous, "to": target,
        }
        if item.get("evidence"):
            entry["prev_evidence"] = str(item["evidence"])
            entry["prev_evidence_sha256"] = _evidence_digest(str(item["evidence"]))
        if item.get("approved_by"):
            entry["prev_approved_by"] = item["approved_by"]
        if item.get("approved_at"):
            entry["prev_approved_at"] = item["approved_at"]
        if target != "done" and evidence_text:
            entry["note"] = evidence_text
        history.append(entry)

        item["status"] = target
        if target == "done":
            item["evidence"] = evidence_text
            if is_approver_item(item):
                item["approved_by"] = actor
                item["approved_at"] = now_iso
        else:
            item.pop("approved_by", None)
            item.pop("approved_at", None)
        item["history"] = history
        updated = list(checklist)
        updated[index] = item

        new_version = await conn.fetchval(
            "UPDATE milestones SET completion_checklist = $4::jsonb, "
            "       version = version + 1, updated_at = NOW() "
            "WHERE id = $1::uuid AND goal_id = $2::uuid AND tenant_id = $3::uuid "
            "  AND version = $5 RETURNING version",
            milestone_id, goal_id, tenant_id,
            json.dumps(updated, ensure_ascii=False), int(expected_version),
        )
        if new_version is None:
            raise ChecklistError("version_conflict", 409)
    return {
        "milestone_id": milestone_id, "step": str(step), "changed": True,
        "idempotent": False, "version": new_version, "status": target,
        "previous_status": previous,
        "checklist": evaluate_checklist(updated),
    }


async def reopen_unverified_completion(
    conn: Any, *, goal_id: str, milestone_id: str, tenant_id: str,
    expected_version: int, request_id: str, reason: str, actor: str,
) -> dict[str, Any]:
    """체크리스트 미충족인데 completed 인 마일스톤만 in_progress 로 되돌린다.

    rewind 와 달리 발송 기록·증거·보고 로그·체크리스트를 지우지 않는다.
    이전 완료 시각·버전·미충족 항목은 review_note 에 덧붙여 남긴다.
    """
    reason_text = (reason or "").strip()
    if len(reason_text) < MIN_REASON_CHARS:
        raise ChecklistError("reason_required", 422, min_chars=MIN_REASON_CHARS)

    marker = f"[완료 정정 {request_id}]"
    async with conn.transaction():
        row = await conn.fetchrow(_MILESTONE_FOR_UPDATE, milestone_id, goal_id, tenant_id)
        if not row:
            raise ChecklistError("milestone_not_found", 404)
        if marker in str(row["review_note"] or ""):
            return {
                "milestone_id": milestone_id, "changed": False, "idempotent": True,
                "status": row["status"], "version": row["version"],
            }
        if int(row["version"]) != int(expected_version):
            raise ChecklistError(
                "version_conflict", 409, current_version=int(row["version"]),
            )
        if row["status"] != "completed":
            raise ChecklistError("not_completed", 409, status=row["status"])
        verdict = evaluate_checklist(_jsonb(row["completion_checklist"]))
        if not verdict["explicit"]:
            raise ChecklistError("no_explicit_checklist", 409)
        if verdict["satisfied"]:
            raise ChecklistError("checklist_satisfied", 409)

        steps = ",".join(str(u["step"]) for u in verdict["unmet"])
        note = (
            f"{marker} {reason_text} | actor={actor} | 이전 status=completed "
            f"version={row['version']} completed_at={row['completed_at']} | 미충족={steps}"
        )
        previous_note = str(row["review_note"] or "").strip()
        merged = f"{previous_note}\n{note}" if previous_note else note
        goal_status = await conn.fetchval(
            "SELECT status FROM goals WHERE id = $1::uuid AND tenant_id = $2::uuid",
            goal_id, tenant_id,
        )
        if goal_status in ("completed", "cancelled"):
            raise ChecklistError("goal_closed", 409, goal_status=goal_status)
        new_version = await conn.fetchval(
            "UPDATE milestones SET status = 'in_progress', completed_at = NULL, "
            "       review_note = $4, version = version + 1, updated_at = NOW() "
            "WHERE id = $1::uuid AND goal_id = $2::uuid AND tenant_id = $3::uuid "
            "  AND version = $5 AND status = 'completed' RETURNING version",
            milestone_id, goal_id, tenant_id, merged, int(expected_version),
        )
        if new_version is None:
            raise ChecklistError("version_conflict", 409)
    return {
        "milestone_id": milestone_id, "changed": True, "idempotent": False,
        "status": "in_progress", "previous_status": "completed",
        "version": new_version, "unmet": verdict["unmet"],
    }
