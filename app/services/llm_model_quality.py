"""M7 모델별 품질·지연·성공률·비용 가시화 — AADS-LLM-M7-MODEL-QUALITY-VISIBILITY.

M9 동급 판정(후보 vs 현행 비교)의 선행 기준선이다. 새로 계산하지 않고 이미 있는
정본을 읽어 모델 / 공급자 / 실행경로 단위로 묶는다.

- 실패 분류: ``pipeline_failure_classification_v1`` 뷰(app.services.pipeline_failure_taxonomy).
  새 분류를 만들지 않는다.
- 비용: ``app.services.llm_cost_basis.fetch_cost_per_success`` 의 runner 표면 항목.
  비용을 다시 계산하지 않는다. cost_source 는 합치지 않는다.
- 모델 정규화: ``app.services.llm_metric_normalization.normalize_model_dimension``.
- 표본 부족: ``llm_cost_basis.MIN_SUCCESS_SAMPLE`` 와 ``insufficient_sample`` 규약.

**모델 귀속.** ``pipeline_jobs.actual_model`` 은 Claude CLI 경로에서 문자열
``unverified`` 로 적힌다(``PipelineRunnerJob._run_model_candidate``: 텍스트 출력
러너는 공급자 모델 영수증이 없어 검증된 실모델과 구분하려는 의도적 표지).
그 결과 실제로는 ``claude-sonnet-5-5`` 가 만든 결과가 모델 차원에서 ``unverified``
한 덩어리가 된다. 또 Codex 후보가 실패해 Claude 후보로 폴백하면 마지막에 쓴
쪽이 이겨서 Codex 를 요청한 작업도 ``unverified`` 가 된다.
읽는 쪽에서는 ``pipeline_runner_events`` 의 마지막 ``model_attempt_started.model``
(그 작업의 결과를 만든 마지막 시도)로 귀속한다. 이 값은 CLI 인자로 넘긴 모델이라
공급자 확인은 아니므로 ``model_attribution`` 에 출처를 함께 남긴다.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.llm_cost_basis import (
    MIN_SUCCESS_SAMPLE,
    SURFACE_RUNNER,
    fetch_cost_per_success,
)
from app.services.llm_metric_normalization import NON_MODEL_STATES, normalize_model_dimension
from app.services.pipeline_failure_taxonomy import (
    CLASS_INSTRUCTION,
    CLASS_UNCLASSIFIED,
    FAILURE_CLASSES,
    VIEW_NAME,
)

# 모델 귀속 출처
ATTRIBUTION_ACTUAL_MODEL = "actual_model"
ATTRIBUTION_LAST_ATTEMPT = "runner_events_last_attempt"
ATTRIBUTION_NO_ATTEMPT = "no_model_attempt"
ATTRIBUTION_SOURCES = (ATTRIBUTION_ACTUAL_MODEL, ATTRIBUTION_LAST_ATTEMPT, ATTRIBUTION_NO_ATTEMPT)

# 모델이 한 번도 실행되지 않은 작업(의존·중복 게이트 등)의 그룹 이름.
NO_ATTEMPT_LABEL = "(no_model_attempt)"

# 실행경로. 러너가 띄우는 CLI/프록시 종류다.
ROUTE_CODEX_CLI = "runner_codex_cli"
ROUTE_CLAUDE_CLI = "runner_claude_cli"
ROUTE_LITELLM = "runner_litellm"
ROUTE_UNKNOWN = "runner_unknown"

RATE_OK = "ok"
RATE_INSUFFICIENT = "insufficient_sample"
RATE_NO_DATA = "no_data"

TERMINAL_STATUSES = ("done", "error", "cancelled", "rejected_done")
SUCCESS_STATUS = "done"
FAILED_STATUSES = ("error", "cancelled")

DIMENSIONS = ("model", "provider", "route")

FIRST_OUTPUT_EVENTS = ("cli_first_stdout", "cli_first_stderr")

# 채팅 경로만 진짜 첫 토큰 지연을 잰다(turn_timing.TurnTimer.mark("first_token")).
CHAT_FIRST_TOKEN_SQL = """
    SELECT model, first_token_ms, total_ms
      FROM chat_turn_timing
     WHERE created_at >= NOW() - (INTERVAL '1 day' * $1::int)
"""

JOBS_SQL = f"""
    WITH win AS (
        SELECT j.job_id, j.status, j.project,
               NULLIF(j.actual_model, '') AS actual_model,
               NULLIF(j.model, '') AS requested_model
          FROM pipeline_jobs j
         WHERE j.created_at >= NOW() - (INTERVAL '1 day' * $1::int)
           AND j.status IN ('done', 'error', 'cancelled', 'rejected_done')
           AND ($2::text = '' OR j.project = $2::text)
    ), att AS (
        SELECT e.job_id,
               COUNT(*)::int AS attempts,
               (array_agg(e.model ORDER BY e.observed_at ASC, e.id ASC))[1] AS first_model,
               (array_agg(e.model ORDER BY e.observed_at DESC, e.id DESC))[1] AS last_model
          FROM pipeline_runner_events e
          JOIN win w ON w.job_id = e.job_id
         WHERE e.event_type = 'model_attempt_started'
         GROUP BY e.job_id
    )
    SELECT w.job_id, w.status, w.actual_model, w.requested_model,
           a.attempts, a.first_model, a.last_model,
           f.failure_class, f.failure_subtype
      FROM win w
      LEFT JOIN att a ON a.job_id = w.job_id
      LEFT JOIN {VIEW_NAME} f ON f.job_id = w.job_id
"""

FIRST_OUTPUT_SQL = """
    SELECT e.event_type, e.model, e.duration_ms
      FROM pipeline_runner_events e
      JOIN pipeline_jobs j ON j.job_id = e.job_id
     WHERE e.observed_at >= NOW() - (INTERVAL '1 day' * $1::int)
       AND e.event_type = ANY($3::text[])
       AND e.duration_ms IS NOT NULL AND e.duration_ms >= 0
       AND ($2::text = '' OR j.project = $2::text)
"""


def _is_model(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.lower() not in NON_MODEL_STATES


def attribute_model(row: Dict[str, Any]) -> Dict[str, Any]:
    """작업 한 건의 결과를 만든 모델을 정한다.

    1. actual_model 이 실모델이면 그것 (Codex/LiteLLM 경로가 적은 값).
    2. 아니면(unverified·NULL) 마지막 model_attempt_started 의 모델.
    3. 시도 기록이 없으면 모델은 한 번도 실행되지 않았다 — 요청 모델(requested_model)에
       책임을 지우지 않는다. 의존·중복 게이트로 죽은 작업이 모델 실패율을 올리는 것을 막는다.
    """
    actual = row.get("actual_model")
    last = row.get("last_model")
    if _is_model(actual):
        raw, source = str(actual), ATTRIBUTION_ACTUAL_MODEL
    elif _is_model(last):
        raw, source = str(last), ATTRIBUTION_LAST_ATTEMPT
    else:
        return {"raw": None, "model": None, "source": ATTRIBUTION_NO_ATTEMPT}
    return {"raw": raw, "model": normalize_model_dimension(raw)["model"], "source": source}


def route_of(raw_model: Optional[str]) -> str:
    text = str(raw_model or "").strip().lower()
    if not text:
        return ROUTE_UNKNOWN
    if text.startswith("codex:"):
        return ROUTE_CODEX_CLI
    if text.startswith("litellm"):
        return ROUTE_LITELLM
    if text.startswith(("claude", "anthropic")):
        return ROUTE_CLAUDE_CLI
    return ROUTE_UNKNOWN


def provider_of(raw_model: Optional[str]) -> Optional[str]:
    if not raw_model:
        return None
    from app.services.llm_account_usage import _classify_provider_from_model

    return _classify_provider_from_model(str(raw_model)) or None


def dimension_key(dimension: str, raw_model: Optional[str]) -> str:
    """raw 모델 문자열(접두사 포함)을 차원 값으로 바꾼다. 모델이 없으면 NO_ATTEMPT_LABEL."""
    if not raw_model:
        return NO_ATTEMPT_LABEL
    if dimension == "model":
        return normalize_model_dimension(raw_model)["model"] or NO_ATTEMPT_LABEL
    if dimension == "provider":
        return provider_of(raw_model) or "unknown"
    return route_of(raw_model)


def rate(numerator: int, denominator: int, *, min_sample: int = MIN_SUCCESS_SAMPLE) -> Dict[str, Any]:
    """비율. 표본이 min_sample 미만이면 값을 내지 않고 insufficient_sample 로 답한다."""
    n = int(denominator or 0)
    if n <= 0:
        return {"value_pct": None, "n": 0, "status": RATE_NO_DATA}
    if n < min_sample:
        return {"value_pct": None, "n": n, "status": RATE_INSUFFICIENT}
    return {"value_pct": round(numerator * 100.0 / n, 1), "n": n, "status": RATE_OK}


def _percentile(sorted_values: List[float], fraction: float) -> Optional[float]:
    if not sorted_values:
        return None
    position = fraction * (len(sorted_values) - 1)
    lo, hi = int(position), min(int(position) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (position - lo)


def latency_summary(values: Iterable[Any], *, min_sample: int = MIN_SUCCESS_SAMPLE) -> Dict[str, Any]:
    """P50/P95. 표본이 min_sample 미만이면 값은 NULL 이고 status=insufficient_sample."""
    nums = sorted(float(v) for v in values if v is not None)
    n = len(nums)
    if n == 0:
        return {"n": 0, "p50_ms": None, "p95_ms": None, "status": "미측정"}
    if n < min_sample:
        return {"n": n, "p50_ms": None, "p95_ms": None, "status": RATE_INSUFFICIENT}
    return {
        "n": n,
        "p50_ms": round(_percentile(nums, 0.50)),
        "p95_ms": round(_percentile(nums, 0.95)),
        "status": RATE_OK,
    }


def _new_bucket(key: str) -> Dict[str, Any]:
    return {
        "key": key,
        "terminal_jobs": 0,
        "by_status": Counter(),
        "failed_by_class": Counter(),
        "failed_by_subtype": Counter(),
        "attribution": Counter(),
        "first_attempt_jobs": 0,
        "fallback_out_jobs": 0,
        "same_model_retry_jobs": 0,
        "fallback_in_jobs": 0,
    }


def aggregate_jobs(
    rows: Iterable[Dict[str, Any]],
    dimension: str,
    *,
    min_sample: int = MIN_SUCCESS_SAMPLE,
) -> List[Dict[str, Any]]:
    """작업 행을 dimension(model|provider|route) 단위로 묶어 성공률·폴백률을 센다."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        attr = attribute_model(row)
        key = dimension_key(dimension, attr["raw"])
        b = buckets.setdefault(key, _new_bucket(key))
        status = str(row.get("status") or "")
        b["terminal_jobs"] += 1
        b["by_status"][status] += 1
        b["attribution"][attr["source"]] += 1
        if status in FAILED_STATUSES:
            cls = str(row.get("failure_class") or CLASS_UNCLASSIFIED)
            b["failed_by_class"][cls if cls in FAILURE_CLASSES else CLASS_UNCLASSIFIED] += 1
            b["failed_by_subtype"][f"{cls}/{row.get('failure_subtype') or 'unknown'}"] += 1

        attempts = int(row.get("attempts") or 0)
        first_raw, last_raw = row.get("first_model"), row.get("last_model")
        if attempts >= 1 and _is_model(first_raw):
            fb = buckets.setdefault(dimension_key(dimension, first_raw), _new_bucket(dimension_key(dimension, first_raw)))
            fb["first_attempt_jobs"] += 1
            if attempts > 1:
                if dimension_key(dimension, first_raw) != dimension_key(dimension, last_raw):
                    fb["fallback_out_jobs"] += 1
                    lb = buckets.setdefault(
                        dimension_key(dimension, last_raw), _new_bucket(dimension_key(dimension, last_raw))
                    )
                    lb["fallback_in_jobs"] += 1
                else:
                    fb["same_model_retry_jobs"] += 1

    out: List[Dict[str, Any]] = []
    for b in buckets.values():
        done = b["by_status"].get(SUCCESS_STATUS, 0)
        classes = {c: b["failed_by_class"].get(c, 0) for c in FAILURE_CLASSES}
        failed = sum(classes.values())
        # 모델 귀속 성공률: 인프라·게이트 실패는 모델 탓이 아니므로 분모에서 뺀다.
        # 미분류는 어느 쪽인지 모르므로 이 분모에도 넣지 않고 그대로 보여 준다.
        attributable_den = done + classes[CLASS_INSTRUCTION]
        out.append({
            "key": b["key"],
            "comparable": b["key"] != NO_ATTEMPT_LABEL,
            "terminal_jobs": b["terminal_jobs"],
            "by_status": dict(b["by_status"]),
            "success_rate": rate(done, b["terminal_jobs"], min_sample=min_sample),
            "model_attributable_success_rate": {
                **rate(done, attributable_den, min_sample=min_sample),
                "definition": "done / (done + instruction_defect). infra·gate_block·unclassified 는 분모에서 제외",
            },
            "failed_jobs": failed,
            "failed_by_class": classes,
            "failed_by_subtype": dict(b["failed_by_subtype"].most_common(8)),
            "fallback": {
                "first_attempt_jobs": b["first_attempt_jobs"],
                "fallback_out_jobs": b["fallback_out_jobs"],
                "fallback_out_rate": rate(
                    b["fallback_out_jobs"], b["first_attempt_jobs"], min_sample=min_sample
                ),
                "same_model_retry_jobs": b["same_model_retry_jobs"],
                "fallback_in_jobs": b["fallback_in_jobs"],
            },
            "model_attribution": {s: b["attribution"].get(s, 0) for s in ATTRIBUTION_SOURCES},
        })
    out.sort(key=lambda r: (-r["terminal_jobs"], r["key"]))
    return out


def attach_latency(
    items: List[Dict[str, Any]],
    dimension: str,
    events: Iterable[Dict[str, Any]],
    *,
    min_sample: int = MIN_SUCCESS_SAMPLE,
) -> None:
    """cli_first_stdout / cli_first_stderr 지연을 그룹에 붙인다.

    측정 대상은 CLI 프로세스 시작 → 첫 출력까지다. **첫 토큰 지연이 아니다.**
    러너는 텍스트 출력 모드라 stdout 이 최종 응답 무렵에야 나온다(2026-09-30 실측:
    claude-opus-5-5 P50 약 13분). stderr 는 CLI 기동 로그라 모델 응답 시각이 아니다.
    둘을 합치지 않고 따로 보여 준다. 첫 토큰 지연은 채팅 경로(chat_first_token)만 잰다.
    """
    samples: Dict[Tuple[str, str], List[Any]] = {}
    for ev in events:
        key = dimension_key(dimension, ev.get("model"))
        samples.setdefault((key, str(ev.get("event_type"))), []).append(ev.get("duration_ms"))
    for item in items:
        item["first_output_latency"] = {
            "first_stdout": latency_summary(samples.get((item["key"], "cli_first_stdout"), []), min_sample=min_sample),
            "first_stderr": latency_summary(samples.get((item["key"], "cli_first_stderr"), []), min_sample=min_sample),
            "basis": "프로세스 시작 → 첫 출력. 러너는 텍스트 출력 모드라 first_stdout 이 최종 응답 무렵이다 — 첫 토큰 지연은 미측정",
        }


def summarize_chat_first_token(
    rows: Iterable[Dict[str, Any]], *, min_sample: int = MIN_SUCCESS_SAMPLE
) -> List[Dict[str, Any]]:
    """채팅 턴의 첫 토큰 지연(chat_turn_timing.first_token_ms)을 정규화 모델별로 묶는다."""
    first: Dict[str, List[Any]] = {}
    total: Dict[str, List[Any]] = {}
    turns: Counter = Counter()
    for r in rows:
        dim = normalize_model_dimension(r.get("model"))
        key = dim["model"] or f"({dim['state']})"
        turns[key] += 1
        first.setdefault(key, []).append(r.get("first_token_ms"))
        total.setdefault(key, []).append(r.get("total_ms"))
    out = [
        {
            "key": key,
            "comparable": not key.startswith("("),
            "turns": turns[key],
            "first_token": latency_summary(first[key], min_sample=min_sample),
            "total": latency_summary(total[key], min_sample=min_sample),
        }
        for key in turns
    ]
    out.sort(key=lambda r: (-r["turns"], r["key"]))
    return out


def attach_cost(items: List[Dict[str, Any]], dimension: str, cost_items: Iterable[Dict[str, Any]]) -> None:
    """llm_cost_basis 의 runner 표면 항목을 그룹에 붙인다. 값을 다시 계산하지 않는다.

    비용은 cost_source 별로 따로 둔다(보고값과 추정값을 합치지 않는다). 러너 CLI 는
    구독 호출이라 이 값은 정가 환산이고 실청구는 어디에서도 측정하지 않는다.
    """
    by_key: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for it in cost_items:
        if it.get("surface") != SURFACE_RUNNER:
            continue
        slug = str(it.get("model") or "")
        # 사용량 로그의 모델은 접두사 없는 슬러그다(gpt-5.6-sol). 러너 표면의 gpt 계열은
        # Codex CLI 로만 돌므로 귀속 쪽 raw 표기(codex:…)로 되돌려 같은 차원 키를 얻는다.
        raw = f"codex:{slug}" if slug.lower().startswith(("gpt-", "o3")) else slug
        key = dimension_key(dimension, raw) if slug else "unknown"
        src = str(it.get("cost_source"))
        acc = by_key.setdefault(key, {}).setdefault(src, {
            "cost_source": src, "calls": 0, "total_cost_usd": None, "catalog_cost_usd": None,
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
        })
        acc["calls"] += int(it.get("samples") or 0)
        for fld in ("total_cost_usd", "catalog_cost_usd"):
            if it.get(fld) is not None:
                acc[fld] = round((acc[fld] or 0.0) + it[fld], 6)
        for fld in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            acc[fld] += int(it.get(fld) or 0)
    for item in items:
        sources = list(by_key.get(item["key"], {}).values())
        item["cost"] = {
            "by_cost_source": sources if sources else None,
            "status": "ok" if sources else "미측정",
            "basis": "정가 환산(구독 호출). 실청구 미측정",
            "billed_usd": None,
        }


def summarize_window(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """창 전체 실패 분류 분포와 모델 귀속 현황. 뷰의 분포가 재현되는지 대조하는 용도다."""
    failed = [r for r in rows if str(r.get("status")) in FAILED_STATUSES]
    classes = Counter(str(r.get("failure_class") or CLASS_UNCLASSIFIED) for r in failed)
    attribution = Counter(attribute_model(r)["source"] for r in rows)
    unverified_recorded = [r for r in rows if str(r.get("actual_model") or "").strip().lower() == "unverified"]
    recovered = [r for r in unverified_recorded if attribute_model(r)["source"] == ATTRIBUTION_LAST_ATTEMPT]
    return {
        "terminal_jobs": len(rows),
        "by_status": dict(Counter(str(r.get("status")) for r in rows)),
        "failed_jobs": len(failed),
        "failed_by_class": {c: classes.get(c, 0) for c in FAILURE_CLASSES},
        "model_attribution": {s: attribution.get(s, 0) for s in ATTRIBUTION_SOURCES},
        "unverified_actual_model": {
            "recorded_jobs": len(unverified_recorded),
            "recovered_by_runner_events": len(recovered),
            "still_unattributed": len(unverified_recorded) - len(recovered),
        },
    }


async def fetch_model_quality(conn: Any, days: int = 7, project: str = "") -> Dict[str, Any]:
    days = max(1, min(int(days), 90))
    project = (project or "").strip()
    rows = [dict(r) for r in await conn.fetch(JOBS_SQL, days, project)]
    events = [dict(r) for r in await conn.fetch(FIRST_OUTPUT_SQL, days, project, list(FIRST_OUTPUT_EVENTS))]
    chat_rows = [dict(r) for r in await conn.fetch(CHAT_FIRST_TOKEN_SQL, days)]
    cost_payload = await fetch_cost_per_success(conn, days)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for dim in DIMENSIONS:
        items = aggregate_jobs(rows, dim)
        attach_latency(items, dim, events)
        attach_cost(items, dim, cost_payload["items"])
        groups[f"by_{dim}"] = items

    return {
        "days": days,
        "project": project or None,
        "min_sample": MIN_SUCCESS_SAMPLE,
        "sources": {
            "jobs": "pipeline_jobs (status in done/error/cancelled/rejected_done, created_at 기준)",
            "failure_class": f"{VIEW_NAME} (error/cancelled 만 분류됨)",
            "model_attribution": "pipeline_jobs.actual_model → 없거나 unverified 면 pipeline_runner_events 마지막 model_attempt_started.model",
            "first_output_latency": "pipeline_runner_events cli_first_stdout / cli_first_stderr duration_ms (러너, 첫 토큰 아님)",
            "chat_first_token": "chat_turn_timing.first_token_ms (채팅 경로, 첫 토큰). project 필터는 적용되지 않는다",
            "cost": "llm_cost_basis.fetch_cost_per_success (oauth_usage_log, surface=runner)",
        },
        "summary": summarize_window(rows),
        **groups,
        "chat_first_token": summarize_chat_first_token(chat_rows),
        "notes": [
            "success_rate 는 done / 종료 작업(done+error+cancelled+rejected_done). rejected_done 은 성공이 아니며 "
            "실패 분류 뷰(error/cancelled)에도 없어 별도 상태로만 센다.",
            "model_attributable_success_rate 는 모델 귀책 후보만 분모에 둔다. instruction_defect 는 리뷰 "
            "REQUEST_CHANGES·빌드 실패 등으로 모델 출력 결함과 지시서 결함을 구분하지 못한다.",
            f"'{NO_ATTEMPT_LABEL}' 는 모델이 한 번도 실행되지 않은 작업(의존·중복 게이트 등)이다. 모델 비교에서 제외한다.",
            "model_attribution 의 runner_events_last_attempt 는 CLI 인자로 넘긴 모델이라 공급자 확인이 아니다.",
            "원천이 비어 있으면 값은 NULL 이고 status 가 '미측정' 또는 insufficient_sample 이다. 추정값은 넣지 않는다.",
            "사용량·한도(rate-limit 헤더)는 /api/v1/ops/llm-response-metrics 의 usage 절이 정본이다.",
        ],
    }
