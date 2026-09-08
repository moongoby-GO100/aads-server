"""OHVIS LLMOps evaluation: dataset promotion + rule evaluator.

Rule evaluator first (PRD 5.4 비용): scoring a trace costs zero LLM calls, so
regression checks can run on every candidate SHA. LLM-as-judge stays an opt-in
follow-up (P1-2) and is intentionally not wired here.

The five rule checks are the completion criteria from PRD §10.4:
source presence, tool policy, final response persistence, cost present,
error classification.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.services.llmops_store import (
    _PoolConn,
    clip,
    get_trace,
    relation_exists,
)

logger = logging.getLogger(__name__)

EVALUATOR_NAME = "rule_v1"
EVALUATOR_VERSION = "v1"

DATASET_TABLE = "llmops_datasets"
EXAMPLE_TABLE = "llmops_examples"
EXPERIMENT_TABLE = "llmops_experiments"
SCORE_TABLE = "llmops_scores"

# Risk tiers whose calls must carry an approval decision to pass tool policy.
APPROVAL_REQUIRED_TIERS = {"write", "deploy", "auth", "financial", "destructive"}
APPROVED_STATES = {"approved", "auto_approved", "rejected", "not_required"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class DatasetPromotionError(ValueError):
    """Raised when a trace cannot be promoted into an eval dataset."""


def slugify(value: str, fallback: str = "llmops-dataset") -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return (slug or fallback)[:80]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


# ── rule evaluator ────────────────────────────────────────────────────────


def _check_source_presence(trace: dict[str, Any]) -> dict[str, Any]:
    """A trace must be attributable: graph run, session, task, or an OHVIS task."""
    evidence = {
        key: bool(trace.get(key))
        for key in ("graph_run_id", "session_id", "ohvis_task_id", "project")
    }
    metadata = _as_dict(trace.get("metadata"))
    evidence["metadata_component"] = bool(metadata.get("component"))
    present = sum(1 for value in evidence.values() if value)
    return {
        "key": "source_presence",
        "score": min(1.0, present / 3),
        "passed": bool(trace.get("graph_run_id")) and present >= 2,
        "comment": f"{present}/5 provenance fields present",
        "evidence": evidence,
    }


def _check_tool_policy(trace: dict[str, Any]) -> dict[str, Any]:
    """Every risky tool call must carry an approval decision."""
    calls = _as_list(trace.get("tool_calls"))
    if not calls:
        return {
            "key": "tool_policy",
            "score": 1.0,
            "passed": True,
            "comment": "no tool calls to police",
            "evidence": {"tool_call_count": 0},
        }

    violations: list[str] = []
    for call in calls:
        entry = _as_dict(call)
        tier = str(entry.get("risk_tier") or "read").lower()
        state = str(entry.get("approval_state") or "not_required").lower()
        name = str(entry.get("tool_name") or entry.get("name") or "unknown")
        if tier == "destructive":
            violations.append(f"{name}: destructive tier is never allowed")
        elif tier in APPROVAL_REQUIRED_TIERS and state not in APPROVED_STATES:
            violations.append(f"{name}: {tier} tier without approval decision ({state})")

    ok = len(calls) - len(violations)
    return {
        "key": "tool_policy",
        "score": ok / len(calls),
        "passed": not violations,
        "comment": "all tool calls within policy" if not violations else "; ".join(violations[:5]),
        "evidence": {"tool_call_count": len(calls), "violations": violations[:10]},
    }


def _check_final_response(trace: dict[str, Any]) -> dict[str, Any]:
    """A finished trace must have persisted an output (or an explicit error)."""
    output = (trace.get("output_summary") or "").strip()
    status = str(trace.get("status") or "").lower()
    has_error = bool((trace.get("error") or "").strip())
    persisted = bool(output) or has_error
    if status == "running":
        return {
            "key": "final_response_persistence",
            "score": 0.5,
            "passed": False,
            "comment": "trace still running; no terminal output yet",
            "evidence": {"status": status, "output_len": len(output)},
        }
    return {
        "key": "final_response_persistence",
        "score": 1.0 if persisted else 0.0,
        "passed": persisted,
        "comment": "final output persisted" if persisted else "terminal trace has no output or error",
        "evidence": {"status": status, "output_len": len(output), "has_error": has_error},
    }


def _check_cost_present(trace: dict[str, Any]) -> dict[str, Any]:
    """Cost/latency must be observable, otherwise budget control is blind."""
    cost = trace.get("cost_usd")
    latency = trace.get("latency_ms")
    has_cost = cost is not None
    has_latency = latency is not None
    score = (0.6 if has_cost else 0.0) + (0.4 if has_latency else 0.0)
    return {
        "key": "cost_present",
        "score": score,
        "passed": has_cost or has_latency,
        "comment": f"cost_usd={'set' if has_cost else 'missing'}, latency_ms={'set' if has_latency else 'missing'}",
        "evidence": {"cost_usd": float(cost) if has_cost else None, "latency_ms": latency},
    }


_ERROR_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeout", ("timeout", "timed out", "deadline")),
    ("auth", ("unauthorized", "401", "403", "forbidden", "invalid api key", "authentication")),
    ("rate_limit", ("rate limit", "429", "too many requests", "overloaded")),
    ("db", ("asyncpg", "psycopg", "relation ", "duplicate key", "deadlock", "connection pool")),
    ("network", ("connection reset", "connection refused", "dns", "unreachable", "ssl")),
    ("tool", ("tool", "mcp", "subprocess", "exit code")),
    ("validation", ("validation", "pydantic", "invalid", "schema")),
)


def classify_error(error: Optional[str]) -> str:
    text = (error or "").lower()
    if not text.strip():
        return "none"
    for label, needles in _ERROR_CLASSES:
        if any(needle in text for needle in needles):
            return label
    return "unclassified"


def _check_error_classification(trace: dict[str, Any]) -> dict[str, Any]:
    """A failure must be classifiable; an unclassifiable failure blocks learning."""
    error = trace.get("error")
    label = classify_error(error)
    status = str(trace.get("status") or "").lower()
    if label == "none":
        # No error text: consistent only when the trace did not fail.
        consistent = status != "error"
        return {
            "key": "error_classification",
            "score": 1.0 if consistent else 0.0,
            "passed": consistent,
            "comment": "no error recorded" if consistent else "status=error but no error text",
            "evidence": {"error_class": label, "status": status},
        }
    return {
        "key": "error_classification",
        "score": 0.0 if label == "unclassified" else 1.0,
        "passed": label != "unclassified",
        "comment": f"error classified as {label}",
        "evidence": {"error_class": label, "status": status},
    }


_RULES = (
    _check_source_presence,
    _check_tool_policy,
    _check_final_response,
    _check_cost_present,
    _check_error_classification,
)


def evaluate_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Score one trace against the five rule checks. Pure, no I/O, no LLM."""
    checks = [rule(trace or {}) for rule in _RULES]
    overall = sum(check["score"] for check in checks) / len(checks)
    return {
        "evaluator": EVALUATOR_NAME,
        "evaluator_version": EVALUATOR_VERSION,
        "trace_id": (trace or {}).get("trace_id"),
        "overall_score": round(overall, 4),
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


# ── dataset promotion ─────────────────────────────────────────────────────


async def promote_trace_to_dataset(
    *,
    trace_id: str,
    dataset_slug: Optional[str] = None,
    dataset_title: Optional[str] = None,
    project: Optional[str] = None,
    purpose: str = "",
    expected: str = "",
    rubric: Optional[dict[str, Any]] = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Promote a failed/low-quality trace into an eval dataset example (FR-002).

    Idempotent: promoting the same trace into the same dataset returns the
    existing example instead of duplicating it.
    """
    if not trace_id:
        raise DatasetPromotionError("trace_id is required")

    async with _PoolConn(conn) as target:
        for table in (DATASET_TABLE, EXAMPLE_TABLE):
            if not await relation_exists(target, table):
                raise DatasetPromotionError(
                    f"{table} is missing; apply migrations/163_ohvis_internal_llmops_v1.sql first"
                )

        trace = await get_trace(trace_id, conn=target)
        if trace is None:
            raise DatasetPromotionError(f"trace not found: {trace_id}")

        resolved_project = project or trace.get("project")
        slug = slugify(
            dataset_slug
            or f"{(resolved_project or 'aads').lower()}-failure-regression"
        )

        dataset_id = await target.fetchval(
            f"""
            INSERT INTO {DATASET_TABLE} (slug, project, title, purpose, source_filter, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
            ON CONFLICT (slug) DO UPDATE SET
                project = COALESCE({DATASET_TABLE}.project, EXCLUDED.project),
                title = COALESCE(NULLIF({DATASET_TABLE}.title, ''), EXCLUDED.title),
                purpose = COALESCE(NULLIF({DATASET_TABLE}.purpose, ''), EXCLUDED.purpose),
                enabled = TRUE,
                updated_at = NOW()
            RETURNING id
            """,
            slug,
            resolved_project,
            clip(dataset_title or f"{resolved_project or 'AADS'} failure regression", 200),
            clip(purpose or "Promoted from failed/low-quality OHVIS traces", 500),
            json.dumps({"source": "trace_promotion", "trace_id": trace_id}, ensure_ascii=False),
            json.dumps({"created_by": "llmops_eval.promote_trace_to_dataset"}, ensure_ascii=False),
        )

        evaluation = evaluate_trace(trace)
        example_metadata = {
            "graph_run_id": trace.get("graph_run_id"),
            "ledger": trace.get("ledger"),
            "status": trace.get("status"),
            "error_class": classify_error(trace.get("error")),
            "rule_overall_score": evaluation["overall_score"],
        }

        row = await target.fetchrow(
            f"""
            INSERT INTO {EXAMPLE_TABLE} (dataset_id, source_trace_id, input, expected, rubric, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
            ON CONFLICT (dataset_id, source_trace_id) WHERE source_trace_id IS NOT NULL
            DO UPDATE SET
                input = EXCLUDED.input,
                expected = COALESCE(NULLIF(EXCLUDED.expected, ''), {EXAMPLE_TABLE}.expected),
                metadata = {EXAMPLE_TABLE}.metadata || EXCLUDED.metadata
            RETURNING id, (xmax = 0) AS inserted
            """,
            dataset_id,
            trace_id,
            clip(trace.get("input_summary")),
            clip(expected),
            json.dumps(rubric or {"rules": [check["key"] for check in evaluation["checks"]]}, ensure_ascii=False),
            json.dumps(example_metadata, ensure_ascii=False, default=str),
        )

        return {
            "dataset_id": str(dataset_id),
            "dataset_slug": slug,
            "example_id": str(row["id"]),
            "created": bool(row["inserted"]),
            "trace_id": trace_id,
            "evaluation": evaluation,
        }


# ── experiments ───────────────────────────────────────────────────────────


async def run_experiment(
    *,
    dataset_slug: str,
    evaluator: str = EVALUATOR_NAME,
    candidate_sha: Optional[str] = None,
    model_id: Optional[str] = None,
    name: str = "",
    limit: int = 200,
    conn: Any = None,
) -> dict[str, Any]:
    """Run the rule evaluator over a dataset and persist one score row per check."""
    if evaluator != EVALUATOR_NAME:
        raise DatasetPromotionError(
            f"unsupported evaluator '{evaluator}'; only '{EVALUATOR_NAME}' is enabled "
            "(LLM-as-judge is opt-in and not wired yet)"
        )

    async with _PoolConn(conn) as target:
        for table in (DATASET_TABLE, EXAMPLE_TABLE, EXPERIMENT_TABLE, SCORE_TABLE):
            if not await relation_exists(target, table):
                raise DatasetPromotionError(
                    f"{table} is missing; apply migrations/163_ohvis_internal_llmops_v1.sql first"
                )

        dataset = await target.fetchrow(
            f"SELECT id, slug, project FROM {DATASET_TABLE} WHERE slug = $1", dataset_slug
        )
        if dataset is None:
            raise DatasetPromotionError(f"dataset not found: {dataset_slug}")

        examples = await target.fetch(
            f"""
            SELECT id, source_trace_id, input, expected, metadata
            FROM {EXAMPLE_TABLE} WHERE dataset_id = $1 ORDER BY created_at ASC LIMIT $2
            """,
            dataset["id"],
            max(1, min(int(limit), 500)),
        )

        experiment_id = await target.fetchval(
            f"""
            INSERT INTO {EXPERIMENT_TABLE}
                (dataset_id, name, evaluator, evaluator_version, candidate_sha, model_id, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'running')
            RETURNING id
            """,
            dataset["id"],
            clip(name or f"{dataset_slug} {evaluator}", 200),
            evaluator,
            EVALUATOR_VERSION,
            candidate_sha,
            model_id,
        )

        scored = 0
        passed = 0
        totals: dict[str, list[float]] = {}

        for example in examples:
            source_trace_id = example["source_trace_id"]
            trace = (
                await get_trace(source_trace_id, conn=target) if source_trace_id else None
            ) or {
                "trace_id": source_trace_id,
                "input_summary": example["input"],
                "output_summary": example["expected"],
                "status": "unknown",
            }
            evaluation = evaluate_trace(trace)
            scored += 1
            passed += 1 if evaluation["passed"] else 0

            rows = [
                (
                    experiment_id,
                    example["id"],
                    source_trace_id,
                    evaluator,
                    EVALUATOR_VERSION,
                    check["key"],
                    float(check["score"]),
                    bool(check["passed"]),
                    clip(check["comment"], 500),
                    json.dumps(check["evidence"], ensure_ascii=False, default=str),
                )
                for check in evaluation["checks"]
            ]
            rows.append(
                (
                    experiment_id,
                    example["id"],
                    source_trace_id,
                    evaluator,
                    EVALUATOR_VERSION,
                    "overall",
                    float(evaluation["overall_score"]),
                    bool(evaluation["passed"]),
                    "rule evaluator aggregate",
                    json.dumps({"checks": len(evaluation["checks"])}, ensure_ascii=False),
                )
            )
            for row in rows:
                totals.setdefault(row[5], []).append(row[6])
            await target.executemany(
                f"""
                INSERT INTO {SCORE_TABLE}
                    (experiment_id, example_id, trace_id, evaluator, evaluator_version,
                     key, score, passed, comment, evidence)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
                """,
                rows,
            )

        summary = {
            "examples": scored,
            "passed": passed,
            "pass_rate": round(passed / scored, 4) if scored else 0.0,
            "mean_by_key": {
                key: round(sum(values) / len(values), 4) for key, values in sorted(totals.items())
            },
        }
        await target.execute(
            f"UPDATE {EXPERIMENT_TABLE} SET status = $2, summary = $3::jsonb, completed_at = NOW() "
            "WHERE id = $1",
            experiment_id,
            "completed",
            json.dumps(summary, ensure_ascii=False),
        )

        return {
            "experiment_id": str(experiment_id),
            "dataset_slug": dataset["slug"],
            "evaluator": evaluator,
            "evaluator_version": EVALUATOR_VERSION,
            "candidate_sha": candidate_sha,
            "status": "completed",
            "summary": summary,
        }


async def get_experiment(experiment_id: str, *, conn: Any = None) -> Optional[dict[str, Any]]:
    """Experiment header plus its per-example scores."""
    async with _PoolConn(conn) as target:
        if not await relation_exists(target, EXPERIMENT_TABLE):
            return None
        header = await target.fetchrow(
            f"""
            SELECT e.id, e.name, e.evaluator, e.evaluator_version, e.candidate_sha,
                   e.model_id, e.status, e.summary, e.started_at, e.completed_at,
                   d.slug AS dataset_slug, d.project
            FROM {EXPERIMENT_TABLE} e
            JOIN {DATASET_TABLE} d ON d.id = e.dataset_id
            WHERE e.id = $1::uuid
            """,
            experiment_id,
        )
        if header is None:
            return None

        detail = dict(header)
        detail["id"] = str(detail["id"])
        detail["summary"] = _as_dict(detail.get("summary"))
        for key in ("started_at", "completed_at"):
            if detail.get(key) is not None:
                detail[key] = detail[key].isoformat()

        score_rows = await target.fetch(
            f"""
            SELECT example_id, trace_id, key, score, passed, comment, evidence
            FROM {SCORE_TABLE} WHERE experiment_id = $1::uuid
            ORDER BY created_at ASC, key ASC LIMIT 2000
            """,
            experiment_id,
        )
        detail["scores"] = [
            {
                **dict(row),
                "example_id": str(row["example_id"]) if row["example_id"] else None,
                "evidence": _as_dict(row["evidence"]),
            }
            for row in score_rows
        ]
        return detail
