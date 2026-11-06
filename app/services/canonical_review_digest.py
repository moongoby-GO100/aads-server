"""검토 대기 정본 문서 조회와 CEO 일일 요약 발송.

검토 대기 = 최신 리비전의 마지막 상태 이벤트(review/approved/archived)가 'review' 이고
승인 포인터가 최신 리비전이 아닌 문서. 같은 조회를 읽기 API 와 일일 잡이 공유한다.
"""
from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from app.services.doc_cross_review import build_review_detail
from app.services.doc_tier import revision_metadata

import structlog

logger = structlog.get_logger()

KST = ZoneInfo("Asia/Seoul")
PER_PROJECT_LIMIT = 10
MAX_MESSAGE_CHARS = 3900
_TITLE_CHARS = 60
_LOCK_KEY = "canonical_review_digest"

_QUEUE_SQL = (
    "SELECT h.tenant_id::text AS tenant_id, h.project_key, h.document_key, h.kind, r.title, "
    "r.doc_tier,r.author_model,r.author_provider,char_length(r.content) AS char_count, "
    "EXISTS(SELECT 1 FROM project_document_goal_links gl WHERE gl.head_id=h.id) AS goal_linked, "
    "ev.created_at AS review_at, "
    "COALESCE((SELECT jsonb_agg(jsonb_build_object('reviewer_model', rv.reviewer_model, "
    "'reviewer_provider', rv.reviewer_provider, 'verdict', rv.verdict, 'reason', rv.reason, "
    "'cross_company', rv.cross_company, 'created_at', rv.created_at) ORDER BY rv.id) "
    "FROM project_document_reviews rv WHERE rv.revision_id=r.id), '[]'::jsonb) AS review_rows "
    "FROM project_document_heads h "
    "JOIN project_document_revisions r ON r.id=h.latest_revision_id AND r.head_id=h.id "
    "JOIN LATERAL (SELECT e.action, e.created_at FROM project_document_events e "
    "WHERE e.head_id=h.id AND e.revision_id=h.latest_revision_id "
    "AND e.action IN ('review','approved','archived') ORDER BY e.id DESC LIMIT 1) ev ON true "
    "WHERE ($1::uuid IS NULL OR h.tenant_id=$1::uuid) AND ($2::text IS NULL OR h.project_key=$2) "
    "AND ev.action='review' AND h.approved_revision_id IS DISTINCT FROM h.latest_revision_id "
    "ORDER BY ev.created_at, h.project_key, h.document_key"
)


def _kst_now() -> datetime:
    return datetime.now(KST)


async def fetch_review_queue(conn: Any, tenant_id: str | None, project_key: str | None = None,
                             *, now: datetime | None = None) -> list[dict]:
    """tenant_id=None 은 일일 잡 전용(전 테넌트). API 는 항상 테넌트를 넘긴다."""
    today = (now or _kst_now()).astimezone(KST).date()
    rows = await conn.fetch(_QUEUE_SQL, tenant_id, project_key)
    items = []
    for row in rows:
        metadata = revision_metadata(row, row["kind"])
        if (metadata["doc_tier"] or metadata.get("doc_tier_computed")) in ("D1", "SPLIT"):
            continue
        review_kst = row["review_at"].astimezone(KST)
        review_rows = row.get("review_rows") or []
        if isinstance(review_rows, str):
            review_rows = json.loads(review_rows)
        items.append({
            **metadata,
            "tenant_id": row["tenant_id"],
            "project_key": row["project_key"],
            "document_key": row["document_key"],
            "title": row["title"],
            "kind": row["kind"],
            "review_detail": build_review_detail(review_rows),
            "review_at": review_kst.isoformat(),
            "waiting_days": max((today - review_kst.date()).days, 0),
        })
    return items


_MAX_HELD_CANDIDATES = 200
_MAX_AUTO_ROWS = 200
_AUTO_SQL = (
    "SELECT h.tenant_id::text AS tenant_id, h.project_key, h.document_key, h.kind, h.generation, h.title AS head_title, "
    "r.id AS revision_id, r.title, r.version, r.doc_tier, r.content, e.created_at AS approved_at, "
    "(h.approved_revision_id IS NOT DISTINCT FROM e.revision_id) AS currently_approved, "
    "COALESCE((SELECT jsonb_agg(jsonb_build_object('reviewer_model', rv.reviewer_model, "
    "'reviewer_provider', rv.reviewer_provider, 'verdict', rv.verdict, 'reason', rv.reason, "
    "'cross_company', rv.cross_company, 'created_at', rv.created_at) ORDER BY rv.id) "
    "FROM project_document_reviews rv WHERE rv.revision_id=r.id), '[]'::jsonb) AS review_rows "
    "FROM project_document_events e "
    "JOIN project_document_heads h ON h.id=e.head_id "
    "JOIN project_document_revisions r ON r.id=e.revision_id "
    "WHERE e.action='approved' AND e.actor_id=$3 AND e.created_at > now() - make_interval(hours => $4::int) "
    "AND ($1::uuid IS NULL OR h.tenant_id=$1::uuid) AND ($2::text IS NULL OR h.project_key=$2) "
    "ORDER BY e.created_at DESC, h.project_key, h.document_key LIMIT " + str(_MAX_AUTO_ROWS + 1)
)
# 최근 since_hours 안에 리뷰(교차검토 결과)가 기록된 승인 필요 판본: 최신 판본이고 아직 승인되지 않은 D2/D3.
_HELD_SQL = (
    "SELECT h.id AS head_id, h.tenant_id::text AS tenant_id, h.project_key, r.id AS revision_id "
    "FROM project_document_heads h "
    "JOIN project_document_revisions r ON r.id=h.latest_revision_id AND r.head_id=h.id "
    "WHERE ($1::uuid IS NULL OR h.tenant_id=$1::uuid) AND ($2::text IS NULL OR h.project_key=$2) "
    "AND r.doc_tier IN ('D2','D3') AND h.approved_revision_id IS DISTINCT FROM h.latest_revision_id "
    # evaluate 의 skip 대상(목표 미연결·report·마이그레이션 승인 문서)은 상한을 차지하지 않게 미리 뺀다.
    "AND EXISTS(SELECT 1 FROM project_document_goal_links gl WHERE gl.head_id=h.id) "
    "AND lower(COALESCE(h.kind,''))<>'report' AND h.document_key NOT LIKE 'migration-apply-%' "
    "AND EXISTS(SELECT 1 FROM project_document_reviews rv WHERE rv.revision_id=r.id "
    "AND rv.created_at > now() - make_interval(hours => $3::int)) "
    "ORDER BY h.updated_at DESC LIMIT " + str(_MAX_HELD_CANDIDATES + 1)
)

# 일일 요약 대상 테넌트: 최근 자동승인 이벤트 또는 최근 리뷰가 있는 테넌트. 테넌트마다 따로 조회한다.
_AUTO_TENANTS_SQL = (
    "SELECT DISTINCT h.tenant_id::text AS tenant_id FROM project_document_heads h "
    "WHERE EXISTS(SELECT 1 FROM project_document_events e WHERE e.head_id=h.id AND e.action='approved' "
    "AND e.actor_id=$1 AND e.created_at > now() - make_interval(hours => $2::int)) "
    "OR EXISTS(SELECT 1 FROM project_document_reviews rv JOIN project_document_revisions r ON r.id=rv.revision_id "
    "WHERE r.head_id=h.id AND rv.created_at > now() - make_interval(hours => $2::int))"
)

# 보류 사유 코드 -> 쉬운 한국어. 모르는 코드는 그대로 두지 않고 일반 문구로 바꾼다(내부 코드 노출 방지).
_HOLD_REASON_KO = {
    "checks_missing": "자동 점검 결과가 없습니다",
    "checks_failed": "자동 점검에서 문제가 발견됐습니다",
    "review_state_pending": "교차검토가 아직 끝나지 않았습니다",
    "review_state_blocked": "교차검토가 막혀 있습니다",
    "review_state_rejected": "검토자가 반려했습니다",
    "reject_present": "검토자가 반려했습니다",
    "legacy_review_no_score": "점수가 없는 예전 방식 검토입니다(재검토 필요)",
    "review_json_invalid": "검토 결과 형식이 올바르지 않습니다",
    "review_issues_truncated": "지적 사항이 너무 많아 일부가 잘렸습니다",
    "non_finite_score": "검토 점수가 올바르지 않습니다",
    "needs_second_company": "서로 다른 두 회사의 통과 검토가 더 필요합니다",
    "blocker_present": "반드시 고쳐야 할 지적이 남아 있습니다",
    "major_present": "중요한 지적이 남아 있습니다",
    "score_below_min": "검토 점수가 기준에 못 미칩니다",
    "high_risk_score_below_min": "고위험 문서라 더 높은 검토 점수가 필요합니다",
    "criterion_below_min": "세부 평가 항목 중 기준 미달이 있습니다",
    "high_risk_unverified_exceeded": "고위험 문서 미검증 3건 이상",
    "high_risk_minor_exceeded": "고위험 문서인데 사소한 지적이 너무 많습니다",
}


def hold_reason_ko(code: str) -> str:
    code = str(code)
    key = code.split(":", 1)[0]
    if key == "review_state_None":
        return "교차검토 결과가 아직 없습니다"
    if key.startswith("review_state_"):
        return _HOLD_REASON_KO.get(key, "교차검토가 통과 상태가 아닙니다")
    return _HOLD_REASON_KO.get(key, "자동승인 기준을 충족하지 못했습니다")


def _review_fields(review_rows: Any) -> dict:
    if isinstance(review_rows, str):
        review_rows = json.loads(review_rows)
    detail = build_review_detail(review_rows or [])
    minors = []
    for review in detail["reviews"]:
        for issue in review["issues"]:
            if issue.get("severity") == "minor":
                text = issue.get("problem") or issue.get("criterion")
                if isinstance(text, str) and text.strip():
                    minors.append(text.strip()[:200])
    return {"ceo_brief": detail["ceo_brief"], "score": detail["min_score"],
            "minor_issues": list(dict.fromkeys(minors))[:3],
            "unverified_count": sum(r.get("unverified_count", 0) for r in detail["reviews"]),
            "reviewer_models": [r["model"] for r in detail["reviews"]]}


def _auto_item(row: Any, *, risk: str, mode: str, reasons: list | None = None, score: Any = None,
               review_rows: Any = None, head: Any = None) -> dict:
    fields = _review_fields(review_rows)
    if score is not None:
        fields["score"] = score
    return {"tenant_id": row.get("tenant_id"), "project_key": row["project_key"], "document_key": row["document_key"],
            "title": row["title"], "kind": row["kind"], "doc_tier": row["doc_tier"],
            "revision_id": str(row["revision_id"]), "version": row["version"],
            "generation": row["generation"], "mode": mode, "risk": risk, **fields,
            "hold_reasons": [hold_reason_ko(c) for c in (reasons or [])],
            "hold_codes": list(reasons or [])}


async def fetch_auto_approval_digest(conn: Any, tenant_id: str | None, project_key: str | None = None,
                                     *, since_hours: int = 24) -> dict:
    """자동승인 참고검토용 요약. DB 쓰기 없음(판정은 doc_auto_approval.assess, lock 없이 읽기만).

    auto_approved: 자동승인 주체가 최근 since_hours 안에 승인해 현재도 승인 상태인 판본.
    held: 최근 리뷰가 기록된 승인 필요 판본 중 판정이 hold 인 것(사유 포함).
    would_approve: shadow 모드에서만 채워진다(켜면 승인될 판본).
    본문(content)은 위험 분류에만 쓰고 결과에 담지 않는다.
    """
    from app.services import doc_auto_approval as auto

    mode = auto.auto_approve_mode()
    result: dict = {"mode": mode, "since_hours": since_hours, "auto_approved": [], "held": [], "would_approve": [],
                    "truncated": {"auto_approved": False, "held": False},
                    "needs_ceo": [], "auto_fixing": []}
    rows = list(await conn.fetch(_AUTO_SQL, tenant_id, project_key, auto.ACTOR, since_hours))
    if len(rows) > _MAX_AUTO_ROWS:
        rows, result["truncated"]["auto_approved"] = rows[:_MAX_AUTO_ROWS], True
    for row in rows:
        risk = auto.classify_risk({"project_key": row["project_key"], "document_key": row["document_key"]},
                                  {"title": row["title"], "content": row["content"]})
        item = _auto_item(row, risk=risk, mode=mode, review_rows=row.get("review_rows"))
        item["approved_at"] = row["approved_at"].astimezone(KST).isoformat()
        # 자동승인 뒤 사람이 취소했거나 새 판본이 승인된 이력도 보여 준다. 승인 취소 버튼은 currently_approved 일 때만.
        item["currently_approved"] = bool(row.get("currently_approved", True))
        result["auto_approved"].append(item)
    # off 여도 보류 문서와 사유는 보여 준다(판정은 읽기 전용). would_approve 만 shadow 전용이다.
    candidates = list(await conn.fetch(_HELD_SQL, tenant_id, project_key, since_hours))
    if len(candidates) > _MAX_HELD_CANDIDATES:
        candidates, result["truncated"]["held"] = candidates[:_MAX_HELD_CANDIDATES], True
    for cand in candidates:
        ctx = await auto.assess(conn, cand["tenant_id"], cand["project_key"], cand["head_id"], cand["revision_id"])
        if not ctx:
            continue
        decision, head, revision = ctx["decision"], ctx["head"], ctx["revision"]
        if decision.action == "hold" or (mode == "shadow" and decision.action == "approve"):
            row = {"tenant_id": cand["tenant_id"], "project_key": head["project_key"], "document_key": head["document_key"],
                   "title": revision["title"], "kind": head["kind"], "doc_tier": revision.get("doc_tier"),
                   "revision_id": revision["id"], "version": revision.get("version"),
                   "generation": head["generation"]}
            rows_json = [dict(r) for r in ctx["rows"]]
            item = _auto_item(row, risk=decision.risk, mode=mode, reasons=decision.reasons,
                              score=decision.score, review_rows=rows_json)
            (result["held"] if decision.action == "hold" else result["would_approve"]).append(item)
            if decision.action == "hold":
                await _classify_held(conn, result, item, cand["tenant_id"], decision, head, revision, ctx["rows"])
    await _classify_migration_docs(conn, result, tenant_id, project_key, since_hours, mode)
    return result


# 마이그레이션 승인 문서: evaluate 는 skip(migration_document)이라 held 에 없다. 자동 보완도 하지 않으므로
# 최근 리뷰된 미승인 문서를 '대표님 판단 필요'로만 올린다(읽기 전용). 승인 자체는 마이그레이션 자동화 경로 소관이다.
_MIGRATION_SQL = (
    "SELECT h.id AS head_id, h.tenant_id::text AS tenant_id, h.project_key, r.id AS revision_id "
    "FROM project_document_heads h "
    "JOIN project_document_revisions r ON r.id=h.latest_revision_id AND r.head_id=h.id "
    "WHERE ($1::uuid IS NULL OR h.tenant_id=$1::uuid) AND ($2::text IS NULL OR h.project_key=$2) "
    "AND r.doc_tier IN ('D2','D3') AND h.approved_revision_id IS DISTINCT FROM h.latest_revision_id "
    "AND (h.document_key LIKE 'migration-apply-%' OR (lower(COALESCE(h.kind,''))='contract' "
    "AND (h.document_key ILIKE '%migration%' OR r.title ILIKE '%migration%' OR r.title LIKE '%마이그레이션%'))) "
    "AND EXISTS(SELECT 1 FROM project_document_reviews rv WHERE rv.revision_id=r.id "
    "AND rv.created_at > now() - make_interval(hours => $3::int)) "
    "ORDER BY h.updated_at DESC LIMIT 51"
)


async def _classify_migration_docs(conn: Any, result: dict, tenant_id: Any, project_key: Any, since_hours: int,
                                   mode: str) -> None:
    from app.services import doc_auto_approval as auto
    from app.services import doc_auto_fix as fix

    if fix.auto_fix_mode() != "on":
        return
    try:
        rows = list(await conn.fetch(_MIGRATION_SQL, tenant_id, project_key, since_hours))
    except Exception:
        logger.warning("canonical_review_digest_migration_failed", exc_info=True)
        return
    if len(rows) > 50:
        rows = rows[:50]
        result.setdefault("truncated", {})["needs_ceo"] = True
    for cand in rows:
        ctx = await auto.assess(conn, cand["tenant_id"], cand["project_key"], cand["head_id"], cand["revision_id"])
        if not ctx or "migration_document" not in (ctx["decision"].reasons or []):
            continue
        decision, head, revision = ctx["decision"], ctx["head"], ctx["revision"]
        row = {"tenant_id": cand["tenant_id"], "project_key": head["project_key"], "document_key": head["document_key"],
               "title": revision["title"], "kind": head["kind"], "doc_tier": revision.get("doc_tier"),
               "revision_id": revision["id"], "version": revision.get("version"), "generation": head["generation"]}
        item = _auto_item(row, risk=decision.risk, mode=mode, reasons=decision.reasons,
                          score=decision.score, review_rows=[dict(r) for r in ctx["rows"]])
        await _classify_held(conn, result, item, cand["tenant_id"], decision, head, revision, ctx["rows"])


async def _classify_held(conn: Any, result: dict, item: dict, tenant_id: Any, decision: Any, head: Any,
                         revision: Any, rows: Any) -> None:
    """보류 문서를 자동 보완 계획으로 나눈다(읽기 전용): 한도 초과 등은 needs_ceo, 보완 중·예정은 auto_fixing. 실패는 무시."""
    from app.services import doc_auto_fix as fix

    if fix.auto_fix_mode() != "on":
        return
    try:
        history = await fix.load_history(conn, tenant_id, head["project_key"], head["document_key"])
        state, mark = fix.round_state(history, revision)
        resolve = await _resolve_status(conn, tenant_id, head, revision)
        if resolve and resolve.get("active"):
            # 후속 자동 처리(독립 심판 뒤 조사·분할 작업)가 돌고 있다 — 대표님 판단 필요가 아니라 자동 보완 중이다.
            active = resolve["active"]
            result["auto_fixing"].append({"project_key": item["project_key"], "document_key": item["document_key"],
                                          "title": item["title"], "round": fix.DOC_AUTO_FIX_MAX_ROUNDS,
                                          "max_rounds": fix.DOC_AUTO_FIX_MAX_ROUNDS, "job_id": active.get("job_id"),
                                          "resolve_path": active.get("path")})
            return
        if state in ("busy", "submitted"):
            # 이미 보완 작업이 있다 — 결과가 나오기 전에 같은 지적으로 '반복 실패' 상신하지 않는다.
            result["auto_fixing"].append({"project_key": item["project_key"], "document_key": item["document_key"],
                                          "title": item["title"], "round": mark.get("round", 0),
                                          "max_rounds": fix.DOC_AUTO_FIX_MAX_ROUNDS, "job_id": mark.get("job_id")})
            return
        if state == "failed":
            plan = fix.FixPlan("escalate", ["fix_job_failed"], mark.get("round", 0), mark.get("fingerprint", ""))
        else:
            plan = fix.plan_auto_fix(decision, head, revision, rows, history)
        if plan.action in ('fix', 'escalate'):
            session_id, why = await fix.resolve_session_id(conn, tenant_id, head, revision)
            if session_id is None:
                plan = fix.without_session_plan(plan, why)
    except Exception:
        logger.warning("canonical_review_digest_fix_plan_failed", exc_info=True)
        return
    if plan.action == "fix":
        result["auto_fixing"].append({"project_key": item["project_key"], "document_key": item["document_key"],
                                      "title": item["title"], "round": plan.round, "max_rounds": fix.DOC_AUTO_FIX_MAX_ROUNDS})
    elif plan.action == "escalate":
        remaining = [f for f in fix.extract_findings(rows) if f["severity"] in ("blocker", "major")][:3]
        result["needs_ceo"].append({
            "project_key": item["project_key"], "document_key": item["document_key"], "title": item["title"],
            "risk": item["risk"], "round": plan.round, "max_rounds": fix.DOC_AUTO_FIX_MAX_ROUNDS,
            "reason": " / ".join(fix.reason_ko(r) for r in plan.reasons),
            "ceo_brief": item.get("ceo_brief"),
            "remaining_issues": [{"severity": f["severity"], "location": f["location"][:40], "problem": f["problem"][:200]}
                                 for f in remaining],
            "decision_needed": fix.DECISION_REQUEST_KO,
            "resolve_path": (resolve or {}).get("resolve_path", "none"),
            "card_id": (resolve or {}).get("card_id"),
            "pending_days": (resolve or {}).get("pending_days", 0)})


async def _resolve_status(conn: Any, tenant_id: Any, head: Any, revision: Any) -> dict | None:
    """후속 자동 처리(doc_auto_resolve) 상태 — 읽기 전용. off 이거나 실패하면 None."""
    try:
        from app.services import doc_auto_resolve as resolve

        if resolve.auto_resolve_mode() != "on":
            return None
        return await resolve.resolve_status(conn, tenant_id, head["project_key"], head["document_key"], revision["id"])
    except Exception:
        logger.warning("canonical_review_digest_resolve_status_failed", exc_info=True)
        return None


def auto_digest_is_empty(auto: dict | None) -> bool:
    return not auto or not (auto.get("auto_approved") or auto.get("held") or auto.get("would_approve")
                            or auto.get("needs_ceo") or auto.get("auto_fixing"))


def _short(title: str) -> str:
    return title if len(title) <= _TITLE_CHARS else title[:_TITLE_CHARS] + "…"


def _auto_section(auto: dict | None, auto_limit: int) -> list[str]:
    if auto_digest_is_empty(auto):
        return []
    lines: list[str] = []
    approved, held, would = auto["auto_approved"], auto["held"], auto.get("would_approve") or []
    if approved:
        lines.append(f"\n✅ <b>자동승인 {len(approved)}건</b> (참고용 — 필요하면 수정 요청)")
        for item in approved[:auto_limit]:
            flag = " 🔴고위험" if item["risk"] == "high" else ""
            score = f" · 점수 {item['score']:.2f}" if isinstance(item.get("score"), (int, float)) else ""
            lines.append(f"  • {html.escape(_short(item['title']))} ({html.escape(item['project_key'])}/"
                         f"{html.escape(item['document_key'])}){flag}{score} · 미검증 {item.get('unverified_count', 0)}건")
        if len(approved) > auto_limit:
            lines.append(f"  … 외 {len(approved) - auto_limit}건")
    if held:
        lines.append(f"\n⏸ <b>보류 {len(held)}건(수정 필요)</b>")
        for item in held[:PER_PROJECT_LIMIT]:
            reason = item["hold_reasons"][0] if item["hold_reasons"] else "자동승인 기준 미충족"
            flag = " 🔴고위험" if item["risk"] == "high" else ""
            lines.append(f"  • {html.escape(_short(item['title']))} ({html.escape(item['project_key'])}/"
                         f"{html.escape(item['document_key'])}){flag} — {html.escape(reason)} · 미검증 {item.get('unverified_count', 0)}건")
        if len(held) > PER_PROJECT_LIMIT:
            lines.append(f"  … 외 {len(held) - PER_PROJECT_LIMIT}건")
    if would:
        lines.append(f"\n🧪 (시험 모드) 켜면 자동승인될 문서 {len(would)}건")
    truncated = auto.get("truncated") or {}
    if truncated.get("auto_approved") or truncated.get("held"):
        lines.append("  ⚠️ 목록 상한을 넘어 일부만 표시했습니다 — 전체는 /docs 에서 확인")
    return lines


_CEO_DETAIL_LIMIT = 5


def _ceo_head(auto: dict | None) -> list[str]:
    """요약 맨 앞: 대표님 판단 필요 건수와 상세, 자동 보완 중 건수. 3900자 안에서도 항상 맨 위에 남는다."""
    needs = (auto or {}).get("needs_ceo") or []
    fixing = (auto or {}).get("auto_fixing") or []
    lines: list[str] = []
    if needs:
        lines.append(f"🚨 <b>대표님 판단 필요 {len(needs)}건</b>")
        for item in needs[:_CEO_DETAIL_LIMIT]:
            lines.append(f"  • {html.escape(_short(item['title']))} ({html.escape(item['project_key'])}/"
                         f"{html.escape(item['document_key'])}) · 자동 보완 {item['round']}/{item['max_rounds']}회")
            lines.append(f"    사유: {html.escape(item['reason'][:120])}")
            if item.get("ceo_brief"):
                lines.append(f"    요약: {html.escape(str(item['ceo_brief'])[:100])}")
            for issue in item["remaining_issues"]:
                lines.append(f"    - [{issue['severity']}] {html.escape(issue['problem'][:60])}")
            lines.append(f"    결정하실 것: {html.escape(item['decision_needed'])}")
            if item.get("card_id"):
                lines.append(f"    👉 버튼으로 결정 (승인 카드의 권장안) · 미결 {int(item.get('pending_days') or 0)}일")
        if len(needs) > _CEO_DETAIL_LIMIT:
            lines.append(f"  … 외 {len(needs) - _CEO_DETAIL_LIMIT}건")
    if fixing:
        lines.append(f"🔧 자동 보완 중 {len(fixing)}건")
    return lines + [""] if lines else lines


def format_digest(items: list[dict], auto: dict | None = None) -> str:
    by_project: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_project[item["project_key"]].append(item)
    lines = _ceo_head(auto) + (
        [f"📋 <b>검토 대기 정본 문서 {len(items)}건</b>"] if items else ["📋 <b>정본 문서 자동승인 참고검토</b>"])
    for project in sorted(by_project):
        docs = sorted(by_project[project], key=lambda d: (-d["waiting_days"], d["document_key"]))
        lines.append(f"\n<b>{html.escape(project)}</b> ({len(docs)}건)")
        for doc in docs[:PER_PROJECT_LIMIT]:
            title = doc["title"] if len(doc["title"]) <= _TITLE_CHARS else doc["title"][:_TITLE_CHARS] + "…"
            lines.append(f"  • {html.escape(title)} ({html.escape(doc['document_key'])}) — {doc['waiting_days']}일 대기")
        if len(docs) > PER_PROJECT_LIMIT:
            lines.append(f"  … 외 {len(docs) - PER_PROJECT_LIMIT}건")
    footer = "\n대시보드 '문서 &gt; 정본 문서'(/docs)에서 검토 후 승인해 주세요." if items else "\n자세히: /docs"
    # 3900자를 넘으면 자동승인 절부터 줄인다(검토 대기 절은 마지막까지 유지).
    base_len = len("\n".join(lines + [footer]))
    text = ""
    for limit in range(len((auto or {}).get("auto_approved") or []), -1, -1):
        section = _auto_section(auto, limit)
        text = "\n".join(lines + section + [footer])
        if len(text) <= MAX_MESSAGE_CHARS:
            break
    if not auto_digest_is_empty(auto) and base_len > MAX_MESSAGE_CHARS:
        text = "\n".join(lines + [footer])
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS].rsplit("\n", 1)[0] + "\n  … (이하 생략)"
    return text


def format_push_payload(items: list[dict], digest_date: date, auto: dict | None = None) -> dict:
    def plain(value: str) -> str:
        return " ".join(re.sub(r"<[^>]*>", "", html.unescape(value)).split())

    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[plain(item["project_key"])] += 1
    projects = ", ".join(f"{key} {count}건" for key, count in sorted(counts.items()))
    oldest = sorted(items, key=lambda item: (item["review_at"], item["document_key"]))[:3]
    titles = "; ".join(f"{plain(item['title'])[:40]} ({item['waiting_days']}일)" for item in oldest)
    extra = ""
    if not auto_digest_is_empty(auto):
        extra = f"\n자동승인 {len(auto['auto_approved'])}건 · 보류 {len(auto['held'])}건"
        if auto.get("needs_ceo"):
            extra = f"\n대표님 판단 필요 {len(auto['needs_ceo'])}건" + extra
    title = f"검토 대기 정본 {len(items)}건" if items else "정본 자동승인 참고검토"
    return {"title": title, "body": f"{projects}\n{titles}{extra}".strip()[:240],
            "url": "/docs", "tag": f"canonical-review-digest-{digest_date:%Y%m%d}"}


async def _default_sender(tenant_id: str, items: list[dict], *, conn: Any,
                          digest_date: date, auto: dict | None = None) -> str:
    from app.services.ceo_notify import send_ceo_text
    from app.services.push_notifications import is_web_push_configured, send_web_push_to_user

    reason = ""
    try:
        # Savepoint keeps a missing/unavailable membership table from aborting the daily transaction.
        async with conn.transaction():
            recipients = await conn.fetch(
                "SELECT DISTINCT user_id::text AS user_id FROM tenant_memberships "
                "WHERE tenant_id=$1::uuid AND role IN ('owner','admin') "
                "AND status='active' AND deleted_at IS NULL", tenant_id,
            )
    except Exception:
        recipients = []
    if not recipients:
        reason = "no_recipient"
    elif not is_web_push_configured():
        reason = "vapid_not_configured"
    if reason:
        logger.warning("canonical_review_digest_send_skipped", tenant_id=tenant_id, reason=reason)
        return "skipped"

    sent = 0
    payload = format_push_payload(items, digest_date, auto)
    for recipient in recipients:
        try:
            response = await send_web_push_to_user(
                tenant_id=tenant_id, user_id=recipient["user_id"], payload=payload,
            )
            sent += response.get("sent", 0)
        except Exception:
            logger.warning("canonical_review_digest_send_failed", tenant_id=tenant_id, reason="push_failed")
    # Telegram is supplementary; neither its return value nor exception determines success.
    try:
        await send_ceo_text(format_digest(items, auto))
    except Exception:
        logger.warning("canonical_review_digest_telegram_failed", tenant_id=tenant_id)
    if sent < 1:
        logger.warning("canonical_review_digest_send_failed", tenant_id=tenant_id, reason="push_failed")
        return "failed"
    return "sent"


async def send_daily_digest(pool: Any, *, now: datetime | None = None,
                            sender: Callable[[str], Awaitable[bool]] | None = None) -> dict:
    """KST 날짜·테넌트당 1회 발송. 두 블루그린 슬롯이 동시에 불러도 advisory lock 과 기록 PK 로 중복이 없다."""
    digest_date: date = (now or _kst_now()).astimezone(KST).date()
    result = {"date": digest_date.isoformat(), "sent": 0, "skipped": 0, "failed": 0}
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _LOCK_KEY)
        items = await fetch_review_queue(conn, None, now=now)
        by_tenant: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            by_tenant[item["tenant_id"]].append(item)
        auto_by_tenant: dict[str, dict] = {}
        try:
            # 자동승인 참고절은 보조 정보다. 실패해도 기존 검토 대기 요약은 그대로 나간다.
            from app.services import doc_auto_approval as _auto
            tenant_rows = await conn.fetch(_AUTO_TENANTS_SQL, _auto.ACTOR, 24)
            for tenant_row in tenant_rows:
                tid = tenant_row["tenant_id"]
                async with conn.transaction():
                    tenant_auto = await fetch_auto_approval_digest(conn, tid)
                if not auto_digest_is_empty(tenant_auto):
                    auto_by_tenant[tid] = tenant_auto
        except Exception:
            logger.warning("canonical_review_digest_auto_section_failed")
        for tenant_id in auto_by_tenant:
            by_tenant.setdefault(tenant_id, [])
        for tenant_id, tenant_items in by_tenant.items():
            auto = auto_by_tenant.get(tenant_id)
            if await conn.fetchval(
                "SELECT 1 FROM canonical_review_digest_log WHERE digest_date=$1 AND tenant_id=$2::uuid",
                digest_date, tenant_id,
            ):
                result["skipped"] += 1
                continue
            if sender is not None:
                # Preserve the existing text/bool injection interface.
                outcome = "sent" if await sender(format_digest(tenant_items, auto)) else "failed"
                if outcome == "failed":
                    logger.warning("canonical_review_digest_send_failed", tenant_id=tenant_id, reason="push_failed")
            else:
                outcome = await _default_sender(tenant_id, tenant_items, conn=conn, digest_date=digest_date, auto=auto)
            if outcome != "sent":
                result[outcome] += 1
                continue
            await conn.execute(
                "INSERT INTO canonical_review_digest_log(digest_date,tenant_id,pending_count) "
                "VALUES($1,$2::uuid,$3) ON CONFLICT DO NOTHING",
                digest_date, tenant_id, len(tenant_items),
            )
            result["sent"] += 1
    logger.info("canonical_review_digest_done", **result)
    return result
