"""AI 리뷰 반려(REQUEST_CHANGES) 뒤 자동 재작업 제출 (AADS-RUNNER-AUTO-REWORK).

왜 필요한가.
러너는 리뷰가 REQUEST_CHANGES 를 내면 작업을 status='error', phase='review_failed'
로 끝내고, notify 경로는 종료 상태라며 채팅 반응까지 막는다. 제출 시 받은
``max_cycles``(기본 3)는 리뷰 반려에 쓰이지 않았다 — 2026-09-29 AADS 리뷰
19건 중 14건이 반려됐고 ``code_reviews.review_cycle`` 은 전부 1이었다.
그래서 반려가 날 때마다 누군가 R2, R3 … 을 손으로 다시 써야 했고,
그 사이 목표 자동진행은 멈춰 있었다.

여기서는 반려 1건에 재작업 1건을 만든다. 원 지시는 그대로 두고 리뷰 지적과
보존된 산출물 위치를 덧붙인다. 상한은 ``max_cycles - 1`` 라운드(최대 3)이고,
상한을 넘으면 더 제출하지 않고 사람 판단으로 넘긴다.

끄는 법: 환경변수 ``PIPELINE_AUTO_REWORK=0`` 또는 지시서에 ``NO_AUTO_REWORK`` 행.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

AUTO_REWORK_ROUND_RE = re.compile(r"^AUTO_REWORK_ROUND:\s*(\d+)\s*/\s*(\d+)\s*$", re.M)
_OPT_OUT_RE = re.compile(r"^\s*NO_AUTO_REWORK\s*$", re.M)
_SECTION_MARK = "## 자동 재작업 — AI 리뷰 반려 지적 교정"
_HEADER_KEYS = ("ALLOW_DUP_JOB", "AUTO_REWORK_OF:", "AUTO_REWORK_ROUND:")
_MAX_ROUNDS_CAP = 3
_MAX_ISSUES = 8
_MAX_ISSUE_CHARS = 600
AUTO_REWORK_FAILURE_PHASES = frozenset({
    "review_failed", "push_stale_base", "deploy_isolated_push_state",
    "deploy_isolated_stale_approval",
})


def auto_rework_enabled() -> bool:
    return os.getenv("PIPELINE_AUTO_REWORK", "1").strip().lower() not in {"0", "false", "off", "no"}


def is_request_changes_failure(status: str, phase: str, error_detail: str) -> bool:
    """Bounded rework for code review rejection or a recoverable deploy gate."""
    if status != "error":
        return False
    if phase not in AUTO_REWORK_FAILURE_PHASES and phase != "error":
        return False
    if phase == "review_failed":
        return (error_detail or "").startswith("review_failed: verdict=REQUEST_CHANGES")
    if phase == "push_stale_base":
        return True
    # The older runner stored gate reasons in phase or error_detail.  The
    # stale_approval value only occurs in legacy rows; current preflight
    # classifies stale_base before reaching this point.
    if phase == "deploy_isolated_stale_approval" or phase == "error" and error_detail == "deploy_isolated_stale_approval":
        return True
    if phase == "deploy_isolated_push_state" or phase == "error" and error_detail.startswith("deploy_isolated_push_state"):
        # Old generic rows contain no state and cannot distinguish a network
        # failure from stale_base.  Only an explicit stale_base is recoverable.
        return "stale_base" in error_detail and "fetch_fail" not in error_detail
    return False


def max_rounds(max_cycles: Any) -> int:
    try:
        cycles = int(max_cycles or 3)
    except (TypeError, ValueError):
        cycles = 3
    return max(0, min(cycles - 1, _MAX_ROUNDS_CAP))


def current_round(instruction: str) -> int:
    m = AUTO_REWORK_ROUND_RE.search(instruction or "")
    return int(m.group(1)) if m else 0


def strip_rework_scaffold(instruction: str) -> str:
    """이전 라운드가 붙인 머리 행과 재작업 절을 걷어내 원 지시만 남긴다.

    남겨 두면 라운드마다 지적 목록이 겹겹이 쌓여 지시가 흐려진다.
    """
    text = instruction or ""
    idx = text.find(_SECTION_MARK)
    if idx >= 0:
        text = text[:idx]
    lines = text.split("\n")
    while lines and lines[0].strip().startswith(_HEADER_KEYS):
        lines.pop(0)
    return "\n".join(lines).rstrip() + "\n"


def extract_issues(feedback: Any) -> list[str]:
    """code_reviews.feedback(jsonb 또는 문자열)에서 지적 목록을 꺼낸다."""
    if isinstance(feedback, (bytes, bytearray)):
        feedback = feedback.decode("utf-8", "replace")
    data = feedback
    if isinstance(feedback, str):
        try:
            data = json.loads(feedback)
        except ValueError:
            data = {"summary": feedback}
    issues: list[str] = []
    if isinstance(data, dict):
        raw = data.get("issues") or []
        if isinstance(raw, list):
            issues = [str(x).strip() for x in raw if str(x).strip()]
        if not issues and data.get("summary"):
            issues = [str(data["summary"]).strip()]
    return [i[:_MAX_ISSUE_CHARS] for i in issues[:_MAX_ISSUES]]


def build_rework_instruction(
    *,
    original: str,
    parent_job_id: str,
    round_no: int,
    rounds_max: int,
    issues: list[str],
    commit_hash: str = "",
    score: Any = None,
    failure_phase: str = "review_failed",
) -> str:
    base = strip_rework_scaffold(original)
    header = (
        "ALLOW_DUP_JOB\n"
        f"AUTO_REWORK_OF: {parent_job_id}\n"
        f"AUTO_REWORK_ROUND: {round_no}/{rounds_max}\n"
    )
    start = f"`/tmp/aads-wt-{parent_job_id}`"
    if commit_hash:
        start += f" (커밋 `{commit_hash[:12]}`)"
    issue_lines = "\n".join(f"{i}. {text}" for i, text in enumerate(issues, 1))
    if not issue_lines:
        issue_lines = "1. (종료 사유와 최신 origin/main 기준을 확인하라)"
    score_txt = f" score={score}" if score is not None else ""
    reason = "AI 리뷰 반려" if failure_phase == "review_failed" else f"배포 게이트 {failure_phase}"
    section = (
        f"\n{_SECTION_MARK} (라운드 {round_no}/{rounds_max})\n"
        f"직전 작업 {parent_job_id} 이 {reason}로 중단돼 자동으로 다시 제출됐다{score_txt}.\n"
        f"- 출발점: 반려된 산출물 {start}. 처음부터 다시 쓰지 말고, 그 변경을 origin/main 기준 "
        "깨끗한 worktree 로 가져와 이어서 고쳐라. 산출물이 없으면 위 원 지시대로 구현하라.\n"
        "- 아래 지적을 **전부** 고치고, 각 지적을 어떻게 처리했는지 RESULT 에 번호별로 적어라. "
        "지적이 틀렸다고 판단하면 고치지 말고 근거(코드 위치·테스트)를 적어라.\n"
        "- 원 지시의 범위·금지사항·검증 명령은 그대로다. 지적과 무관한 변경은 하지 마라.\n\n"
        f"리뷰 지적:\n{issue_lines}\n"
    )
    return header + base + section


def _hash(project: str, instruction: str) -> str:
    # app.api.pipeline_runner._compute_instruction_hash 와 같은 식이어야 중복 차단이 맞물린다.
    return hashlib.sha256(f"{project}:{instruction}".encode()).hexdigest()[:16]


async def maybe_submit_auto_rework(conn, job_id: str) -> dict | None:
    """반려된 job 에 대한 재작업을 최대 1건 만든다. 만들었으면 새 job 정보를 돌려준다.

    같은 부모로 두 번 불려도(notify 중복) 부모당 하나만 생긴다 — advisory lock +
    ``AUTO_REWORK_OF`` 존재 검사.
    """
    if not auto_rework_enabled():
        return None
    row = await conn.fetchrow(
        """
        SELECT job_id, project, instruction, chat_session_id, status, phase, error_detail,
               max_cycles, model, size, worker_model, model_override_reason, parallel_group,
               tenant_id::text AS tenant_id, commit_hash, review_score, goal_id, milestone_id
        FROM pipeline_jobs WHERE job_id = $1
        """,
        job_id,
    )
    if not row:
        return None
    if not is_request_changes_failure(row["status"] or "", row["phase"] or "", row["error_detail"] or ""):
        return None
    instruction = row["instruction"] or ""
    if _OPT_OUT_RE.search(instruction):
        return {"skipped": "opt_out", "parent": job_id}
    rounds_max = max_rounds(row["max_cycles"])
    next_round = current_round(instruction) + 1
    if next_round > rounds_max:
        logger.info("pipeline_auto_rework.cap_reached", job_id=job_id, rounds_max=rounds_max)
        return {"skipped": "cap_reached", "parent": job_id, "rounds_max": rounds_max}

    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", f"pipeline_auto_rework:{job_id}"
        )
        existing = await conn.fetchval(
            "SELECT job_id FROM pipeline_jobs WHERE instruction LIKE $1 LIMIT 1",
            f"%AUTO_REWORK_OF: {job_id}\n%",
        )
        if existing:
            return {"skipped": "already_submitted", "parent": job_id, "job_id": existing}
        feedback = await conn.fetchval(
            "SELECT feedback FROM code_reviews WHERE job_id = $1 ORDER BY created_at DESC LIMIT 1",
            job_id,
        )
        issues = extract_issues(feedback)
        if row["phase"] != "review_failed":
            issues = [f"배포 게이트 원인: {(row['error_detail'] or row['phase'])[:_MAX_ISSUE_CHARS]}"]
        new_instruction = build_rework_instruction(
            original=instruction,
            parent_job_id=job_id,
            round_no=next_round,
            rounds_max=rounds_max,
            issues=issues,
            commit_hash=row["commit_hash"] or "",
            score=row["review_score"],
            failure_phase=row["phase"],
        )
        new_job_id = f"runner-{uuid.uuid4().hex[:8]}"
        await conn.execute(
            """
            INSERT INTO pipeline_jobs
              (job_id, project, instruction, instruction_hash, chat_session_id,
               status, phase, max_cycles, model, size, worker_model, model_override_reason,
               parallel_group, review_feedback, logs, goal_id, milestone_id,
               created_at, updated_at, tenant_id)
            VALUES ($1, $2, $3, $4, $5, 'queued', 'queued', $6, $7, $8, $9, $10, $11, $12,
                    jsonb_build_array(jsonb_build_object('ts', NOW()::text,
                        'event', 'auto_rework_submitted', 'parent', $13::text)),
                    $14, $15, NOW(), NOW(), $16::uuid)
            """,
            new_job_id, row["project"], new_instruction, _hash(row["project"], new_instruction),
            row["chat_session_id"], row["max_cycles"], row["model"], row["size"],
            row["worker_model"], row["model_override_reason"], row["parallel_group"],
            f"[Auto Rework] {job_id} 반려 지적 {len(issues)}건 교정 라운드 {next_round}/{rounds_max}",
            job_id, row["goal_id"], row["milestone_id"], row["tenant_id"],
        )
        await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", new_job_id)
        if row["chat_session_id"]:
            await conn.execute(
                """INSERT INTO chat_messages (session_id,role,content,intent,cost,tokens_in,tokens_out,attachments,sources,tools_called)
                VALUES($1::uuid,'assistant',$2,'runner_notification',0,0,0,'[]'::jsonb,'[]'::jsonb,'[]'::jsonb)""",
                row["chat_session_id"],
                f"🔁 [자동 재작업] `{job_id}` 리뷰 반려 지적 {len(issues)}건을 고치는 작업 `{new_job_id}` 를 "
                f"자동 제출했습니다 (라운드 {next_round}/{rounds_max}). 상한에 닿으면 자동 제출을 멈추고 판단을 요청합니다.",
            )
            await conn.execute(
                "UPDATE chat_sessions SET message_count=message_count+1, updated_at=NOW() WHERE id=$1::uuid",
                row["chat_session_id"],
            )
    logger.info("pipeline_auto_rework.submitted", parent=job_id, job_id=new_job_id, round=next_round)
    return {
        "job_id": new_job_id,
        "parent": job_id,
        "round": next_round,
        "rounds_max": rounds_max,
        "project": row["project"],
        "instruction": new_instruction,
        "goal_id": row["goal_id"],
        "milestone_id": row["milestone_id"],
    }
