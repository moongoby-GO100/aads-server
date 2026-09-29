"""M9 비용 계측 정본 — AADS-LLM-M9-COST-BASIS-20260930.

정본은 `oauth_usage_log` 하나다. `task_cost_log`(2026-03-05 정지)·`cost_tracking`
(2026-03-11 정지)은 기록하는 코드가 더 이상 없어 폐기했다. 새 기록 경로를 거기로
되살리지 마라 — 두 원장을 합산하면 같은 호출이 두 번 잡힌다.

이 모듈이 정하는 세 가지.

1. **비용 출처(cost_source)** — `cost_usd` 가 누가 낸 값인가.
   relay_reported(CLI/릴레이 자체 보고) · catalog_estimated(앱 단가표 추정) ·
   unknown(미측정, cost_usd=NULL). 정가 재계산값은 `cost_usd_catalog` 에 **따로**
   적고, 그 값은 DB 함수 `llm_catalog_cost_usd()` 가 기록 시점 `llm_models` 로
   계산한다. 두 값을 한 컬럼에 섞지 않는 것이 불일치를 드러내는 유일한 방법이다.
2. **표면(surface)** — chat / runner / terminal_cli / service / unknown.
   `classify_surface()` 하나로만 판정한다. 추측하지 않고 식별자로 판정한다.
3. **성공 작업당 비용** — 분모가 정의된 표면(runner)만 계산한다. 분모를 정의할 수
   없는 표면은 NULL 과 `denominator_undefined` 로 답한다. 임의의 분모를 만들지 않는다.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

COST_SOURCE_RELAY_REPORTED = "relay_reported"
COST_SOURCE_CATALOG_ESTIMATED = "catalog_estimated"
COST_SOURCE_UNKNOWN = "unknown"
COST_SOURCES: Tuple[str, ...] = (
    COST_SOURCE_RELAY_REPORTED,
    COST_SOURCE_CATALOG_ESTIMATED,
    COST_SOURCE_UNKNOWN,
)

SURFACE_CHAT = "chat"
SURFACE_RUNNER = "runner"
SURFACE_TERMINAL_CLI = "terminal_cli"
SURFACE_SERVICE = "service"
SURFACE_UNKNOWN = "unknown"
SURFACES: Tuple[str, ...] = (
    SURFACE_CHAT,
    SURFACE_RUNNER,
    SURFACE_TERMINAL_CLI,
    SURFACE_SERVICE,
    SURFACE_UNKNOWN,
)

# 러너가 CLI 를 직접 띄워 남기는 call_source (scripts/runner_cli_usage.py).
RUNNER_CALL_SOURCES = frozenset({"runner_claude_cli", "runner_codex_cli"})
# 앱 안의 CLI 릴레이 경로 (model_selector._stream_cli_relay / _stream_codex_relay).
CLI_RELAY_CALL_SOURCES = frozenset({"cli_relay", "codex_relay"})
# 앱 내부 서비스 호출.
SERVICE_CALL_SOURCES = frozenset({
    "anthropic_client",
    "anthropic_client_msg",
    "model_selector_sdk",
    "ceo_chat",
    "ceo_chat_tools",
})

# 성공 작업당 비용을 낼 최소 성공 작업 수.
MIN_SUCCESS_SAMPLE = 5

# 러너 분모: 사람이 승인해 끝난 작업만 성공이다. rejected_done 은 제외한다.
RUNNER_SUCCESS_STATUS = "done"

PER_SUCCESS_OK = "ok"
PER_SUCCESS_INSUFFICIENT = "insufficient_sample"
PER_SUCCESS_DENOMINATOR_ZERO = "denominator_zero"
PER_SUCCESS_DENOMINATOR_UNDEFINED = "denominator_undefined"
# 분모는 충분한데 비용이 미측정이다. cost_source 의 'unknown' 과는 다른 이름공간이다 —
# 상태값에 비용출처 상수를 빌려 쓰지 않는다(rework 2, 리뷰 지적 6).
PER_SUCCESS_COST_UNKNOWN = "cost_unknown"
PER_SUCCESS_STATUSES: Tuple[str, ...] = (
    PER_SUCCESS_OK,
    PER_SUCCESS_INSUFFICIENT,
    PER_SUCCESS_DENOMINATOR_ZERO,
    PER_SUCCESS_DENOMINATOR_UNDEFINED,
    PER_SUCCESS_COST_UNKNOWN,
)

# 분모가 정의된 표면. 채팅은 "성공한 대화" 를 식별할 수 있는 기록이 없다.
_DENOMINATOR_DEFINED_SURFACES = frozenset({SURFACE_RUNNER})


def normalize_cost_source(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text if text in COST_SOURCES else None


def resolve_cost_fields(
    cost_usd: Any,
    cost_source: Any = None,
) -> Tuple[Optional[float], str]:
    """(cost_usd, cost_source) 를 기록 가능한 쌍으로 정한다.

    - 출처를 밝히지 않은 비용은 unknown 이다. 출처 없는 숫자는 정본이 아니다.
    - unknown 인데 값이 0/None 이면 NULL 로 적는다 — 0 달러가 아니라 미측정이다.
    - 보고/추정이라고 했는데 값이 없으면 unknown 으로 내린다.
    - 이 함수는 정가 재계산값(cost_usd_catalog)을 절대 만들지 않는다. 그 값은
      DB 함수 llm_catalog_cost_usd() 만 만든다.
    """
    source = normalize_cost_source(cost_source) or COST_SOURCE_UNKNOWN
    value: Optional[float]
    try:
        value = None if cost_usd is None else float(cost_usd)
    except (TypeError, ValueError):
        value = None

    if source == COST_SOURCE_UNKNOWN:
        if value is None or value == 0:
            return None, COST_SOURCE_UNKNOWN
        return value, COST_SOURCE_UNKNOWN
    if value is None:
        return None, COST_SOURCE_UNKNOWN
    return value, source


def classify_surface(
    call_source: Any,
    job_id: Any = None,
    session_is_chat: bool = False,
) -> str:
    """사용량 한 건이 어느 표면에서 났는지 판정한다 — 이 함수가 유일한 판정기다.

    순서가 곧 규칙이다.
    1. job_id 가 있거나 러너 call_source 면 runner. 러너 행은 채팅이 띄운 작업이어도
       러너가 태운 토큰이다(러너 행의 session_id 는 CLI 세션이라 채팅 id 와 겹치지
       않지만, 겹치더라도 실행 컨텍스트가 우선이다).
    2. session_id 가 chat_sessions.id 에 있으면 chat.
    3. 그 외 CLI 릴레이(cli_relay/codex_relay)는 terminal_cli.
    4. 앱 내부 호출(anthropic_client/model_selector_sdk/ceo_chat…)은 service.
    5. 나머지는 unknown. 짐작해서 어디에 붙이지 않는다.
    """
    source = str(call_source or "").strip()
    if str(job_id or "").strip() or source in RUNNER_CALL_SOURCES:
        return SURFACE_RUNNER
    if session_is_chat:
        return SURFACE_CHAT
    if source in CLI_RELAY_CALL_SOURCES:
        return SURFACE_TERMINAL_CLI
    if source in SERVICE_CALL_SOURCES:
        return SURFACE_SERVICE
    return SURFACE_UNKNOWN


def cost_per_success(
    total_cost_usd: Optional[float],
    success_jobs: Optional[int],
    *,
    denominator_defined: bool,
    min_sample: int = MIN_SUCCESS_SAMPLE,
) -> Dict[str, Any]:
    """성공 작업당 비용. 값을 낼 수 없으면 None 과 이유를 돌려준다."""
    if not denominator_defined:
        return {"value": None, "status": PER_SUCCESS_DENOMINATOR_UNDEFINED}
    n = int(success_jobs or 0)
    if n <= 0:
        return {"value": None, "status": PER_SUCCESS_DENOMINATOR_ZERO}
    if n < min_sample:
        return {"value": None, "status": PER_SUCCESS_INSUFFICIENT}
    if total_cost_usd is None:
        # 성공 작업은 충분하지만 비용이 미측정이다. 0 으로 나누지도, 0 을 내지도 않는다.
        return {"value": None, "status": PER_SUCCESS_COST_UNKNOWN}
    return {"value": round(float(total_cost_usd) / n, 6), "status": PER_SUCCESS_OK}


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_usage_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    min_sample: int = MIN_SUCCESS_SAMPLE,
) -> List[Dict[str, Any]]:
    """집계 전 행(작업 단위까지 쪼갠 것)을 표면·모델·비용출처 단위로 합친다.

    입력 행 키: call_source, model, cost_source, job_id, session_is_chat, job_status,
    calls, cost_usd, cost_usd_catalog, catalog_calls, both_cost_usd, both_catalog_usd,
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens.

    cost_source 는 그룹 키다 — 보고값과 추정값을 한 합계로 섞지 않는다.
    """
    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in rows:
        surface = classify_surface(
            row.get("call_source"), row.get("job_id"), bool(row.get("session_is_chat"))
        )
        model = str(row.get("model") or "")
        source = normalize_cost_source(row.get("cost_source")) or COST_SOURCE_UNKNOWN
        key = (surface, model, source)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "surface": surface,
                "model": model,
                "cost_source": source,
                "calls": 0,
                "total_cost_usd": None,
                "catalog_cost_usd": None,
                "catalog_calls": 0,
                "_both_reported": 0.0,
                "_both_catalog": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "_jobs": set(),
                "_success_jobs": set(),
            }
        g["calls"] += int(row.get("calls") or 0)
        cost = _num(row.get("cost_usd"))
        if cost is not None:
            g["total_cost_usd"] = (g["total_cost_usd"] or 0.0) + cost
        catalog = _num(row.get("cost_usd_catalog"))
        if catalog is not None:
            g["catalog_cost_usd"] = (g["catalog_cost_usd"] or 0.0) + catalog
        g["catalog_calls"] += int(row.get("catalog_calls") or 0)
        g["_both_reported"] += _num(row.get("both_cost_usd")) or 0.0
        g["_both_catalog"] += _num(row.get("both_catalog_usd")) or 0.0
        for tok in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            g[tok] += int(row.get(tok) or 0)
        job_id = str(row.get("job_id") or "").strip()
        if job_id:
            g["_jobs"].add(job_id)
            if str(row.get("job_status") or "") == RUNNER_SUCCESS_STATUS:
                g["_success_jobs"].add(job_id)

    # 분모는 표면 × 비용출처 단위에서 job_id 로 중복을 없앤 성공 작업 수다
    # (rework 1, 리뷰 지적 8). 한 작업이 여러 모델을 쓰면 모델마다 분모에 한 번씩
    # 들어가던 것을 막는다. 모델 행의 cost_per_success_usd 는 그 공통 분모로 나눈
    # "작업당 비용 중 이 모델 몫" 이다 — 모델 행을 더하면 표면 행과 같다.
    surface_jobs: Dict[Tuple[str, str], set] = {}
    surface_success: Dict[Tuple[str, str], set] = {}
    for g in groups.values():
        skey = (g["surface"], g["cost_source"])
        surface_jobs.setdefault(skey, set()).update(g["_jobs"])
        surface_success.setdefault(skey, set()).update(g["_success_jobs"])

    out: List[Dict[str, Any]] = []
    for g in groups.values():
        defined = g["surface"] in _DENOMINATOR_DEFINED_SURFACES
        skey = (g["surface"], g["cost_source"])
        success = len(surface_success[skey]) if defined else None
        per = cost_per_success(
            g["total_cost_usd"], success, denominator_defined=defined, min_sample=min_sample
        )
        per_catalog = cost_per_success(
            g["catalog_cost_usd"], success, denominator_defined=defined, min_sample=min_sample
        )
        ratio = None
        if g["_both_catalog"] > 0:
            ratio = round(g["_both_reported"] / g["_both_catalog"], 4)
        out.append({
            "surface": g["surface"],
            "model": g["model"],
            "cost_source": g["cost_source"],
            "samples": g["calls"],
            "jobs": len(surface_jobs[skey]) if defined else None,
            "success_jobs": success,
            "success_jobs_using_model": len(g["_success_jobs"]) if defined else None,
            "denominator": (
                "pipeline_jobs.status='done' (rejected_done 제외), 표면×비용출처 단위 job_id 중복 제거"
                if defined else "undefined"
            ),
            "total_cost_usd": None if g["total_cost_usd"] is None else round(g["total_cost_usd"], 6),
            "cost_per_success_usd": per["value"],
            "cost_per_success_status": per["status"],
            "cost_per_success_scope": "model_share_of_surface" if defined else None,
            "catalog_cost_usd": None if g["catalog_cost_usd"] is None else round(g["catalog_cost_usd"], 6),
            "catalog_calls": g["catalog_calls"],
            "catalog_cost_per_success_usd": per_catalog["value"],
            "reported_to_catalog_ratio": ratio,
            "input_tokens": g["input_tokens"],
            "output_tokens": g["output_tokens"],
            "cache_read_tokens": g["cache_read_tokens"],
            "cache_creation_tokens": g["cache_creation_tokens"],
        })
    out.sort(key=lambda r: (SURFACES.index(r["surface"]), -(r["total_cost_usd"] or 0.0), r["model"]))
    return out


def summarize_by_surface(
    items: List[Dict[str, Any]],
    *,
    min_sample: int = MIN_SUCCESS_SAMPLE,
) -> List[Dict[str, Any]]:
    """표면 × 비용출처 합계. 모델만 접는다 — 비용출처는 여전히 접지 않는다.

    M9 의 "성공 작업당 실제 비용" 은 이 행의 cost_per_success_usd 다. 분모는
    summarize_usage_rows 가 job_id 로 중복을 없앤 표면 단위 성공 작업 수다.
    """
    acc: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for it in items:
        key = (it["surface"], it["cost_source"])
        a = acc.setdefault(key, {
            "surface": it["surface"],
            "cost_source": it["cost_source"],
            "samples": 0,
            "total_cost_usd": None,
            "catalog_cost_usd": None,
            "jobs": it.get("jobs"),
            "success_jobs": it.get("success_jobs"),
        })
        a["samples"] += it["samples"]
        if it["total_cost_usd"] is not None:
            a["total_cost_usd"] = round((a["total_cost_usd"] or 0.0) + it["total_cost_usd"], 6)
        if it.get("catalog_cost_usd") is not None:
            a["catalog_cost_usd"] = round((a["catalog_cost_usd"] or 0.0) + it["catalog_cost_usd"], 6)
    for a in acc.values():
        defined = a["surface"] in _DENOMINATOR_DEFINED_SURFACES
        per = cost_per_success(
            a["total_cost_usd"], a["success_jobs"], denominator_defined=defined, min_sample=min_sample
        )
        per_catalog = cost_per_success(
            a["catalog_cost_usd"], a["success_jobs"], denominator_defined=defined, min_sample=min_sample
        )
        a["cost_per_success_usd"] = per["value"]
        a["cost_per_success_status"] = per["status"]
        a["catalog_cost_per_success_usd"] = per_catalog["value"]
    return sorted(acc.values(), key=lambda r: (SURFACES.index(r["surface"]), r["cost_source"]))


# 작업 단위까지 쪼개 가져온다. 성공 작업 수는 모델 그룹마다 중복 없이 세야 하므로
# job_id 를 Python 쪽에서 합친다. 표면 판정은 여기서 하지 않는다(classify_surface).
COST_PER_SUCCESS_SQL = """
    SELECT o.call_source,
           o.model,
           o.cost_source,
           o.job_id,
           (cs.id IS NOT NULL) AS session_is_chat,
           pj.status AS job_status,
           COUNT(*)::bigint AS calls,
           SUM(o.cost_usd) AS cost_usd,
           SUM(o.cost_usd_catalog) AS cost_usd_catalog,
           COUNT(o.cost_usd_catalog)::bigint AS catalog_calls,
           SUM(o.cost_usd) FILTER (
               WHERE o.cost_usd IS NOT NULL AND o.cost_usd_catalog IS NOT NULL
           ) AS both_cost_usd,
           SUM(o.cost_usd_catalog) FILTER (
               WHERE o.cost_usd IS NOT NULL AND o.cost_usd_catalog IS NOT NULL
           ) AS both_catalog_usd,
           COALESCE(SUM(o.input_tokens), 0)::bigint AS input_tokens,
           COALESCE(SUM(o.output_tokens), 0)::bigint AS output_tokens,
           COALESCE(SUM(o.cache_read_tokens), 0)::bigint AS cache_read_tokens,
           COALESCE(SUM(o.cache_creation_tokens), 0)::bigint AS cache_creation_tokens
      FROM oauth_usage_log o
      LEFT JOIN chat_sessions cs ON cs.id::text = o.session_id
      LEFT JOIN pipeline_jobs pj ON pj.job_id = o.job_id
     WHERE o.created_at >= NOW() - (INTERVAL '1 day' * $1::int)
     GROUP BY 1, 2, 3, 4, 5, 6
"""


async def fetch_cost_per_success(conn: Any, days: int = 7) -> Dict[str, Any]:
    rows = await conn.fetch(COST_PER_SUCCESS_SQL, int(days))
    items = summarize_usage_rows([dict(r) for r in rows])
    return {
        "days": int(days),
        "source": "oauth_usage_log",
        "min_success_sample": MIN_SUCCESS_SAMPLE,
        "by_surface": summarize_by_surface(items),
        "items": items,
        "notes": [
            "total_cost_usd 는 cost_source 별로 따로 합산한다. relay_reported 와 "
            "catalog_estimated 를 더해 하나의 비용으로 읽지 마라.",
            "catalog_cost_usd 는 기록 시점 llm_models 정가(input/output)로 다시 계산한 값이다. "
            "카탈로그에 캐시 단가가 없어 캐시 토큰은 포함하지 않는다. 마이그레이션 이전 행은 NULL 이다.",
            "reported_to_catalog_ratio 는 두 값이 모두 있는 행만으로 계산한다 — "
            "릴레이 보고값과 카탈로그 단가의 불일치 배율이다.",
            "성공 작업당 비용은 runner 표면만 정의된다(분모 pipeline_jobs.status='done'). "
            "chat/terminal_cli/service 는 분모 미정의로 NULL 이다.",
            "작업당 총비용은 by_surface 의 cost_per_success_usd 다(분모는 job_id 중복 제거). "
            "items 의 cost_per_success_usd 는 같은 분모로 나눈 모델별 몫이라 더하면 by_surface 와 같다.",
        ],
    }
