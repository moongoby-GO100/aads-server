"""
Evaluation/Benchmark Pipeline for AADS Chat AI.

Measures and tracks response quality over time.
- Quality dashboard aggregation
- Regression detection
- Weekly report generation
- A/B metric tracking
- Tool grounding score calculation

No new DB migrations — uses existing quality_score, quality_details (chat_messages)
and ai_meta_memory for A/B metrics.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 1. Quality Dashboard Aggregator
# ---------------------------------------------------------------------------

async def aggregate_quality_stats(pool, days: int = 7) -> Dict[str, Any]:
    """
    Aggregate quality_score stats from chat_messages over the given window.

    Returns a structured dict with:
      - overall: avg, median, p10, p90, total_scored, trend
      - by_workspace: per-workspace breakdown
      - worst_5 / best_5: sample responses
      - violation_counts: from quality_details
    """
    async with pool.acquire() as conn:
        # ── Per-workspace score rows ──
        rows = await conn.fetch(
            """
            SELECT
                w.name AS workspace,
                m.quality_score,
                m.quality_details,
                m.id::text AS message_id,
                LEFT(m.content, 120) AS preview,
                m.created_at
            FROM chat_messages m
            JOIN chat_sessions s ON s.id = m.session_id
            JOIN chat_workspaces w ON w.id = s.workspace_id
            WHERE m.role = 'assistant'
              AND m.quality_score IS NOT NULL
              AND m.created_at >= NOW() - ($1 || ' days')::interval
            ORDER BY m.created_at DESC
            """,
            str(days),
        )

        # ── Previous window for trend ──
        prev_rows = await conn.fetch(
            """
            SELECT m.quality_score
            FROM chat_messages m
            WHERE m.role = 'assistant'
              AND m.quality_score IS NOT NULL
              AND m.created_at >= NOW() - ($1 || ' days')::interval
              AND m.created_at <  NOW() - ($2 || ' days')::interval
            """,
            str(days * 2),
            str(days),
        )

    if not rows:
        return {
            "overall": {
                "avg": None, "median": None, "p10": None, "p90": None,
                "total_scored": 0, "trend": "no_data",
            },
            "by_workspace": {},
            "worst_5": [],
            "best_5": [],
            "violation_counts": {},
        }

    # ── Compute overall stats ──
    scores = [float(r["quality_score"]) for r in rows]
    scores_sorted = sorted(scores)
    n = len(scores_sorted)

    overall_avg = statistics.mean(scores)
    overall_median = statistics.median(scores)
    p10 = scores_sorted[max(0, int(n * 0.1))]
    p90 = scores_sorted[min(n - 1, int(n * 0.9))]

    # Trend
    prev_scores = [float(r["quality_score"]) for r in prev_rows]
    if prev_scores:
        prev_avg = statistics.mean(prev_scores)
        if overall_avg > prev_avg + 0.03:
            trend = "improving"
        elif overall_avg < prev_avg - 0.03:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = "no_prior_data"

    # ── By workspace ──
    ws_map: Dict[str, List[float]] = {}
    for r in rows:
        ws = r["workspace"] or "unknown"
        ws_map.setdefault(ws, []).append(float(r["quality_score"]))

    by_workspace = {}
    for ws, ws_scores in ws_map.items():
        by_workspace[ws] = {
            "avg": round(statistics.mean(ws_scores), 3),
            "median": round(statistics.median(ws_scores), 3),
            "count": len(ws_scores),
        }

    # ── Worst 5 / Best 5 ──
    sorted_rows = sorted(rows, key=lambda r: float(r["quality_score"]))
    worst_5 = [
        {
            "message_id": r["message_id"],
            "score": round(float(r["quality_score"]), 3),
            "preview": r["preview"],
            "workspace": r["workspace"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in sorted_rows[:5]
    ]
    best_5 = [
        {
            "message_id": r["message_id"],
            "score": round(float(r["quality_score"]), 3),
            "preview": r["preview"],
            "workspace": r["workspace"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in sorted_rows[-5:]
    ]
    best_5.reverse()

    # ── Violation counts from quality_details ──
    violation_counts: Dict[str, int] = {}
    for r in rows:
        details = r["quality_details"]
        if not details:
            continue
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (json.JSONDecodeError, TypeError):
                continue
        # quality_details may have a "violations" list or a "violation_type" field
        violations = details.get("violations", [])
        if isinstance(violations, list):
            for v in violations:
                vtype = v if isinstance(v, str) else str(v)
                violation_counts[vtype] = violation_counts.get(vtype, 0) + 1
        vtype_single = details.get("violation_type")
        if vtype_single:
            violation_counts[vtype_single] = violation_counts.get(vtype_single, 0) + 1
        # Also count sub-scores below threshold as implicit violations
        for dim in ("accuracy", "completeness", "relevance", "tool_grounding", "actionability"):
            val = details.get(dim)
            if val is not None and float(val) < 0.3:
                key = f"low_{dim}"
                violation_counts[key] = violation_counts.get(key, 0) + 1

    logger.info(
        "quality_stats_aggregated",
        days=days,
        total=n,
        avg=round(overall_avg, 3),
        trend=trend,
    )

    return {
        "overall": {
            "avg": round(overall_avg, 3),
            "median": round(overall_median, 3),
            "p10": round(p10, 3),
            "p90": round(p90, 3),
            "total_scored": n,
            "trend": trend,
        },
        "by_workspace": by_workspace,
        "worst_5": worst_5,
        "best_5": best_5,
        "violation_counts": violation_counts,
    }


# ---------------------------------------------------------------------------
# 2. Regression Detector
# ---------------------------------------------------------------------------

async def detect_quality_regression(
    pool, window_days: int = 3
) -> List[Dict[str, Any]]:
    """
    Compare recent `window_days` avg quality vs the prior `window_days`.
    If drop > 15%, flag as regression.

    Returns list of regressions per workspace (and overall).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                w.name AS workspace,
                m.quality_score,
                CASE
                    WHEN m.created_at >= NOW() - ($1 || ' days')::interval THEN 'recent'
                    ELSE 'previous'
                END AS period
            FROM chat_messages m
            JOIN chat_sessions s ON s.id = m.session_id
            JOIN chat_workspaces w ON w.id = s.workspace_id
            WHERE m.role = 'assistant'
              AND m.quality_score IS NOT NULL
              AND m.created_at >= NOW() - ($2 || ' days')::interval
            """,
            str(window_days),
            str(window_days * 2),
        )

        # Fetch sample bad responses for context
        bad_samples = await conn.fetch(
            """
            SELECT
                w.name AS workspace,
                m.id::text AS message_id,
                m.quality_score,
                LEFT(m.content, 200) AS preview
            FROM chat_messages m
            JOIN chat_sessions s ON s.id = m.session_id
            JOIN chat_workspaces w ON w.id = s.workspace_id
            WHERE m.role = 'assistant'
              AND m.quality_score IS NOT NULL
              AND m.quality_score < 0.5
              AND m.created_at >= NOW() - ($1 || ' days')::interval
            ORDER BY m.quality_score ASC
            LIMIT 20
            """,
            str(window_days),
        )

    # Group by workspace + period
    groups: Dict[str, Dict[str, List[float]]] = {}
    # Include an "ALL" aggregate
    groups["__ALL__"] = {"recent": [], "previous": []}
    for r in rows:
        ws = r["workspace"] or "unknown"
        groups.setdefault(ws, {"recent": [], "previous": []})
        score = float(r["quality_score"])
        groups[ws][r["period"]].append(score)
        groups["__ALL__"][r["period"]].append(score)

    # Index bad samples by workspace
    bad_by_ws: Dict[str, List[Dict]] = {}
    for b in bad_samples:
        ws = b["workspace"] or "unknown"
        bad_by_ws.setdefault(ws, []).append({
            "message_id": b["message_id"],
            "score": round(float(b["quality_score"]), 3),
            "preview": b["preview"],
        })

    regressions: List[Dict[str, Any]] = []
    for ws, periods in groups.items():
        recent = periods["recent"]
        previous = periods["previous"]
        if len(recent) < 3 or len(previous) < 3:
            continue  # not enough data

        recent_avg = statistics.mean(recent)
        prev_avg = statistics.mean(previous)
        if prev_avg == 0:
            continue

        drop_pct = (prev_avg - recent_avg) / prev_avg * 100
        if drop_pct > 15:
            label = "Overall" if ws == "__ALL__" else ws
            regressions.append({
                "workspace": label,
                "old_avg": round(prev_avg, 3),
                "new_avg": round(recent_avg, 3),
                "drop_pct": round(drop_pct, 1),
                "recent_count": len(recent),
                "previous_count": len(previous),
                "sample_bad_responses": bad_by_ws.get(
                    ws if ws != "__ALL__" else "", []
                )[:5],
            })

    if regressions:
        logger.warning(
            "quality_regression_detected",
            count=len(regressions),
            workspaces=[r["workspace"] for r in regressions],
        )
    else:
        logger.info("quality_regression_none", window_days=window_days)

    return regressions


# ---------------------------------------------------------------------------
# 3. Weekly Quality Report Generator
# ---------------------------------------------------------------------------

async def generate_weekly_report(pool) -> str:
    """
    Generate a markdown quality report for the last 7 days.
    Designed to be called by Sleep-Time Agent (C1) weekly.
    """
    stats = await aggregate_quality_stats(pool, days=7)
    regressions = await detect_quality_regression(pool, window_days=3)
    overall = stats["overall"]

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: List[str] = []
    lines.append(f"# AADS AI Quality Report - Weekly")
    lines.append(f"Generated: {now_str}\n")

    # ── Overall Score ──
    lines.append("## Overall Score")
    if overall["total_scored"] == 0:
        lines.append("No scored responses in the past 7 days.\n")
    else:
        trend_emoji = {
            "improving": "UP", "declining": "DOWN", "stable": "STABLE",
            "no_prior_data": "N/A",
        }.get(overall["trend"], "?")
        lines.append(f"- **Average**: {overall['avg']}")
        lines.append(f"- **Median**: {overall['median']}")
        lines.append(f"- **P10 / P90**: {overall['p10']} / {overall['p90']}")
        lines.append(f"- **Total scored**: {overall['total_scored']}")
        lines.append(f"- **Trend**: {trend_emoji} ({overall['trend']})\n")

    # ── By Workspace ──
    lines.append("## By Workspace")
    if stats["by_workspace"]:
        lines.append("| Workspace | Avg | Median | Count |")
        lines.append("|-----------|-----|--------|-------|")
        for ws, ws_stats in sorted(
            stats["by_workspace"].items(), key=lambda x: x[1]["avg"]
        ):
            lines.append(
                f"| {ws} | {ws_stats['avg']} | {ws_stats['median']} | {ws_stats['count']} |"
            )
        lines.append("")
    else:
        lines.append("No workspace data.\n")

    # ── Top Issues (Violations) ──
    lines.append("## Top Issues")
    vc = stats["violation_counts"]
    if vc:
        sorted_vc = sorted(vc.items(), key=lambda x: x[1], reverse=True)
        for vtype, count in sorted_vc[:10]:
            lines.append(f"- **{vtype}**: {count} occurrences")
        lines.append("")
    else:
        lines.append("No violations detected.\n")

    # ── Regressions ──
    if regressions:
        lines.append("## Regressions Detected")
        for reg in regressions:
            lines.append(
                f"- **{reg['workspace']}**: {reg['old_avg']} -> {reg['new_avg']} "
                f"(drop {reg['drop_pct']}%)"
            )
        lines.append("")

    # ── Worst Responses ──
    lines.append("## Worst 5 Responses")
    for item in stats["worst_5"]:
        lines.append(
            f"- [{item['score']}] {item['workspace']}: "
            f"{item['preview'][:80]}..."
        )
    lines.append("")

    # ── Improvement Recommendations ──
    lines.append("## Improvement Recommendations")
    recommendations = _generate_recommendations(stats, regressions)
    for rec in recommendations:
        lines.append(f"- {rec}")
    lines.append("")

    report = "\n".join(lines)
    logger.info("weekly_report_generated", length=len(report))
    return report


def _generate_recommendations(
    stats: Dict[str, Any], regressions: List[Dict[str, Any]]
) -> List[str]:
    """Heuristic recommendations based on stats."""
    recs: List[str] = []
    overall = stats["overall"]

    if overall["total_scored"] == 0:
        recs.append("Enable self-evaluation (SELF_EVAL_ENABLED=true) to start tracking quality.")
        return recs

    avg = overall["avg"]
    if avg is not None:
        if avg < 0.5:
            recs.append(
                "Overall quality is critically low. Review system prompts and tool availability."
            )
        elif avg < 0.7:
            recs.append(
                "Quality is below target (0.7). Focus on improving tool_grounding and accuracy."
            )

    vc = stats["violation_counts"]
    if vc.get("low_tool_grounding", 0) > 5:
        recs.append(
            "High count of low tool_grounding scores. Ensure AI is using tools before making claims."
        )
    if vc.get("low_accuracy", 0) > 3:
        recs.append(
            "Accuracy issues detected. Check for hallucination patterns and strengthen R-CRITICAL rules."
        )
    if vc.get("FABRICATED_RESULTS", 0) > 0:
        recs.append(
            f"FABRICATED_RESULTS violations: {vc['FABRICATED_RESULTS']}. "
            "Output validator is catching fabricated data — review and reinforce prompts."
        )
    if vc.get("EMPTY_PROMISE", 0) > 0:
        recs.append(
            "EMPTY_PROMISE violations detected. AI is promising actions without executing tools."
        )

    if regressions:
        ws_names = [r["workspace"] for r in regressions]
        recs.append(
            f"Quality regression in: {', '.join(ws_names)}. "
            "Investigate recent prompt/model changes."
        )

    # Workspace-specific
    for ws, ws_stats in stats.get("by_workspace", {}).items():
        if ws_stats["avg"] < 0.4:
            recs.append(
                f"Workspace '{ws}' has critically low avg ({ws_stats['avg']}). "
                "Review workspace system prompt."
            )

    if not recs:
        recs.append("Quality is on track. Continue monitoring.")

    return recs


# ---------------------------------------------------------------------------
# 4. A/B Metric Tracker
# ---------------------------------------------------------------------------

async def record_ab_metric(
    pool,
    variant: str,
    metric_name: str,
    value: float,
    metadata: Optional[dict] = None,
) -> None:
    """
    Record an A/B test metric into ai_meta_memory.

    key format: "ab:{variant}:{metric_name}"
    value: JSONB with history of recorded values.
    """
    key = f"ab:{variant}:{metric_name}"
    meta_json = json.dumps(metadata) if metadata else "{}"

    async with pool.acquire() as conn:
        # Upsert: append to values array in JSONB
        existing = await conn.fetchval(
            "SELECT value FROM ai_meta_memory WHERE key = $1",
            key,
        )

        entry = {
            "value": value,
            "ts": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
        }

        if existing:
            if isinstance(existing, str):
                try:
                    data = json.loads(existing)
                except (json.JSONDecodeError, TypeError):
                    data = {"entries": []}
            else:
                data = existing if isinstance(existing, dict) else {"entries": []}

            entries = data.get("entries", [])
            entries.append(entry)
            # Keep last 500 entries
            if len(entries) > 500:
                entries = entries[-500:]
            data["entries"] = entries
            data["latest"] = value
            data["count"] = len(entries)

            await conn.execute(
                """
                UPDATE ai_meta_memory
                SET value = $1::jsonb, confidence = 1.0, updated_at = NOW()
                WHERE key = $2
                """,
                json.dumps(data),
                key,
            )
        else:
            data = {
                "entries": [entry],
                "latest": value,
                "count": 1,
                "variant": variant,
                "metric": metric_name,
            }
            await conn.execute(
                """
                INSERT INTO ai_meta_memory (category, key, value, confidence, updated_at)
                VALUES ('ab_metric', $1, $2::jsonb, 1.0, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW()
                """,
                key,
                json.dumps(data),
            )

    logger.debug(
        "ab_metric_recorded",
        variant=variant,
        metric=metric_name,
        value=value,
    )


async def get_ab_summary(pool, variant: str) -> Dict[str, Any]:
    """
    Retrieve summary of all metrics for a given A/B variant.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, value FROM ai_meta_memory
            WHERE category = 'ab_metric' AND key LIKE $1
            ORDER BY updated_at DESC
            """,
            f"ab:{variant}:%",
        )

    summary: Dict[str, Any] = {"variant": variant, "metrics": {}}
    for r in rows:
        val = r["value"]
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(val, dict):
            continue

        metric_name = r["key"].split(":", 2)[-1] if ":" in r["key"] else r["key"]
        entries = val.get("entries", [])
        values = [e["value"] for e in entries if isinstance(e.get("value"), (int, float))]
        summary["metrics"][metric_name] = {
            "count": len(values),
            "latest": val.get("latest"),
            "avg": round(statistics.mean(values), 4) if values else None,
            "min": round(min(values), 4) if values else None,
            "max": round(max(values), 4) if values else None,
        }

    return summary


# ---------------------------------------------------------------------------
# 5. Tool Grounding Score Calculator
# ---------------------------------------------------------------------------

def calculate_grounding_score(
    response_text: str,
    tools_called: List[str],
    tool_results: List[str],
) -> float:
    """
    Score 0.0-1.0 for how well the response is grounded in tool results.
    Pure heuristic — no LLM call, designed for speed.

    Checks:
    1. Does the response reference specific data from tool outputs?
    2. Are numeric claims traceable to tool results?
    3. Did the response use tools at all?
    """
    if not response_text or not response_text.strip():
        return 0.0

    response_lower = response_text.lower()
    response_clean = re.sub(r'\s+', ' ', response_lower)

    # ── Component 1: Tool usage (0.0-0.3) ──
    if not tools_called:
        tool_usage_score = 0.0
    elif len(tools_called) == 1:
        tool_usage_score = 0.2
    else:
        tool_usage_score = 0.3

    # ── Component 2: Data reference overlap (0.0-0.4) ──
    # Extract meaningful tokens from tool results
    result_tokens = set()
    result_numbers = set()
    for result in tool_results:
        if not result:
            continue
        # Extract numbers (potential data points)
        numbers = re.findall(r'\b\d+(?:\.\d+)?\b', result)
        for num in numbers:
            if len(num) >= 2:  # skip single digits
                result_numbers.add(num)
        # Extract multi-char words that are likely data
        words = re.findall(r'[a-zA-Z가-힣]{3,}', result.lower())
        result_tokens.update(words)

    if not result_tokens and not result_numbers:
        data_overlap_score = 0.0  # no meaningful data tokens to compare
    else:
        # Check how many tool-result tokens appear in the response
        token_hits = sum(1 for t in result_tokens if t in response_clean)
        number_hits = sum(1 for n in result_numbers if n in response_text)

        token_ratio = token_hits / max(len(result_tokens), 1)
        number_ratio = number_hits / max(len(result_numbers), 1)

        data_overlap_score = min(0.4, (token_ratio * 0.2) + (number_ratio * 0.2))

    # ── Component 3: Claim traceability (0.0-0.3) ──
    # Check if numbers/specific data in the response can be traced back to tool results
    response_numbers = set(re.findall(r'\b\d{2,}(?:\.\d+)?\b', response_text))

    if not response_numbers:
        # No numeric claims — neutral score
        traceability_score = 0.15
    else:
        traceable = sum(1 for n in response_numbers if n in str(tool_results))
        untraceable = len(response_numbers) - traceable
        if len(response_numbers) > 0:
            trace_ratio = traceable / len(response_numbers)
        else:
            trace_ratio = 1.0

        # Penalize untraceable claims
        traceability_score = max(0.0, min(0.3, trace_ratio * 0.3 - untraceable * 0.02))

    total = tool_usage_score + data_overlap_score + traceability_score
    return round(min(1.0, max(0.0, total)), 3)
