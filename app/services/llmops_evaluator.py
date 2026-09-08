"""LLMOps rule evaluator — LLM 비용 0으로 trace/example을 점수화한다.

PRD 완료 기준 §10.4의 5개 기준을 규칙으로 채점한다.

1. `source_presence`  — 재현에 필요한 근거(입력/graph_run_id/도구)가 남았는가
2. `tool_policy`      — 위험 도구가 승인 상태 없이 실행되지 않았는가
3. `final_response`   — 최종 응답이 저장되었는가
4. `cost_present`     — 비용/토큰 근거가 있는가
5. `error_classification` — 실패가 분류되었는가, 성공이 에러를 숨기지 않았는가

LLM-as-judge는 별도 opt-in 축이며 이 모듈은 순수 규칙만 쓴다. 채점은 결정적이라
같은 example은 항상 같은 점수를 낸다.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.llmops_store import (
    DATASET_TABLE,
    EXAMPLE_TABLE,
    EXPERIMENT_TABLE,
    SCORE_TABLE,
    _as_json,
    classify_error,
    loads_json,
    relation_exists,
)

logger = logging.getLogger(__name__)

EVALUATOR_NAME = "rule"
EVALUATOR_VERSION = "rule-v1"
PASS_THRESHOLD = 0.6

# 승인 게이트가 필요한 risk tier (app.services.ohvis_harness.RISK_POLICIES와 동일 축)
APPROVAL_REQUIRED_TIERS = frozenset({"write", "deploy", "auth", "financial"})
DESTRUCTIVE_TIER = "destructive"
APPROVED_STATES = frozenset({"approved", "approved_by_ceo", "auto_approved", "not_required"})


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _criterion(name: str, score: float, comment: str, evidence: dict[str, Any]) -> dict[str, Any]:
    bounded = max(0.0, min(1.0, float(score)))
    return {
        "criterion": name,
        "score": round(bounded, 4),
        "passed": bounded >= PASS_THRESHOLD,
        "comment": comment,
        "evidence": evidence,
    }


def _check_source_presence(payload: dict[str, Any]) -> dict[str, Any]:
    signals = {
        "input": bool(_text(payload.get("input_summary")).strip()),
        "graph_run_id": bool(payload.get("graph_run_id")),
        "tool_calls": bool(payload.get("tool_calls")),
    }
    present = sum(1 for value in signals.values() if value)
    return _criterion(
        "source_presence",
        present / len(signals),
        f"근거 {present}/{len(signals)}개 존재",
        signals,
    )


def _check_tool_policy(payload: dict[str, Any]) -> dict[str, Any]:
    calls = payload.get("tool_calls") or []
    if not calls:
        return _criterion("tool_policy", 1.0, "도구 호출 없음 — 정책 위반 없음", {"tool_calls": 0})
    violations: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = _text(call.get("tool_name") or call.get("name") or "tool")
        tier = _text(call.get("risk_tier") or "read").lower()
        state = _text(call.get("approval_state") or "").lower()
        if tier == DESTRUCTIVE_TIER:
            violations.append(f"{name}: destructive tier executed")
        elif tier in APPROVAL_REQUIRED_TIERS and state not in APPROVED_STATES:
            violations.append(f"{name}: {tier} tier without approval")
    return _criterion(
        "tool_policy",
        0.0 if violations else 1.0,
        "; ".join(violations[:5]) if violations else f"{len(calls)}건 모두 정책 만족",
        {"tool_calls": len(calls), "violations": violations[:10]},
    )


def _check_final_response(payload: dict[str, Any]) -> dict[str, Any]:
    output = _text(payload.get("output_summary")).strip()
    has_output = bool(output)
    return _criterion(
        "final_response",
        1.0 if has_output else 0.0,
        f"최종 응답 요약 저장됨 ({len(output)}자)" if has_output else "최종 응답이 남지 않음",
        {"output_length": len(output)},
    )


def _check_cost_present(payload: dict[str, Any]) -> dict[str, Any]:
    cost = payload.get("cost_usd")
    if cost is not None:
        try:
            return _criterion(
                "cost_present", 1.0, f"cost_usd={float(cost):.6f}", {"cost_usd": float(cost)}
            )
        except (TypeError, ValueError):
            pass
    for key in ("total_tokens", "prompt_tokens", "completion_tokens", "usage"):
        if payload.get(key) is not None:
            return _criterion("cost_present", 1.0, f"{key} 기록됨", {key: payload.get(key)})
    return _criterion("cost_present", 0.0, "비용/토큰 근거 없음", {"cost_usd": None})


def _check_error_classification(payload: dict[str, Any]) -> dict[str, Any]:
    status = _text(payload.get("status")).lower()
    error = _text(payload.get("error")).strip()
    error_class = _text(payload.get("error_class")).strip() or _text(classify_error(error))
    if status == "error" or error:
        classified = bool(error_class) and error_class != "unknown"
        return _criterion(
            "error_classification",
            1.0 if classified else 0.0,
            f"에러가 {error_class}로 분류됨" if classified else "에러가 분류되지 않음",
            {"status": status or "error", "error_class": error_class or None},
        )
    if error_class:
        return _criterion(
            "error_classification",
            0.0,
            f"성공 상태인데 에러 신호({error_class})가 남아 있음",
            {"status": status or "success", "error_class": error_class},
        )
    return _criterion(
        "error_classification", 1.0, "에러 없는 성공", {"status": status or "success"}
    )


CHECKS = (
    _check_source_presence,
    _check_tool_policy,
    _check_final_response,
    _check_cost_present,
    _check_error_classification,
)


def evaluate_payload(payload: dict[str, Any], rubric: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """trace payload 1건을 5개 규칙으로 채점한다. DB/네트워크를 쓰지 않는다."""
    payload = payload if isinstance(payload, dict) else {}
    rubric = rubric or {}
    criteria: list[dict[str, Any]] = []
    for check in CHECKS:
        try:
            criteria.append(check(payload))
        except Exception as exc:  # noqa: BLE001 — 규칙 하나가 평가 전체를 깨지 않는다
            criteria.append(_criterion(
                getattr(check, "__name__", "check").removeprefix("_check_"),
                0.0,
                f"evaluator error: {str(exc)[:120]}",
                {},
            ))

    # latency는 rubric에 상한이 있을 때만 추가 기준으로 본다.
    max_latency = rubric.get("max_latency_ms")
    latency = payload.get("latency_ms")
    if max_latency and latency is not None:
        try:
            within = int(latency) <= int(max_latency)
            criteria.append(_criterion(
                "latency_budget",
                1.0 if within else 0.0,
                f"latency {latency}ms / 상한 {max_latency}ms",
                {"latency_ms": latency, "max_latency_ms": max_latency},
            ))
        except (TypeError, ValueError):
            pass

    overall = sum(item["score"] for item in criteria) / len(criteria)
    failed = [item["criterion"] for item in criteria if not item["passed"]]
    return {
        "evaluator": EVALUATOR_NAME,
        "evaluator_version": EVALUATOR_VERSION,
        "criteria": criteria,
        "overall": round(overall, 4),
        "passed": overall >= PASS_THRESHOLD,
        "comment": "all rule checks passed" if not failed else "failed: " + ", ".join(failed),
    }


def evaluate_example(example: dict[str, Any]) -> dict[str, Any]:
    """llmops_examples 행 1건을 채점한다.

    승격 시점의 trace payload를 `metadata.trace_payload`에 보존하므로 그것을
    우선 채점하고, 없으면 example 자체 필드로 평가한다.
    """
    example = example if isinstance(example, dict) else {}
    metadata = loads_json(example.get("metadata"))
    payload = metadata.get("trace_payload")
    if not isinstance(payload, dict):
        payload = {
            "input_summary": example.get("input"),
            "output_summary": metadata.get("output_summary"),
            "status": metadata.get("source_status"),
            "error_class": metadata.get("source_error_class"),
            "graph_run_id": metadata.get("graph_run_id"),
            "tool_calls": metadata.get("tool_calls") or [],
        }
    result = evaluate_payload(payload, loads_json(example.get("rubric")))
    result["example_id"] = example.get("id")
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """실험 전체 요약 — 평균 점수, 통과율, 기준별 평균."""
    if not results:
        return {"examples": 0, "mean_score": 0.0, "pass_rate": 0.0, "criteria": {}}
    per_criterion: dict[str, list[float]] = {}
    for result in results:
        for item in result["criteria"]:
            per_criterion.setdefault(item["criterion"], []).append(item["score"])
    return {
        "examples": len(results),
        "mean_score": round(sum(item["overall"] for item in results) / len(results), 4),
        "pass_rate": round(sum(1 for item in results if item["passed"]) / len(results), 4),
        "criteria": {
            name: round(sum(scores) / len(scores), 4) for name, scores in per_criterion.items()
        },
    }


async def run_experiment(
    *,
    dataset_slug: Optional[str] = None,
    dataset_id: Optional[str] = None,
    name: str = "",
    candidate_sha: Optional[str] = None,
    model_id: Optional[str] = None,
    limit: int = 200,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    """dataset의 example들을 rule evaluator로 채점하고 실험 결과를 저장한다 (FR-006)."""
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        if not await relation_exists(conn, EXAMPLE_TABLE):
            return {"ok": False, "reason": "llmops_tables_missing"}

        if dataset_id:
            dataset = await conn.fetchrow(
                f"SELECT id, slug, project FROM {DATASET_TABLE} WHERE id = $1::uuid", dataset_id
            )
        else:
            dataset = await conn.fetchrow(
                f"SELECT id, slug, project FROM {DATASET_TABLE} WHERE slug = $1", dataset_slug
            )
        if dataset is None:
            return {
                "ok": False,
                "reason": "dataset_not_found",
                "dataset_slug": dataset_slug,
                "dataset_id": dataset_id,
            }

        rows = await conn.fetch(
            f"""
            SELECT id, source_trace_id, input, expected, rubric, metadata
            FROM {EXAMPLE_TABLE} WHERE dataset_id = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            dataset["id"],
            int(limit),
        )
        examples = [
            {
                "id": str(row["id"]),
                "source_trace_id": str(row["source_trace_id"]) if row["source_trace_id"] else None,
                "input": row["input"],
                "expected": row["expected"],
                "rubric": loads_json(row["rubric"]),
                "metadata": loads_json(row["metadata"]),
            }
            for row in rows
        ]

        results = [evaluate_example(example) for example in examples]
        summary = summarize(results)

        experiment_id = await conn.fetchval(
            f"""
            INSERT INTO {EXPERIMENT_TABLE}
                (dataset_id, name, evaluator, evaluator_version, candidate_sha,
                 model_id, status, summary, created_by, completed_at)
            VALUES ($1, $2, $3, $4, $5, $6, 'completed', $7::jsonb, $8, NOW())
            RETURNING id
            """,
            dataset["id"],
            (name or f"{dataset['slug']} {EVALUATOR_VERSION}")[:200],
            EVALUATOR_NAME,
            EVALUATOR_VERSION,
            candidate_sha,
            model_id,
            _as_json(summary),
            created_by,
        )

        for example, result in zip(examples, results):
            for item in result["criteria"]:
                await conn.execute(
                    f"""
                    INSERT INTO {SCORE_TABLE}
                        (experiment_id, example_id, source_trace_id, evaluator,
                         evaluator_version, criterion, score, passed, comment, evidence)
                    VALUES ($1, $2::uuid, $3::text, $4, $5, $6, $7, $8, $9, $10::jsonb)
                    """,
                    experiment_id,
                    example["id"],
                    example["source_trace_id"],
                    EVALUATOR_NAME,
                    EVALUATOR_VERSION,
                    item["criterion"],
                    item["score"],
                    item["passed"],
                    item["comment"][:500],
                    _as_json(item["evidence"]),
                )

        return {
            "ok": True,
            "experiment_id": str(experiment_id),
            "dataset_id": str(dataset["id"]),
            "dataset_slug": dataset["slug"],
            "evaluator": EVALUATOR_NAME,
            "evaluator_version": EVALUATOR_VERSION,
            "summary": summary,
        }


async def get_experiment(experiment_id: str) -> Optional[dict[str, Any]]:
    """실험 결과 + 기준별 점수를 반환한다. 없으면 None."""
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        if not await relation_exists(conn, EXPERIMENT_TABLE):
            return None
        row = await conn.fetchrow(
            f"""
            SELECT e.id, e.dataset_id, d.slug AS dataset_slug, e.name, e.evaluator,
                   e.evaluator_version, e.candidate_sha, e.model_id, e.status,
                   e.summary, e.created_by, e.started_at, e.completed_at
            FROM {EXPERIMENT_TABLE} e
            JOIN {DATASET_TABLE} d ON d.id = e.dataset_id
            WHERE e.id = $1::uuid
            """,
            experiment_id,
        )
        if row is None:
            return None
        scores = await conn.fetch(
            f"""
            SELECT example_id, criterion, score, passed, comment
            FROM {SCORE_TABLE} WHERE experiment_id = $1::uuid
            ORDER BY criterion, example_id
            """,
            experiment_id,
        )
        return {
            "id": str(row["id"]),
            "dataset_id": str(row["dataset_id"]),
            "dataset_slug": row["dataset_slug"],
            "name": row["name"],
            "evaluator": row["evaluator"],
            "evaluator_version": row["evaluator_version"],
            "candidate_sha": row["candidate_sha"],
            "model_id": row["model_id"],
            "status": row["status"],
            "summary": loads_json(row["summary"]),
            "created_by": row["created_by"],
            "started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
            "scores": [
                {
                    "example_id": str(score["example_id"]) if score["example_id"] else None,
                    "criterion": score["criterion"],
                    "score": score["score"],
                    "passed": score["passed"],
                    "comment": score["comment"],
                }
                for score in scores
            ],
        }
