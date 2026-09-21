"""Best-effort shadow reviews that never participate in the approval verdict.

The production runner review contract remains CLI-only.  This module samples a
bounded number of already-reviewed jobs with a local PC model so its quality and
availability can be measured before anyone considers promoting it into the
authoritative review chain.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_ADVISORY_TASKS: set[asyncio.Task] = set()
_ADVISORY_PROMPT_MAX_CHARS = int(os.environ.get("REVIEW_ADVISORY_PROMPT_MAX_CHARS", "40000"))
_ADVISORY_SYSTEM_PROMPT = """코드 변경을 독립적으로 검수하고 JSON 객체 하나만 출력하세요.
필수 숫자 키 correctness, security, scope_compliance, preservation, quality는 각각 0과 1 사이 값입니다.
필수 배열 키 issues와 suggestions에는 구체적인 문자열만 넣고, summary는 한 줄 문자열로 작성하세요.
기존 함수/API 삭제, 허용 범위 밖 변경, 보안 취약점은 점수를 크게 낮추고 issues에 적으세요.
마크다운 코드펜스, verdict 키, JSON 앞뒤 설명은 출력하지 마세요."""


def _advisory_messages(prompt: str) -> list[dict[str, str]]:
    """Build a bounded request tuned for the local Qwen native chat parser."""
    text = str(prompt or "")
    if len(text) > _ADVISORY_PROMPT_MAX_CHARS:
        head_chars = int(_ADVISORY_PROMPT_MAX_CHARS * 0.75)
        tail_chars = _ADVISORY_PROMPT_MAX_CHARS - head_chars
        text = (
            text[:head_chars]
            + "\n\n[보조 리뷰 입력 절단: 중간 diff 생략]\n\n"
            + text[-tail_chars:]
        )
    return [
        {"role": "system", "content": _ADVISORY_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]


def _safe_text(value: object, limit: int = 500) -> str:
    """Bound persisted diagnostics and avoid echoing common secret forms."""
    from app.services.code_reviewer import _sanitize_review_text

    return _sanitize_review_text(value, limit=limit)


def _advisory_verdict(payload: dict[str, Any] | None) -> tuple[str | None, float | None]:
    """Return the same coarse verdict/weighted score used by the main reviewer."""
    if not isinstance(payload, dict):
        return None, None
    required = ("correctness", "security", "scope_compliance", "preservation", "quality")
    try:
        score = (
            float(payload["correctness"]) * 0.30
            + float(payload["security"]) * 0.25
            + float(payload["scope_compliance"]) * 0.20
            + float(payload["preservation"]) * 0.15
            + float(payload["quality"]) * 0.10
        )
    except (KeyError, TypeError, ValueError):
        return None, None
    if not all(0.0 <= float(payload[field]) <= 1.0 for field in required):
        return None, None
    score = min(1.0, max(0.0, score))
    if score >= 0.7:
        verdict = "APPROVE"
    elif score >= 0.4:
        verdict = "REQUEST_CHANGES"
    else:
        verdict = "FLAG"
    return verdict, score


async def _claim_sample(*, job_id: str, project: str, primary_verdict: str,
                        primary_model: str, diff_size: int) -> dict[str, Any] | None:
    """Atomically reserve one of the bounded shadow-review observations."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        config = await conn.fetchrow(
                """
                SELECT model_id, max_samples, timeout_seconds
                FROM runner_review_advisory_config
                WHERE config_key = 'default' AND enabled = TRUE
                FOR UPDATE
                """
        )
        if not config:
            return None
        used = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM code_review_advisories
                WHERE model_id = $1 AND status IN ('running', 'completed', 'failed')
                """,
                config["model_id"],
        )
        if int(used or 0) >= int(config["max_samples"]):
            return None
        inserted = await conn.fetchrow(
                """
                INSERT INTO code_review_advisories
                    (job_id, project, model_id, status, primary_verdict,
                     primary_model, diff_size, started_at)
                VALUES ($1, $2, $3, 'running', $4, $5, $6, NOW())
                ON CONFLICT (job_id, model_id) DO NOTHING
                RETURNING id
                """,
                job_id,
                project,
                config["model_id"],
                primary_verdict,
                primary_model,
                diff_size,
        )
        if not inserted:
            return None
        return {
            "id": inserted["id"],
            "model_id": config["model_id"],
            "timeout_seconds": int(config["timeout_seconds"]),
        }


async def _finish_sample(sample_id: object, *, status: str,
                         advisory_verdict: str | None = None,
                         advisory_score: float | None = None,
                         agrees_with_primary: bool | None = None,
                         response: dict[str, Any] | None = None,
                         error: str | None = None) -> None:
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE code_review_advisories
            SET status = $2,
                advisory_verdict = $3,
                advisory_score = $4,
                agrees_with_primary = $5,
                response = $6::jsonb,
                error = $7,
                completed_at = NOW()
            WHERE id = $1
            """,
            sample_id,
            status,
            advisory_verdict,
            advisory_score,
            agrees_with_primary,
            json.dumps(response or {}, ensure_ascii=False),
            _safe_text(error, limit=500) if error else None,
        )


async def _run_advisory(*, project: str, job_id: str, prompt: str,
                        primary_verdict: str, primary_model: str,
                        diff_size: int) -> None:
    sample: dict[str, Any] | None = None
    try:
        sample = await _claim_sample(
            job_id=job_id,
            project=project,
            primary_verdict=primary_verdict,
            primary_model=primary_model,
            diff_size=diff_size,
        )
        if not sample:
            return

        from app.api.pc_ollama_bridge import _run_pc_ollama_chat
        from app.services.code_reviewer import _parse_review_json

        timeout_seconds = int(sample["timeout_seconds"])
        raw = await asyncio.wait_for(
            _run_pc_ollama_chat(
                {
                    "model": sample["model_id"],
                    "messages": _advisory_messages(prompt),
                    "temperature": 0.1,
                    "max_tokens": 1024,
                    "timeout_seconds": timeout_seconds,
                    "think": False,
                }
            ),
            timeout=timeout_seconds + 5,
        )
        content = str(raw.get("content") or "")
        parsed = _parse_review_json(content)
        advisory_verdict, advisory_score = _advisory_verdict(parsed)
        if advisory_verdict is None:
            await _finish_sample(
                sample["id"],
                status="failed",
                response={
                    "response_chars": len(content),
                    "response_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "preview": _safe_text(content),
                },
                error="invalid advisory review structure",
            )
            return
        await _finish_sample(
            sample["id"],
            status="completed",
            advisory_verdict=advisory_verdict,
            advisory_score=advisory_score,
            agrees_with_primary=advisory_verdict == primary_verdict,
            response={
                "summary": _safe_text(parsed.get("summary", ""), limit=1000),
                "issues": [
                    _safe_text(issue, limit=1000)
                    for issue in parsed.get("issues", [])[:20]
                ] if isinstance(parsed.get("issues"), list) else [],
                "response_chars": len(content),
                "prompt_tokens": int(raw.get("prompt_tokens") or 0),
                "completion_tokens": int(raw.get("completion_tokens") or 0),
            },
        )
    except Exception as exc:  # noqa: BLE001 - advisory failures must not affect runner verdicts
        logger.warning("review_advisory_failed: job_id=%s error=%s", job_id, _safe_text(exc, 160))
        if sample:
            try:
                await _finish_sample(sample["id"], status="failed", error=str(exc))
            except Exception as persist_exc:  # noqa: BLE001 - logging is the final fallback
                logger.warning(
                    "review_advisory_persist_failed: job_id=%s error=%s",
                    job_id,
                    _safe_text(persist_exc, 160),
                )


def schedule_review_advisory(*, project: str, job_id: str, prompt: str,
                             primary_verdict: str, primary_model: str,
                             diff_size: int) -> None:
    """Launch a bounded shadow review without delaying the authoritative verdict."""
    task = asyncio.create_task(
        _run_advisory(
            project=project,
            job_id=job_id,
            prompt=prompt,
            primary_verdict=primary_verdict,
            primary_model=primary_model,
            diff_size=diff_size,
        ),
        name=f"review-advisory-{job_id}",
    )
    _ADVISORY_TASKS.add(task)
    task.add_done_callback(_ADVISORY_TASKS.discard)
