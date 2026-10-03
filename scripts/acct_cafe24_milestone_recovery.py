#!/usr/bin/env python3
"""ACCT 카페24 이전 목표(df479771) 마일스톤 자동진행 복구 — 이 목표 하나에만 적용 (기본 dry-run).

2026-10-03 CEO 질문 "왜 자동으로 진행이 안되지?" 의 원인 4가지를 한 번에 고친다.

  1. auto_advance=false      검증된 완료 뒤 다음 마일스톤을 자동 시작하지 않았다.
  2. M3 허위 완료            마일스톤 완료 판정(check_milestone_completion)은 "활성 링크가 전부
                             done" 이면 증거 없이 completed 로 만든다. 러너 종료(runner-362971f1,
                             결과문 자체가 "M3 완료로 신고하지 않았다")가 기능 완료로 읽혔다.
  3. 소유자 불일치           마일스톤 소유가 목표 담당(acc75e55)이 아닌 AADS-011 도구관리 세션
                             (8ad08cc2, 이 목표 외 링크 없음, 10-01 이후 무활동)이다. 디스패치가
                             엉뚱한 세션으로 간다.
  4. 실행 가능한 재작업 없음 M1/M2 의 재작업 후보는 만료된 카드·답이 없는 큐로 소진됐다.

하지 않는 것: 재작업 예산 초기화(후속 R2 작업 소관), approval_policy 변경, goal 상태 변경
(실패 링크가 남아 있는 동안 blocked 유지), 다른 목표/세션 소유 이동, 증거 삭제.

모든 쓰기는 tenant+goal 조건 + 행 잠금 + 전제 조건 재검증이라 재실행해도 no-op 이다.

사용 (운영 컨테이너 안):
    docker exec -i aads-server python3 - < scripts/acct_cafe24_milestone_recovery.py            # dry-run
    docker exec -i aads-server python3 - < scripts/acct_cafe24_milestone_recovery.py --apply
(stdin 실행에서는 argv 를 못 받으므로 ACCT_RECOVERY_APPLY=1 환경변수로도 켠다.)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

for _root in (Path(__file__).resolve().parents[1] if "__file__" in globals() else None, Path("/app")):
    if _root and (_root / "app").is_dir():
        sys.path.insert(0, str(_root))
        break

GOAL_ID = "df479771-f250-4a11-90a3-220432da2bfa"
TENANT_ID = "2d701a8c-9596-4757-8588-faa4f7837112"
RECOVERY_KEY = "acct-cafe24-milestone-auto-recovery-20261003-r1"
HANDOVER_PROJECT = "AADS"

# 완료 증거로 세지 않는 evidence 키 — 재작업 장부/이 스크립트의 기록은 증거가 아니다.
NON_COMPLETION_EVIDENCE_KEYS = frozenset(
    {"retry_candidates", "retry_candidates_history", "acct_cafe24_recovery"}
)
TERMINAL_JOB_STATES = frozenset(
    {"done", "completed", "error", "failed", "cancelled", "rejected", "rejected_done", "review_failed"}
)

# 어느 링크가 어떤 마일스톤의 가짜 신호인지 — 실측(2026-10-03)으로 확인한 것만 둔다.
M3_FALSE_COMPLETION_JOB = "runner-362971f1"
M1_STALE_PENDING_JOB = "runner-3fec3696"

COMMON_GUARDS = (
    "범위: ACCT 카페24 이전 한정. 금융 주문·자금 집행·대량 수집 아님. 진아서버 접속/사용 금지, "
    "라일론·귀속불명 자료 적재 금지, 금융 조회 확대 금지.\n"
    "러너 규칙: git add/commit/push/build/deploy 를 직접 하지 않는다. 결과문에 실행한 검증과 "
    "못 한 검증을 구분해 적는다. 실행하지 않은 검사를 통과로 쓰지 않는다.\n"
    "독립 검수 기준(마일스톤 completion_criteria)은 바꾸지 않는다. 이미 검증된 부분은 다시 하지 말고 유지한다.\n"
)


def completion_evidence(evidence: Any) -> dict:
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            evidence = {}
    if not isinstance(evidence, dict):
        return {}
    return {k: v for k, v in evidence.items() if k not in NON_COMPLETION_EVIDENCE_KEYS and v}


def is_false_completion(ms: dict) -> bool:
    """completed 인데 보고·검수·증거가 하나도 없다 → 링크 done 만으로 만들어진 완료."""
    return (
        ms.get("status") == "completed"
        and not completion_evidence(ms.get("evidence"))
        and ms.get("reported_at") is None
        and not (ms.get("review_note") or "").strip()
    )


def remediation_steps(state: dict) -> list[dict]:
    """M1/M2/M3 각각의 실행 가능한 재작업 지시. RETRY_OF_LINK 카드 경로가 아니라
    SUPERSEDES/AUTO_REWORK_OF 헤더 경로를 쓴다 — 후보 예산을 건드리지 않는다."""
    by_seq = {m["sequence_order"]: m for m in state["milestones"]}
    failed = {
        l["task_id"] for l in state["links"]
        if l["task_type"] == "pipeline_job" and l["status"] == "failed"
        and (l.get("link_state") or "active") == "active"
    }
    steps = []

    def ids(milestone_id: str) -> list[str]:
        return sorted(
            l["task_id"] for l in state["links"]
            if l["milestone_id"] == milestone_id and l["task_id"] in failed
        )

    m1, m2, m3 = by_seq.get(1), by_seq.get(2), by_seq.get(3)
    if m1 and m1["status"] == "blocked" and ids(m1["id"]):
        failed_ids = ids(m1["id"])
        steps.append({
            "key": f"{RECOVERY_KEY}:M1", "milestone_id": m1["id"], "supersedes": failed_ids,
            "title": f"[복구 M1] {m1['title']} 재작업 — 리뷰 지적 반영",
            "detail": (
                f"RECOVERY_KEY: {RECOVERY_KEY}:M1\nGOAL_ID: {GOAL_ID}\nMILESTONE_ID: {m1['id']}\n"
                f"AUTO_REWORK_OF: {failed_ids[0]}\nSUPERSEDES: {' '.join(failed_ids)}\n"
                "REMEDIATION_PURPOSE: failed_link_retry\n"
                "runner-4b4e75f4 는 REQUEST_CHANGES(0.635)로 반려됐다. 그 리뷰 피드백(pipeline_jobs.review_feedback)과 "
                "결과문을 먼저 읽고, 현재 HEAD 기준으로 지적 항목만 고쳐 다시 제출한다. "
                "runner-3fec3696 은 인증 회귀 테스트만 추가했고 M1 인수 기준(독립 실행 기반·설정 분리)을 "
                "충족하지 않았으므로 완료 근거로 쓰지 않는다.\n" + COMMON_GUARDS
            ),
        })
    if m2 and m2["status"] == "blocked" and ids(m2["id"]):
        failed_ids = ids(m2["id"])
        lead = "runner-6aec5740" if "runner-6aec5740" in failed_ids else failed_ids[-1]
        steps.append({
            "key": f"{RECOVERY_KEY}:M2", "milestone_id": m2["id"], "supersedes": failed_ids,
            "title": f"[복구 M2] {m2['title']} 재작업 — 리뷰 지적 반영",
            "detail": (
                f"RECOVERY_KEY: {RECOVERY_KEY}:M2\nGOAL_ID: {GOAL_ID}\nMILESTONE_ID: {m2['id']}\n"
                f"AUTO_REWORK_OF: {lead}\nSUPERSEDES: {' '.join(failed_ids)}\n"
                "REMEDIATION_PURPOSE: failed_link_retry\n"
                "runner-6aec5740 은 코드·격리 검증을 마쳤으나 REQUEST_CHANGES(0.698)로 반려됐고, "
                "runner-3c5d0d58 은 approval_commit_failed, runner-94ff86f5 는 결과 없이 종료됐다. "
                "리뷰 피드백을 읽고 지적 항목을 고친 뒤 인증·조직·권한 검증을 다시 제출한다. "
                "앞서 이 마일스톤으로 간 자동 큐 두 건은 결과 job 을 만들지 못했다.\n" + COMMON_GUARDS
            ),
        })
    rewound_by_us = m3 and m3["status"] == "pending" and RECOVERY_KEY in (m3.get("dispatch_note") or "")
    if m3 and (m3["id"] in state.get("false_completed", ()) or rewound_by_us):
        steps.append({
            "key": f"{RECOVERY_KEY}:M3", "milestone_id": m3["id"], "supersedes": [],
            "title": f"[복구 M3] {m3['title']} — 실제 이전 검증 증거 확보",
            "detail": (
                f"RECOVERY_KEY: {RECOVERY_KEY}:M3\nGOAL_ID: {GOAL_ID}\nMILESTONE_ID: {m3['id']}\n"
                "REMEDIATION_PURPOSE: false_completion_rework\n"
                "M3 는 증거 없이 completed 로 기록돼 pending 으로 되돌렸다. runner-362971f1 의 결과는 "
                "'공개 전환 불가, M3 완료로 신고하지 않음' 이다. 이미 확인된 항목(DB 재조회 수치 일치, 격리 파일 테스트 8/8, "
                "파일 분류 결과)은 유지하고, 남은 미충족 항목(귀속 근거 확보, 업무 DB·파일·자동수집 이전 검증)만 "
                "M1/M2 가 풀린 뒤 증거와 함께 report_done 으로 보고한다. 귀속 불명 자료를 적재해 숫자를 맞추지 않는다.\n"
                + COMMON_GUARDS
            ),
        })
    return steps


def build_plan(state: dict) -> list[dict]:
    """상태 스냅샷 → 해야 할 일. 이미 수렴했으면 빈 목록 (재실행 no-op)."""
    plan: list[dict] = []
    goal = state["goal"]
    if str(goal["tenant_id"]) != TENANT_ID or str(goal["id"]) != GOAL_ID:
        return [{"kind": "abort", "why": "tenant_or_goal_mismatch"}]
    links = state["links"]
    ms_by_id = {m["id"]: m for m in state["milestones"]}

    state["false_completed"] = set()
    for m in state["milestones"]:
        if is_false_completion(m) and m["sequence_order"] == 3:
            state["false_completed"].add(m["id"])
            for l in links:
                if (l["milestone_id"] == m["id"] and l["task_id"] == M3_FALSE_COMPLETION_JOB
                        and (l.get("link_state") or "active") == "active"):
                    plan.append({"kind": "detach_link", "link_id": l["id"], "milestone_id": m["id"],
                                 "reason": "허위 완료 차단: 러너 종료(결과문에 M3 미완료 명시)가 기능 완료로 집계됨"})
            plan.append({"kind": "rewind", "milestone_id": m["id"],
                         "reason": "증거·보고·검수 없는 completed 를 공식 rewind 로 pending 복귀 (허위 완료)"})

    for l in links:
        if (l["task_id"] == M1_STALE_PENDING_JOB and l["status"] == "pending"
                and (l.get("link_state") or "active") == "active"
                and state["jobs"].get(l["task_id"], {}).get("status") in TERMINAL_JOB_STATES):
            plan.append({"kind": "detach_link", "link_id": l["id"], "milestone_id": l["milestone_id"],
                         "reason": "job 은 종료됐는데 링크만 pending(stale). 결과는 M1 인수 기준을 충족하지 않아 완료 근거로 쓰지 않음"})

    missing = [m["id"] for m in state["milestones"] if m["auto_advance"] is not True]
    if missing:
        plan.append({"kind": "auto_advance", "milestone_ids": missing})

    owner = goal.get("owner_session_id")
    if owner and state.get("owner_linked"):
        wrong = [m["id"] for m in state["milestones"]
                 if m["status"] != "completed" and m.get("owner_session_id") not in (None, owner)
                 and ms_by_id[m["id"]].get("owner_linked_same_goal")]
        if wrong:
            plan.append({"kind": "owner", "milestone_ids": wrong, "to": owner})

    for step in remediation_steps(state):
        if state["live_markers"].get(step["key"]):
            continue
        if state["live_jobs"].get(step["milestone_id"]):
            continue
        plan.append({"kind": "remediate", **step})
    return plan


async def load_state(conn, tenant_id: str, goal_id: str) -> dict:
    goal = await conn.fetchrow(
        "SELECT id::text AS id, tenant_id::text AS tenant_id, status, owner_session_id::text AS owner_session_id "
        "FROM goals WHERE id = $1::uuid AND tenant_id = $2::uuid", goal_id, tenant_id)
    if not goal:
        raise SystemExit("goal_not_found_for_tenant")
    milestones = [dict(r) for r in await conn.fetch(
        "SELECT id::text AS id, title, sequence_order, status, auto_advance, owner_session_id::text AS owner_session_id, "
        "evidence, reported_at, review_note, dispatch_count, dispatch_note, completed_at "
        "FROM milestones WHERE goal_id = $1::uuid AND tenant_id = $2::uuid ORDER BY sequence_order", goal_id, tenant_id)]
    links = [dict(r) for r in await conn.fetch(
        "SELECT id::text AS id, milestone_id::text AS milestone_id, task_type, task_id, status, link_state, detach_reason "
        "FROM goal_task_links WHERE goal_id = $1::uuid AND tenant_id = $2::uuid", goal_id, tenant_id)]
    job_ids = [l["task_id"] for l in links if l["task_type"] == "pipeline_job"]
    jobs = {r["job_id"]: dict(r) for r in await conn.fetch(
        "SELECT job_id, status, phase FROM pipeline_jobs WHERE job_id = ANY($1::text[])", job_ids)}
    chat_links = {l["task_id"] for l in links
                  if l["task_type"] == "chat_session" and (l["link_state"] or "active") == "active"}
    owner = goal["owner_session_id"]
    for m in milestones:
        m["owner_linked_same_goal"] = bool(m["owner_session_id"]) and m["owner_session_id"] in chat_links
    keys = [f"{RECOVERY_KEY}:M{i}" for i in (1, 2, 3)]
    live_markers = {}
    if owner:
        for k in keys:
            live_markers[k] = bool(await conn.fetchval(
                "SELECT 1 FROM chat_deferred_reactions WHERE session_id = $1::uuid "
                "AND status IN ('pending','claimed','completed') AND system_message LIKE '%RECOVERY_KEY: ' || $2 || '%' LIMIT 1",
                owner, k))
    live_jobs = {}
    for m in milestones:
        rows = await conn.fetch(
            "SELECT job_id FROM pipeline_jobs WHERE tenant_id = $3::uuid AND status NOT IN "
            "('done','completed','error','failed','cancelled','rejected','rejected_done','review_failed') "
            "AND instruction LIKE '%MILESTONE_ID: ' || $1 || '%' AND instruction LIKE '%GOAL_ID: ' || $2 || '%'",
            m["id"], goal_id, tenant_id)
        live_jobs[m["id"]] = [r["job_id"] for r in rows]
    return {"goal": dict(goal), "milestones": milestones, "links": links, "jobs": jobs,
            "owner_linked": owner in chat_links, "live_markers": live_markers, "live_jobs": live_jobs}


def snapshot(state: dict) -> dict:
    return {
        "goal": state["goal"],
        "milestones": [
            {k: (str(v) if v is not None and not isinstance(v, (int, bool, str, dict, list)) else v)
             for k, v in m.items() if k not in ("evidence",)} | {"evidence_keys": sorted(
                 (json.loads(m["evidence"]) if isinstance(m["evidence"], str) else (m["evidence"] or {})).keys())}
            for m in state["milestones"]
        ],
        "links": [{k: l[k] for k in ("id", "milestone_id", "task_id", "status", "link_state", "detach_reason")}
                  for l in state["links"] if l["task_type"] == "pipeline_job"],
    }


async def apply_action(conn, action: dict, tenant_id: str, goal_id: str) -> dict:
    kind = action["kind"]
    if kind == "detach_link":
        async with conn.transaction():
            tag = await conn.execute(
                "UPDATE goal_task_links SET link_state = 'detached', detach_reason = $3, reconciled_at = NOW(), updated_at = NOW() "
                "WHERE id = $1::uuid AND goal_id = $4::uuid AND tenant_id = $2::uuid AND COALESCE(link_state,'active') = 'active'",
                action["link_id"], tenant_id, f"[{RECOVERY_KEY}] {action['reason']}"[:500], goal_id)
        return {"detached": tag}
    if kind == "rewind":
        from app.services.goal_intervene import rewind  # 공식 경로 — 발송 기록도 같이 되돌린다
        async with conn.transaction():
            locked = await conn.fetchrow(
                "SELECT status, evidence, reported_at, review_note FROM milestones "
                "WHERE id = $1::uuid AND goal_id = $3::uuid AND tenant_id = $2::uuid FOR UPDATE",
                action["milestone_id"], tenant_id, goal_id)
            if not locked or not is_false_completion(dict(locked)):
                return {"rewind": "skipped_precondition_changed"}
        res = await rewind(action["milestone_id"], f"{RECOVERY_KEY}: {action['reason']}", tenant_id=tenant_id)
        return {"rewind": res}
    if kind == "auto_advance":
        async with conn.transaction():
            tag = await conn.execute(
                "UPDATE milestones SET auto_advance = TRUE, updated_at = NOW() WHERE goal_id = $1::uuid "
                "AND tenant_id = $2::uuid AND id = ANY($3::uuid[]) AND auto_advance IS DISTINCT FROM TRUE",
                goal_id, tenant_id, action["milestone_ids"])
        return {"auto_advance": tag}
    if kind == "owner":
        async with conn.transaction():
            tag = await conn.execute(
                "UPDATE milestones SET owner_session_id = $4::uuid, updated_at = NOW() WHERE goal_id = $1::uuid "
                "AND tenant_id = $2::uuid AND id = ANY($3::uuid[]) AND status <> 'completed' "
                "AND owner_session_id IS DISTINCT FROM $4::uuid "
                "AND EXISTS (SELECT 1 FROM goal_task_links l WHERE l.goal_id = $1::uuid AND l.tenant_id = $2::uuid "
                "            AND l.task_type = 'chat_session' AND l.task_id = $5::text AND COALESCE(l.link_state,'active') = 'active')",
                goal_id, tenant_id, action["milestone_ids"], action["to"], action["to"])
        return {"owner": tag}
    if kind == "remediate":
        from app.services.next_step_proposals import propose
        owner = await conn.fetchval(
            "SELECT owner_session_id::text FROM goals WHERE id = $1::uuid AND tenant_id = $2::uuid", goal_id, tenant_id)
        res = await propose(
            owner,
            [{"title": action["title"], "detail": action["detail"], "risk": "high",
              "rollback": "미착수 큐 항목은 취소하면 된다. 러너 작업은 승인 전 반려 가능(커밋·배포 전)."}],
            context=f"{RECOVERY_KEY} — 목표 자동진행 복구", tenant_id=tenant_id)
        entry = {"key": action["key"], "queue": [a.get("queue_id") for a in res.get("auto", [])],
                 "cards": [c.get("id") for c in res.get("cards", [])], "supersedes": action["supersedes"]}
        async with conn.transaction():
            await conn.execute(
                "UPDATE milestones SET evidence = COALESCE(evidence, '{}'::jsonb) || jsonb_build_object("
                "'acct_cafe24_recovery', COALESCE(evidence->'acct_cafe24_recovery', '[]'::jsonb) || $3::jsonb), "
                "updated_at = NOW() WHERE id = $1::uuid AND tenant_id = $2::uuid",
                action["milestone_id"], tenant_id, json.dumps([entry]))
        return {"remediate": entry, "raw_error": res.get("error")}
    raise ValueError(kind)


async def main(apply: bool) -> int:
    from app.core.db_pool import get_pool, init_pool

    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        before = await load_state(conn, TENANT_ID, GOAL_ID)
        plan = build_plan(before)
        print(json.dumps({"mode": "apply" if apply else "dry-run", "plan": plan, "before": snapshot(before)},
                         ensure_ascii=False, indent=1, default=str))
        if not apply or plan and plan[0]["kind"] == "abort":
            return 0
        results = [await apply_action(conn, a, TENANT_ID, GOAL_ID) for a in plan]
        after = await load_state(conn, TENANT_ID, GOAL_ID)
        blockers = [m["title"] for m in after["milestones"] if m["status"] == "blocked"]
        print(json.dumps({"results": results, "after": snapshot(after), "goal_status_kept": after["goal"]["status"],
                          "blocked_milestones": blockers, "remaining_plan": build_plan(after)},
                         ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__" or "__file__" not in globals():
    _apply = "--apply" in sys.argv or os.getenv("ACCT_RECOVERY_APPLY") == "1"
    raise SystemExit(asyncio.run(main(_apply)))
