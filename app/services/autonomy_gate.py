"""
점진적 자율성 게이트 (T-009) — AADS 프로덕션 전환.

PostgreSQL에 태스크 유형별 성공률 추적 테이블 관리.
성공률 기반으로 HITL(Human-In-The-Loop) 체크포인트 자동 조정:

  Judge 통과율 ≥ 90% (최근 50건) → 해당 유형 자동 승인 전환
  사용자 수정 요청 ≤ 10%         → 체크포인트 간소화
  성공률 < 70%                   → HITL 재활성화
  최소 20건 미만                  → 항상 HITL 유지

DDL: autonomy_stats 테이블은 init_autonomy_schema() 호출 시 자동 생성.
"""
from __future__ import annotations

import structlog
from datetime import datetime
from typing import Optional, Dict, Any, Literal

logger = structlog.get_logger()

# ─── 임계값 상수 ─────────────────────────────────────────────────────────────
MIN_SAMPLE_SIZE = 20          # 최소 샘플 수 (미만이면 항상 HITL)
WINDOW_SIZE = 50              # 성공률 계산 윈도우 (최근 N건)
AUTO_APPROVE_THRESHOLD = 0.90 # 자동 승인 전환 임계값 (Judge 통과율)
SIMPLIFY_THRESHOLD = 0.10     # 체크포인트 간소화 임계값 (사용자 수정 요청 비율 이하)
HITL_REACTIVATE_THRESHOLD = 0.70  # HITL 재활성화 임계값 (성공률 미만)

AutonomyLevel = Literal["full_hitl", "simplified_hitl", "auto_approve"]


# ─── DDL ─────────────────────────────────────────────────────────────────────
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS autonomy_stats (
    id              BIGSERIAL PRIMARY KEY,
    task_type       TEXT        NOT NULL,
    task_id         TEXT        NOT NULL,
    project_id      TEXT        NOT NULL DEFAULT '',
    judge_verdict   TEXT        NOT NULL,  -- 'pass' | 'fail' | 'conditional_pass'
    user_modified   BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_autonomy_stats_task_type_created
    ON autonomy_stats (task_type, created_at DESC);
CREATE TABLE IF NOT EXISTS autonomy_levels (
    task_type       TEXT        PRIMARY KEY,
    level           TEXT        NOT NULL DEFAULT 'full_hitl',
    judge_pass_rate FLOAT       NOT NULL DEFAULT 0.0,
    user_modify_rate FLOAT      NOT NULL DEFAULT 1.0,
    sample_count    INT         NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def init_autonomy_schema(conn) -> None:
    """autonomy_stats, autonomy_levels 테이블 생성 (idempotent)."""
    try:
        await conn.execute(CREATE_TABLE_SQL)
        logger.info("autonomy_schema_initialized")
    except Exception as e:
        logger.warning("autonomy_schema_init_failed", error=str(e))


# ─── 기록 ─────────────────────────────────────────────────────────────────────
async def record_task_result(
    conn,
    task_type: str,
    task_id: str,
    judge_verdict: str,
    user_modified: bool = False,
    project_id: str = "",
) -> None:
    """
    태스크 완료 결과 기록.
    judge_verdict: 'pass' | 'fail' | 'conditional_pass'
    user_modified: 사용자가 결과를 수정 요청했으면 True
    """
    try:
        await conn.execute(
            """
            INSERT INTO autonomy_stats
                (task_type, task_id, project_id, judge_verdict, user_modified)
            VALUES ($1, $2, $3, $4, $5)
            """,
            task_type, task_id, project_id, judge_verdict, user_modified,
        )
        logger.info(
            "autonomy_record_saved",
            task_type=task_type,
            verdict=judge_verdict,
            user_modified=user_modified,
        )
    except Exception as e:
        logger.warning("autonomy_record_failed", error=str(e))


# ─── 성공률 계산 ──────────────────────────────────────────────────────────────
async def compute_success_rate(
    conn,
    task_type: str,
    window: int = WINDOW_SIZE,
) -> Dict[str, Any]:
    """
    최근 window건의 Judge 통과율 및 사용자 수정율 계산.

    Returns:
        {
            "task_type": str,
            "sample_count": int,
            "judge_pass_rate": float,   # pass + conditional_pass / total
            "user_modify_rate": float,  # user_modified=True / total
        }
    """
    try:
        rows = await conn.fetch(
            """
            SELECT judge_verdict, user_modified
            FROM autonomy_stats
            WHERE task_type = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            task_type, window,
        )
        if not rows:
            return {
                "task_type": task_type,
                "sample_count": 0,
                "judge_pass_rate": 0.0,
                "user_modify_rate": 1.0,
            }

        total = len(rows)
        passed = sum(
            1 for r in rows if r["judge_verdict"] in ("pass", "conditional_pass")
        )
        modified = sum(1 for r in rows if r["user_modified"])

        return {
            "task_type": task_type,
            "sample_count": total,
            "judge_pass_rate": passed / total,
            "user_modify_rate": modified / total,
        }
    except Exception as e:
        logger.warning("autonomy_compute_failed", error=str(e))
        return {
            "task_type": task_type,
            "sample_count": 0,
            "judge_pass_rate": 0.0,
            "user_modify_rate": 1.0,
        }


# ─── 자율성 수준 결정 ─────────────────────────────────────────────────────────
async def evaluate_autonomy_level(
    conn,
    task_type: str,
) -> Dict[str, Any]:
    """
    태스크 유형의 자율성 수준을 계산하고 autonomy_levels 테이블에 업데이트.

    Returns:
        {
            "task_type": str,
            "level": AutonomyLevel,
            "judge_pass_rate": float,
            "user_modify_rate": float,
            "sample_count": int,
            "reason": str,
        }
    """
    stats = await compute_success_rate(conn, task_type)
    sample_count = stats["sample_count"]
    pass_rate = stats["judge_pass_rate"]
    modify_rate = stats["user_modify_rate"]

    # ── 결정 로직 ──────────────────────────────────────────────────────
    if sample_count < MIN_SAMPLE_SIZE:
        level: AutonomyLevel = "full_hitl"
        reason = f"샘플 부족 ({sample_count} < {MIN_SAMPLE_SIZE}건) — 항상 HITL"
    elif pass_rate < HITL_REACTIVATE_THRESHOLD:
        level = "full_hitl"
        reason = (
            f"성공률 {pass_rate:.1%} < {HITL_REACTIVATE_THRESHOLD:.0%} "
            f"— HITL 재활성화"
        )
    elif pass_rate >= AUTO_APPROVE_THRESHOLD and modify_rate <= SIMPLIFY_THRESHOLD:
        level = "auto_approve"
        reason = (
            f"Judge 통과율 {pass_rate:.1%} ≥ {AUTO_APPROVE_THRESHOLD:.0%} "
            f"& 수정율 {modify_rate:.1%} ≤ {SIMPLIFY_THRESHOLD:.0%} "
            f"— 자동 승인 전환"
        )
    elif modify_rate <= SIMPLIFY_THRESHOLD:
        level = "simplified_hitl"
        reason = (
            f"수정율 {modify_rate:.1%} ≤ {SIMPLIFY_THRESHOLD:.0%} "
            f"— 체크포인트 간소화"
        )
    else:
        level = "full_hitl"
        reason = (
            f"성공률 {pass_rate:.1%}, 수정율 {modify_rate:.1%} "
            f"— 표준 HITL 유지"
        )

    logger.info(
        "autonomy_level_evaluated",
        task_type=task_type,
        level=level,
        pass_rate=f"{pass_rate:.2%}",
        modify_rate=f"{modify_rate:.2%}",
        sample_count=sample_count,
        reason=reason,
    )

    # autonomy_levels 테이블 upsert
    try:
        await conn.execute(
            """
            INSERT INTO autonomy_levels
                (task_type, level, judge_pass_rate, user_modify_rate, sample_count, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (task_type) DO UPDATE SET
                level = EXCLUDED.level,
                judge_pass_rate = EXCLUDED.judge_pass_rate,
                user_modify_rate = EXCLUDED.user_modify_rate,
                sample_count = EXCLUDED.sample_count,
                updated_at = NOW()
            """,
            task_type, level, pass_rate, modify_rate, sample_count,
        )
    except Exception as e:
        logger.warning("autonomy_level_upsert_failed", error=str(e))

    return {
        "task_type": task_type,
        "level": level,
        "judge_pass_rate": pass_rate,
        "user_modify_rate": modify_rate,
        "sample_count": sample_count,
        "reason": reason,
    }


# ─── 체크포인트 필요 여부 ─────────────────────────────────────────────────────
async def needs_hitl_checkpoint(
    conn,
    task_type: str,
    stage: str,
) -> bool:
    """
    해당 task_type + stage에서 HITL 체크포인트가 필요한지 반환.

    auto_approve  → False (체크포인트 불필요)
    simplified_hitl → 핵심 단계(final_review)만 True
    full_hitl     → 항상 True
    """
    try:
        row = await conn.fetchrow(
            "SELECT level FROM autonomy_levels WHERE task_type = $1",
            task_type,
        )
        level = row["level"] if row else "full_hitl"
    except Exception:
        level = "full_hitl"

    if level == "auto_approve":
        return False
    elif level == "simplified_hitl":
        # 핵심 단계만 체크포인트
        return stage in ("final_review", "plan_review")
    else:
        return True


# ─── 전체 통계 조회 ───────────────────────────────────────────────────────────
async def get_all_autonomy_levels(conn) -> list:
    """모든 태스크 유형의 자율성 수준 조회."""
    try:
        rows = await conn.fetch(
            "SELECT * FROM autonomy_levels ORDER BY updated_at DESC"
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("autonomy_get_all_failed", error=str(e))
        return []
