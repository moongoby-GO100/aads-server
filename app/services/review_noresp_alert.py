"""검수 무응답률 7일 롤링 경보.

코드 검수 LLM 이 응답하지 않으면 code_reviews 에 flag_category=
'REVIEW_MODEL_NO_RESPONSE' 행이 남는다. 지금까지는 이 비율이 오르는 것을
사람이 DB 를 열어봐야만 알 수 있었다(alert_history 에 검수 관련 경보 0건).

## 측정

- 분모: model_used IS DISTINCT FROM 'precheck' 인 행 (사전검사 행은 LLM 을 안 부른다)
- 분자: 분모 중 flag_category = 'REVIEW_MODEL_NO_RESPONSE' 인 행
- REVIEW_API_UNAVAILABLE / REVIEW_TIMEOUT 은 분자에 넣지 않는다(원인이 다른 별도 지표).
  본문에 건수만 병기한다.
- 판정 기준은 **행 기준률**. 재시도마다 행이 생기므로 job 기준률(고유 job_id)은
  더 낮다(10/12~10/14: 행 7.5% vs job 3.5%). job 기준은 본문에 병기만 한다.

## 임계 근거 (2026-10-22 실측 60일 이력, 7일 롤링 행 기준)

    장애      8/23~8/27 16~21% · 9/13 8.1% → 9/22 34.9% → 9/28 19.8% · 10/12 5.0%
              10/14 5.9% · 10/18 4.9%
    소량 잡음 9/12 0.5% · 9/29~10/6 0.7~1.1% (3~6건) · 10/19 1.5% · 10/20 1.2%
    정상      8/31~9/11 0% · 10/7~10/11 0% · 10/21~10/22 0%

가장 낮은 "실제 장애" 는 4.9%(10/18), 가장 높은 "소량 잡음" 은 1.5%(10/19)다.
기본 3.0% 는 그 사이에 있고 양쪽 여유가 1.5%p/1.9%p 라 어느 쪽으로도 한 건
차이로 뒤집히지 않는다. 최소 표본 100 은 주간 분모가 보통 450~1,800 이므로
평시에는 걸리지 않고, 가동이 멈춘 주(분모 수십 건)의 1~2건이 퍼센트를 흔드는
것만 막는다. CRITICAL 9% 는 1.5배 이상 심한 구간(9/13 8.1% 직상)이다.
백테스트(scripts/review_noresp_alert_check.py --backtest)와
tests/unit/test_review_noresp_alert.py 가 이 이력을 재생해 기본값이 유지됨을 확인한다.
기본값이 바뀌면 위 표와 테스트를 같이 고친다.

## 상태 저장과 중복 억제 — 서로 다른 세 개념이다

새 테이블이 없다. 알림함 `alert_history` 안에서 **사용자에게 보낸 알림**과 **장애 생명주기 기록**을
category 로 나눈다.

1. **사용자 알림 = category `review_noresp_rate`** — 실제로 발송된 경보(warning/critical)와 해소(info)만
   쌓는다. 발송하지 않은 것은 이 category 에 절대 넣지 않는다("미발송을 발송 성공으로 기록" 금지).
   일일 예산(상한)과 제목 회차는 이 행들만 센다.
2. **장애 생명주기(영속 상태) = 두 category 를 합친 가장 최근 행** — severity 가 warning/critical 이면
   장애 중, info 면 정상. 일일 상한 때문에 경보를 보내지 않은 재발은 category
   `review_noresp_state` 에 확인 완료(acknowledged=true) 행으로 남겨 장애 중임을 영속화하고, 그 장애의
   해소도 같은 category 의 info 행으로 닫는다. 알림이 아니므로 사용자에게 가지 않는다. 메모리 플래그가
   없으므로 재시작해도 알림함에서 복구된다. 기록(INSERT)이 실패하면 행이 없어 상태가 안 바뀌고 다음
   주기에 같은 조건에서 재시도된다(M3 runner-43c69c7e 반려 사유: 실패했는데 "보냈다" 표식이 남는 패턴).
3. **제목 dedupe** — notify(dedupe_minutes=1440) 가 같은 제목의 재발송을 막는다. "이미 알렸다" 만 뜻한다.

예전에는 1·2·3 이 한 제목·한 category 에 섞여 있었다. (a) 같은 날 경보→해소→재발이면 두 번째 경보가
제목 dedupe 로 막혀 마지막 행이 해소(info)로 남았고, (b) 상한 뒤에는 경보를 안 보내니 아무 행도 없어
장애 중인데 정상으로 읽혔고, (c) 일일 예산을 "오늘 해소 수 + 1" 로 세서 전날 장애의 오늘 해소가
오늘 예산을 소진했다.

### 같은 KST 날짜의 재발 정책

- 경보 제목 회차 = 그날 **발송된** 경보 수 + 1: `…(재발 n-1회) — D`. 해소 제목 회차 = 그날 발송된
  해소 수 + 1: `…(n회차) — D`. 같은 알림은 하루 1회(제목 dedupe)이고 재시도는 같은 제목이다.
- 하루 상한 `REVIEW_NORESP_ALERT_MAX_DAILY`(기본 3)는 **그날 발송된 경보 수**에 건다. 전날 시작한 장애의
  오늘 해소는 예산을 쓰지 않는다. 상한에 닿은 뒤의 새 장애는 경보를 보내지 않고(`fire_suppressed`)
  장애 중 상태만 기록하며, 사용자에게 한 번도 알리지 않은 장애이므로 해소 알림도 보내지 않고
  (`resolve_suppressed`) 상태만 정상으로 닫는다.
- 다음 KST 날짜에도 장애가 이어지면 새 날짜 제목으로 경보가 다시 나간다(예산은 날짜별).

시험 발송은 category='review_noresp_rate_test' 로 분리되어 상태 판정과 오탐 집계에서 빠진다.
"""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import structlog

logger = structlog.get_logger(__name__)

KST = timezone(timedelta(hours=9))

NORESP_CATEGORY = "REVIEW_MODEL_NO_RESPONSE"
PRECHECK_MODEL = "precheck"

ALERT_CATEGORY = "review_noresp_rate"
STATE_CATEGORY = "review_noresp_state"
STATE_CATEGORIES = (ALERT_CATEGORY, STATE_CATEGORY)
TEST_ALERT_CATEGORY = "review_noresp_rate_test"
DEDUPE_MINUTES = 1440
STATE_LOOKBACK_DAYS = 30
OPEN_SEVERITIES = ("warning", "critical")
RESOLVED_SEVERITY = "info"

DECISION_INSUFFICIENT = "insufficient"
DECISION_OK = "ok"
DECISION_WARNING = "warning"
DECISION_CRITICAL = "critical"


@dataclass(frozen=True)
class AlertConfig:
    threshold_pct: float = 3.0
    critical_pct: float = 9.0
    min_samples: int = 100
    window_days: int = 7
    enabled: bool = True
    interval_minutes: int = 30
    max_daily_incidents: int = 3


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(float(os.getenv(name, "")))
    except ValueError:
        return default
    return value if value > 0 else default


def load_config() -> AlertConfig:
    threshold = _env_float("REVIEW_NORESP_ALERT_PCT", 3.0)
    critical = max(_env_float("REVIEW_NORESP_ALERT_CRITICAL_PCT", 9.0), threshold)
    return AlertConfig(
        threshold_pct=threshold,
        critical_pct=critical,
        min_samples=_env_int("REVIEW_NORESP_ALERT_MIN_SAMPLES", 100),
        window_days=_env_int("REVIEW_NORESP_ALERT_WINDOW_DAYS", 7),
        enabled=os.getenv("REVIEW_NORESP_ALERT_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        interval_minutes=_env_int("REVIEW_NORESP_ALERT_INTERVAL_MIN", 30),
        max_daily_incidents=_env_int("REVIEW_NORESP_ALERT_MAX_DAILY", 3),
    )


@dataclass(frozen=True)
class NoRespStats:
    window_start: datetime
    window_end: datetime
    numerator: int = 0
    denominator: int = 0
    job_numerator: int = 0
    job_denominator: int = 0
    last24h_numerator: int = 0
    api_unavailable: int = 0
    timeout: int = 0
    top_flag_categories: list = field(default_factory=list)
    top_models: list = field(default_factory=list)
    sample_job_ids: list = field(default_factory=list)

    @property
    def row_rate_pct(self) -> float:
        return 100.0 * self.numerator / self.denominator if self.denominator else 0.0

    @property
    def job_rate_pct(self) -> float:
        return (
            100.0 * self.job_numerator / self.job_denominator
            if self.job_denominator
            else 0.0
        )


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def compute_noresp_rate(
    rows: Iterable[Any], now: datetime, window_days: int = 7
) -> NoRespStats:
    """[now - window_days, now) 구간 행에서 무응답률을 센다. DB 를 부르지 않는다.

    rows 의 각 원소는 job_id / flag_category / model_used / created_at 를 키로
    읽을 수 있어야 한다(dict, asyncpg Record). 분모 0 이면 모든 률은 0.0.
    """
    now = _aware(now)
    start = now - timedelta(days=window_days)
    day_ago = now - timedelta(hours=24)

    den = num = last24 = api_unavail = timeout = 0
    jobs_all: set = set()
    jobs_noresp: set = set()
    flag_counts: Counter = Counter()
    model_counts: Counter = Counter()
    recent_noresp: list = []

    for row in rows:
        created = row["created_at"]
        if created is None:
            continue
        created = _aware(created)
        if not (start <= created < now):
            continue
        model = row["model_used"]
        if model == PRECHECK_MODEL:
            continue
        den += 1
        job_id = row["job_id"]
        jobs_all.add(job_id)
        flag = row["flag_category"]
        if flag:
            flag_counts[flag] += 1
        if flag == "REVIEW_API_UNAVAILABLE":
            api_unavail += 1
        elif flag == "REVIEW_TIMEOUT":
            timeout += 1
        elif flag == NORESP_CATEGORY:
            num += 1
            jobs_noresp.add(job_id)
            model_counts[model or "(없음)"] += 1
            recent_noresp.append((created, job_id))
            if created >= day_ago:
                last24 += 1

    recent_noresp.sort(key=lambda item: item[0], reverse=True)
    sample_jobs: list = []
    for _, job_id in recent_noresp:
        if job_id not in sample_jobs:
            sample_jobs.append(job_id)
        if len(sample_jobs) >= 5:
            break

    return NoRespStats(
        window_start=start,
        window_end=now,
        numerator=num,
        denominator=den,
        job_numerator=len(jobs_noresp),
        job_denominator=len(jobs_all),
        last24h_numerator=last24,
        api_unavailable=api_unavail,
        timeout=timeout,
        top_flag_categories=flag_counts.most_common(2),
        top_models=model_counts.most_common(2),
        sample_job_ids=sample_jobs,
    )


def decide(numerator: int, denominator: int, cfg: AlertConfig) -> str:
    """표본 부족·0건은 절대 발화하지 않는다. 임계 이상이면 발화, 미만이면 정상."""
    if denominator <= 0 or denominator < cfg.min_samples:
        return DECISION_INSUFFICIENT
    if numerator <= 0:
        return DECISION_OK
    rate = 100.0 * numerator / denominator
    if rate >= cfg.critical_pct:
        return DECISION_CRITICAL
    if rate >= cfg.threshold_pct:
        return DECISION_WARNING
    return DECISION_OK


def _kst_date(ts: datetime) -> date:
    return _aware(ts).astimezone(KST).date()


def _fmt_kst(ts: datetime) -> str:
    return _aware(ts).astimezone(KST).strftime("%Y-%m-%d %H:%M")


def alert_title(now: datetime, ordinal: int = 1) -> str:
    """ordinal 은 그날 장애 회차(1부터). 1회차 제목은 날짜만 — 기존 제목과 호환."""
    tag = "" if ordinal <= 1 else f"(재발 {ordinal - 1}회)"
    return f"검수 무응답률 경보{tag} — {_kst_date(now).isoformat()}"


def resolved_title(now: datetime, ordinal: int = 1) -> str:
    tag = "" if ordinal <= 1 else f"({ordinal}회차)"
    return f"[해소] 검수 무응답률 정상 복귀{tag} — {_kst_date(now).isoformat()}"


def suppressed_title(now: datetime) -> str:
    return f"[억제·미발송] 검수 무응답률 경보 — {_kst_date(now).isoformat()}"


def suppressed_resolved_title(now: datetime) -> str:
    return f"[억제·미발송] 검수 무응답률 정상 복귀 — {_kst_date(now).isoformat()}"


def kst_day_start(now: datetime) -> datetime:
    d = _kst_date(now)
    return datetime(d.year, d.month, d.day, tzinfo=KST)


def check_sql(window_days: int) -> str:
    """알림 본문에 싣는 확인용 SQL. 창 길이는 판정에 쓰는 cfg.window_days 와 같아야 한다."""
    return (
        "SELECT job_id, flag_category, model_used, created_at FROM code_reviews "
        "WHERE flag_category = 'REVIEW_MODEL_NO_RESPONSE' "
        f"AND created_at > NOW() - INTERVAL '{int(window_days)} days' "
        "ORDER BY created_at DESC LIMIT 20;"
    )


def build_alert_message(stats: NoRespStats, cfg: AlertConfig, ordinal: int = 1) -> str:
    def _top(pairs: list) -> str:
        return ", ".join(f"{name} {cnt}건" for name, cnt in pairs) or "없음"

    recurrence = (
        [
            f"- 같은 KST 날짜의 재발 {ordinal - 1}회째입니다"
            f"(하루 최대 경보 {cfg.max_daily_incidents}회까지 알림)."
        ]
        if ordinal > 1
        else []
    )
    return "\n".join(
        [
            f"최근 {cfg.window_days}일({_fmt_kst(stats.window_start)} ~ "
            f"{_fmt_kst(stats.window_end)} KST) 검수 LLM 무응답률이 "
            f"임계 {cfg.threshold_pct:g}% 이상입니다.",
            f"- 행 기준(판정): {stats.numerator}/{stats.denominator} = "
            f"{stats.row_rate_pct:.1f}%",
            f"- job 기준(참고, 재시도 중복 제거): {stats.job_numerator}/"
            f"{stats.job_denominator} = {stats.job_rate_pct:.1f}%",
            f"- 최근 24시간 무응답: {stats.last24h_numerator}건",
            f"- flag_category 상위: {_top(stats.top_flag_categories)}",
            f"- 무응답 model_used 상위: {_top(stats.top_models)}",
            f"- 별도 지표(분자 제외): REVIEW_API_UNAVAILABLE {stats.api_unavailable}건, "
            f"REVIEW_TIMEOUT {stats.timeout}건",
            f"- 관련 job_id(최근순 최대 5): {', '.join(stats.sample_job_ids) or '없음'}",
            *recurrence,
            "확인:",
            "  docker exec aads-server python3 /app/scripts/review_noresp_alert_check.py --current",
            f"  {check_sql(cfg.window_days)}",
        ]
    )


def build_suppressed_message(stats: NoRespStats, cfg: AlertConfig, sent_today: int) -> str:
    return (
        f"장애 중이지만 오늘(KST) 경보 {sent_today}회가 이미 나가 일일 상한 "
        f"{cfg.max_daily_incidents}회에 도달해 사용자 알림은 보내지 않았습니다. "
        f"장애 생명주기 상태만 기록한 행입니다. 행 기준 {stats.numerator}/{stats.denominator} = "
        f"{stats.row_rate_pct:.1f}%."
    )


def build_resolved_message(stats: NoRespStats, cfg: AlertConfig) -> str:
    return (
        f"최근 {cfg.window_days}일 검수 무응답률이 임계 {cfg.threshold_pct:g}% 미만으로 "
        f"내려왔습니다. 행 기준 {stats.numerator}/{stats.denominator} = "
        f"{stats.row_rate_pct:.1f}%, job 기준 {stats.job_numerator}/"
        f"{stats.job_denominator} = {stats.job_rate_pct:.1f}%."
    )


# ── DB 접점 (테스트에서 이 세 함수만 바꾸면 SQL 없이 상태 흐름을 시험할 수 있다) ──

async def fetch_window_rows(pool: Any, start: datetime, end: datetime) -> list:
    return await pool.fetch(
        "SELECT job_id, flag_category, model_used, created_at FROM code_reviews "
        "WHERE created_at >= $1 AND created_at < $2",
        start, end,
    )


@dataclass(frozen=True)
class LifecycleState:
    """장애 생명주기의 가장 최근 행. 사용자 알림(ALERT_CATEGORY)과 미발송 기록(STATE_CATEGORY)을 합쳐 본다."""

    category: str
    severity: str
    created_at: datetime

    @property
    def open(self) -> bool:
        return self.severity in OPEN_SEVERITIES

    @property
    def delivered(self) -> bool:
        """이 상태의 알림이 사용자에게 실제로 나갔는가. 미발송 기록이면 False."""
        return self.category == ALERT_CATEGORY


async def _latest_state(pool: Any, now: datetime) -> Optional[LifecycleState]:
    """장애 생명주기의 유일한 근거. 시험 알림(category 가 다름)은 보지 않는다."""
    row = await pool.fetchrow(
        "SELECT category, severity, created_at FROM alert_history "
        "WHERE category = ANY($1::text[]) AND created_at > $2 "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        list(STATE_CATEGORIES), now - timedelta(days=STATE_LOOKBACK_DAYS),
    )
    if row is None:
        return None
    return LifecycleState(row["category"], row["severity"], _aware(row["created_at"]))


def lifecycle_label(state: Optional[LifecycleState]) -> str:
    """--current 표시용. 억제된 장애를 정상으로 읽지 않도록 세 상태를 구분한다."""
    if state is None or not state.open:
        return "정상"
    if state.delivered:
        return "장애 중 (경보 발송됨)"
    return "장애 중 (일일 상한으로 경보 억제됨 — 사용자 미통지)"


async def _delivered_today(pool: Any, now: datetime) -> tuple:
    """오늘(KST) 실제로 발송된 (경보 수, 해소 수). 일일 예산과 제목 회차의 근거.

    미발송 기록(STATE_CATEGORY)과 시험 알림은 세지 않는다. 전날 시작한 장애의 오늘 해소는
    해소 수에만 들어가고 경보 예산을 쓰지 않는다.
    """
    day0 = kst_day_start(now)
    row = await pool.fetchrow(
        "SELECT count(*) FILTER (WHERE severity = ANY($4::text[])) AS fires, "
        "       count(*) FILTER (WHERE severity = $3) AS resolves "
        "FROM alert_history WHERE category = $1 AND created_at >= $2 AND created_at < $5",
        ALERT_CATEGORY, day0, RESOLVED_SEVERITY, list(OPEN_SEVERITIES), day0 + timedelta(days=1),
    )
    return int(row["fires"] or 0), int(row["resolves"] or 0)


async def _alert_exists(pool: Any, title: str, minutes: int) -> bool:
    found = await pool.fetchval(
        "SELECT 1 FROM alert_history WHERE title = $1 AND category = $2 "
        "AND created_at > NOW() - ($3 || ' minutes')::interval LIMIT 1",
        title, ALERT_CATEGORY, str(minutes),
    )
    return bool(found)


async def _send(pool: Any, title: str, message: str, severity: str) -> str:
    """'sent' | 'deduped' | 'failed'. 제목 dedupe 는 "이미 알렸다" 만 뜻한다(장애 상태가 아님).

    notify 는 중복과 실패를 둘 다 False 로 돌려준다. False 일 때 행이 이미 있으면
    중복(이미 보냈음), 없으면 실패다 — 실패는 아무 표식도 남기지 않으므로 다음 주기에
    같은 조건에서 자연히 재시도된다.
    """
    from app.services import ohvis_alert

    ok = await ohvis_alert.notify(
        title, message,
        severity=severity, category=ALERT_CATEGORY, project="AADS",
        dedupe_minutes=DEDUPE_MINUTES,
    )
    if ok:
        return "sent"
    if await _alert_exists(pool, title, DEDUPE_MINUTES):
        return "deduped"
    logger.warning("review_noresp_alert_send_failed title=%s", title)
    return "failed"


async def _record_state(pool: Any, title: str, message: str, severity: str) -> bool:
    """사용자에게 보내지 않은 장애 전이를 알림함에 기록한다(STATE_CATEGORY, 확인 완료 처리).

    notify 를 쓰지 않는다 — notify 는 "발송" 을 뜻하는 창구다. acknowledged=true 로 넣어
    미확인 경보 집계·재알림 대상에서 빠지게 한다. 실패하면 False 이고 행이 없으므로
    상태가 바뀌지 않아 다음 주기에 같은 조건에서 재시도된다.
    """
    try:
        await pool.execute(
            "INSERT INTO alert_history (severity, category, title, message, "
            "       project, server, acknowledged, acknowledged_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, true, NOW())",
            severity, STATE_CATEGORY, title, message, "AADS", None,
        )
        return True
    except Exception as exc:
        logger.warning("review_noresp_alert_state_record_failed title=%s error=%s",
                       title[:40], str(exc)[:160])
        return False


async def evaluate_and_notify(
    pool: Any = None,
    now: Optional[datetime] = None,
    cfg: Optional[AlertConfig] = None,
) -> dict:
    cfg = cfg or load_config()
    now = _aware(now) if now else datetime.now(timezone.utc)
    if pool is None:
        from app.core.db_pool import get_pool

        pool = get_pool()

    start = now - timedelta(days=cfg.window_days)
    rows = await fetch_window_rows(pool, start, now)
    stats = compute_noresp_rate(rows, now, cfg.window_days)
    decision = decide(stats.numerator, stats.denominator, cfg)
    result: dict = {"decision": decision, "stats": stats, "action": "none"}

    if decision in (DECISION_WARNING, DECISION_CRITICAL):
        state = await _latest_state(pool, now)
        fires, _ = await _delivered_today(pool, now)
        result["fires_today"] = fires
        severity = "critical" if decision == DECISION_CRITICAL else "warning"
        if state and state.open and state.created_at >= kst_day_start(now):
            # 오늘 이 장애의 경보를 이미 보냈거나(deduped) 상한으로 기록해 두었다(suppressed)
            result["action"] = "fire_deduped" if state.delivered else "fire_suppressed"
        elif fires >= cfg.max_daily_incidents:
            if state and state.open:
                result["action"] = "fire_suppressed"  # 이미 장애 중으로 기록돼 있다
            else:
                recorded = await _record_state(
                    pool,
                    suppressed_title(now),
                    build_suppressed_message(stats, cfg, fires),
                    severity,
                )
                result["action"] = "fire_suppressed" if recorded else "fire_suppressed_failed"
        else:
            ordinal = fires + 1
            result["ordinal"] = ordinal
            result["action"] = "fire_" + await _send(
                pool,
                alert_title(now, ordinal),
                build_alert_message(stats, cfg, ordinal),
                severity,
            )
    elif decision == DECISION_OK:
        state = await _latest_state(pool, now)
        if state and state.open:
            if state.delivered:
                _, resolves = await _delivered_today(pool, now)
                ordinal = resolves + 1
                result["ordinal"] = ordinal
                result["action"] = "resolve_" + await _send(
                    pool,
                    resolved_title(now, ordinal),
                    build_resolved_message(stats, cfg),
                    RESOLVED_SEVERITY,
                )
            else:
                recorded = await _record_state(
                    pool,
                    suppressed_resolved_title(now),
                    build_resolved_message(stats, cfg)
                    + " 이 장애의 경보는 일일 상한으로 사용자에게 보내지 않았으므로 해소 알림도 보내지 않습니다.",
                    RESOLVED_SEVERITY,
                )
                result["action"] = "resolve_suppressed" if recorded else "resolve_suppressed_failed"

    logger.info(
        "review_noresp_alert_evaluated decision=%s action=%s ordinal=%s num=%d den=%d row_pct=%.2f",
        decision, result["action"], result.get("ordinal"), stats.numerator, stats.denominator,
        stats.row_rate_pct,
    )
    return result


async def send_test_alert(now: Optional[datetime] = None) -> bool:
    """시험 알림 1건. 실제 경보와 category·severity·제목이 모두 다르다."""
    from app.services import ohvis_alert

    now = _aware(now) if now else datetime.now(timezone.utc)
    return await ohvis_alert.notify(
        f"[시험] 검수 무응답률 경보 시험 발송 — {_fmt_kst(now)}",
        "검수 무응답률 경보 수신 경로 확인용 시험 알림입니다. 실제 경보가 아닙니다.",
        severity=ohvis_alert.INFO,
        category=TEST_ALERT_CATEGORY,
        project="AADS",
        dedupe_minutes=0,
    )


# ── 백테스트 (순수) ──

def replay_backtest(
    rows: list, end: datetime, days: int, cfg: Optional[AlertConfig] = None
) -> list:
    """KST 일 단위로 [end 기준 days 일]을 재생한다.

    날짜 D 의 평가 시각은 D+1 00:00 KST(그날 끝)이며 end 를 넘지 않는다.
    오탐: (1) 7일 창에 무응답 행이 0건인데 발화 (2) 임계 미만인데 발화.
    """
    cfg = cfg or AlertConfig()
    end = _aware(end)
    last_day = _kst_date(end - timedelta(microseconds=1))  # 자정 정각이면 직전 날이 마지막 날
    out = []
    for offset in range(days - 1, -1, -1):
        day = last_day - timedelta(days=offset)
        eval_at = datetime(day.year, day.month, day.day, tzinfo=KST) + timedelta(days=1)
        eval_at = min(eval_at, end)
        stats = compute_noresp_rate(rows, eval_at, cfg.window_days)
        decision = decide(stats.numerator, stats.denominator, cfg)
        fired = decision in (DECISION_WARNING, DECISION_CRITICAL)
        out.append(
            {
                "date": day.isoformat(),
                "numerator": stats.numerator,
                "denominator": stats.denominator,
                "row_rate_pct": stats.row_rate_pct,
                "job_rate_pct": stats.job_rate_pct,
                "decision": decision,
                "fired": fired,
                "false_positive": fired
                and (stats.numerator == 0 or stats.row_rate_pct < cfg.threshold_pct),
            }
        )
    return out


# ── 스케줄 ──

async def run_review_noresp_alert() -> None:
    try:
        await evaluate_and_notify()
    except Exception as exc:
        logger.warning("review_noresp_alert_failed error=%s", str(exc)[:200])


def register_scheduler_job(scheduler: Any) -> bool:
    """REVIEW_NORESP_ALERT_ENABLED=0 이면 등록하지 않는다."""
    cfg = load_config()
    if not cfg.enabled:
        logger.info("review_noresp_alert_disabled")
        return False
    scheduler.add_job(
        run_review_noresp_alert,
        "interval",
        minutes=cfg.interval_minutes,
        id="review_noresp_alert",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return True
