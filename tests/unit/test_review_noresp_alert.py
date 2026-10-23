"""검수 무응답률 7일 롤링 경보 — 단위 테스트.

DB 없이 돈다. 알림함(alert_history)은 메모리 목록으로 대체하므로 여기서는 상태 흐름의
논리만 검증한다. 실제 ohvis_alert.notify 의 SQL/INSERT/dedupe 와 집계 SQL 은
tests/integration/test_review_noresp_alert_postgres.py (격리 PostgreSQL) 가 검증한다.
"""
import asyncio
import inspect
from datetime import datetime, timedelta

import pytest

from app.services import ohvis_alert
from app.services import review_noresp_alert as nr

KST = nr.KST
CFG = nr.AlertConfig()  # 3.0 / 9.0 / 100 / 7


def _row(job, flag=None, model="qwen", at=None):
    return {"job_id": job, "flag_category": flag, "model_used": model, "created_at": at}


def _rows(num, den, at, flag=nr.NORESP_CATEGORY, model="qwen"):
    """분모 den 행 중 num 행이 무응답. job_id 는 행마다 고유."""
    return [
        _row(f"j{i}", flag if i < num else None, model, at) for i in range(den)
    ]


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=KST)
AT = NOW - timedelta(days=1)


# ── 순수 계산 ──

def test_zero_denominator_has_no_division_error():
    s = nr.compute_noresp_rate([], NOW)
    assert (s.numerator, s.denominator, s.row_rate_pct, s.job_rate_pct) == (0, 0, 0.0, 0.0)
    assert nr.decide(0, 0, CFG) == nr.DECISION_INSUFFICIENT


def test_below_min_samples_never_fires_even_at_100_percent():
    assert nr.decide(99, 99, CFG) == nr.DECISION_INSUFFICIENT
    assert nr.decide(50, 99, CFG) == nr.DECISION_INSUFFICIENT
    assert nr.decide(100, 100, CFG) == nr.DECISION_CRITICAL


def test_threshold_boundary():
    assert nr.decide(29, 1000, CFG) == nr.DECISION_OK        # 2.9%
    assert nr.decide(30, 1000, CFG) == nr.DECISION_WARNING   # 3.0% — 임계 이상
    assert nr.decide(31, 1000, CFG) == nr.DECISION_WARNING
    assert nr.decide(89, 1000, CFG) == nr.DECISION_WARNING   # 8.9%
    assert nr.decide(90, 1000, CFG) == nr.DECISION_CRITICAL  # 9.0%


def test_zero_numerator_never_fires():
    tiny = nr.AlertConfig(threshold_pct=0.0001, critical_pct=0.0002, min_samples=1)
    assert nr.decide(0, 1000, tiny) == nr.DECISION_OK
    assert nr.decide(0, 1, tiny) == nr.DECISION_OK


def test_precheck_rows_excluded_from_denominator_and_numerator():
    rows = _rows(10, 200, AT) + [
        _row(f"p{i}", nr.NORESP_CATEGORY, "precheck", AT) for i in range(300)
    ]
    s = nr.compute_noresp_rate(rows, NOW)
    assert (s.numerator, s.denominator) == (10, 200)
    # model_used NULL 은 precheck 가 아니므로 분모에 든다 (IS DISTINCT FROM)
    s2 = nr.compute_noresp_rate([_row("n", None, None, AT)], NOW)
    assert s2.denominator == 1


def test_api_unavailable_and_timeout_not_in_numerator():
    rows = (
        _rows(2, 100, AT)
        + [_row(f"a{i}", "REVIEW_API_UNAVAILABLE", "qwen", AT) for i in range(30)]
        + [_row(f"t{i}", "REVIEW_TIMEOUT", "qwen", AT) for i in range(20)]
    )
    s = nr.compute_noresp_rate(rows, NOW)
    assert s.numerator == 2
    assert s.denominator == 150
    assert (s.api_unavailable, s.timeout) == (30, 20)
    assert nr.decide(s.numerator, s.denominator, CFG) == nr.DECISION_OK


def test_window_edges_and_naive_timestamps():
    inside = _row("in", nr.NORESP_CATEGORY, "m", NOW - timedelta(days=7))      # 시작 포함
    outside = _row("out", nr.NORESP_CATEGORY, "m", NOW - timedelta(days=7, seconds=1))
    future = _row("fut", nr.NORESP_CATEGORY, "m", NOW)                          # 끝 제외
    s = nr.compute_noresp_rate([inside, outside, future], NOW)
    assert s.denominator == 1
    naive = _row("nv", None, "m", datetime(2026, 10, 21, 0, 0))  # UTC 로 간주
    assert nr.compute_noresp_rate([naive], NOW).denominator == 1


def test_job_rate_dedupes_retries_and_message_fields():
    rows = (
        [_row("A", nr.NORESP_CATEGORY, "qwen", AT) for _ in range(3)]      # A 재시도 3행
        + [_row("B", nr.NORESP_CATEGORY, "gemini", AT)]
        + [_row(f"ok{i}", None, "qwen", AT) for i in range(6)]
        + [_row("old", nr.NORESP_CATEGORY, "qwen", NOW - timedelta(days=3))]  # 24h 밖
        + [_row("x", "REVIEW_TIMEOUT", "qwen", AT)]
    )
    s = nr.compute_noresp_rate(rows, NOW)
    assert (s.numerator, s.denominator) == (5, 12)
    assert (s.job_numerator, s.job_denominator) == (3, 10)  # {A,B,old} / {A,B,old,x,ok0-5}
    assert s.last24h_numerator == 4
    assert s.top_models[0] == ("qwen", 4)
    assert s.top_flag_categories[0] == (nr.NORESP_CATEGORY, 5)
    assert len(s.top_flag_categories) == 2 and len(s.top_models) == 2
    msg = nr.build_alert_message(s, CFG)
    for needle in ("5/12", "3/10", "최근 24시간 무응답: 4건", "qwen 4건", "REVIEW_TIMEOUT 1건", "--current"):
        assert needle in msg


def test_sample_job_ids_capped_at_five_most_recent_first():
    rows = [
        _row(f"j{i}", nr.NORESP_CATEGORY, "m", NOW - timedelta(hours=i + 1)) for i in range(9)
    ]
    s = nr.compute_noresp_rate(rows, NOW)
    assert s.sample_job_ids == ["j0", "j1", "j2", "j3", "j4"]


# ── 상태 흐름 (알림함 = 메모리 목록) ──

class Inbox:
    def __init__(self):
        self.alerts = []
        self.fail_next = 0
        self.rows = []
        self.now = NOW

    def install(self, monkeypatch):
        async def fake_fetch(pool, start, end):
            return self.rows

        async def fake_latest(pool, now):
            hits = [a for a in self.alerts if a["category"] in nr.STATE_CATEGORIES]
            if not hits:
                return None
            last = hits[-1]
            return nr.LifecycleState(last["category"], last["severity"], last["at"])

        async def fake_delivered_today(pool, now):
            day0 = nr.kst_day_start(now)
            today = [
                a for a in self.alerts
                if a["category"] == nr.ALERT_CATEGORY and day0 <= a["at"] < day0 + timedelta(days=1)
            ]
            return (
                len([a for a in today if a["severity"] in ("warning", "critical")]),
                len([a for a in today if a["severity"] == "info"]),
            )

        async def fake_record_state(pool, title, message, severity):
            if self.fail_next:
                self.fail_next -= 1
                return False
            self.alerts.append(dict(title=title, message=message, severity=severity,
                                    category=nr.STATE_CATEGORY, project="AADS", dedupe=0,
                                    acknowledged=True, at=self.now))
            return True

        async def fake_exists(pool, title, minutes):
            return any(a["title"] == title and a["category"] == nr.ALERT_CATEGORY for a in self.alerts)

        async def fake_notify(title, message="", *, severity="info", category="general",
                              project=None, server=None, dedupe_minutes=0):
            if self.fail_next:
                self.fail_next -= 1
                return False
            if dedupe_minutes and any(a["title"] == title for a in self.alerts):
                return False
            self.alerts.append(dict(title=title, message=message, severity=severity,
                                    category=category, project=project, dedupe=dedupe_minutes,
                                    at=self.now))
            return True

        monkeypatch.setattr(nr, "fetch_window_rows", fake_fetch)
        monkeypatch.setattr(nr, "_latest_state", fake_latest)
        monkeypatch.setattr(nr, "_delivered_today", fake_delivered_today)
        monkeypatch.setattr(nr, "_record_state", fake_record_state)
        monkeypatch.setattr(nr, "_alert_exists", fake_exists)
        monkeypatch.setattr(ohvis_alert, "notify", fake_notify)

    def run(self, now=NOW, cfg=CFG):
        self.now = now
        return asyncio.run(nr.evaluate_and_notify(pool=object(), now=now, cfg=cfg))

    def sent(self):
        """사용자에게 실제 발송된 알림만."""
        return [a for a in self.alerts if a["category"] == nr.ALERT_CATEGORY]

    def state_rows(self):
        """발송하지 않은 장애 전이 기록."""
        return [a for a in self.alerts if a["category"] == nr.STATE_CATEGORY]

    def label(self):
        return nr.lifecycle_label(asyncio.run(nr._latest_state(object(), self.now)))


@pytest.fixture
def inbox(monkeypatch):
    box = Inbox()
    box.install(monkeypatch)
    return box


def test_fire_once_per_day_then_next_day_again(inbox):
    inbox.rows = _rows(50, 1000, AT)  # 5%
    assert inbox.run()["action"] == "fire_sent"
    assert inbox.run(NOW + timedelta(minutes=30))["action"] == "fire_deduped"
    assert len(inbox.alerts) == 1
    a = inbox.alerts[0]
    assert (a["severity"], a["category"], a["project"], a["dedupe"]) == (
        "warning", "review_noresp_rate", "AADS", 1440)
    assert "2026-10-22" in a["title"]
    # KST 날짜가 바뀌면 같은 장애의 다음 날 알림
    assert inbox.run(NOW + timedelta(hours=13))["action"] == "fire_sent"
    assert len(inbox.alerts) == 2


def test_critical_promotion(inbox):
    inbox.rows = _rows(100, 1000, AT)  # 10%
    assert inbox.run()["action"] == "fire_sent"
    assert inbox.alerts[0]["severity"] == "critical"


def test_notify_failure_leaves_no_marker_and_is_retried_without_duplicates(inbox):
    inbox.rows = _rows(50, 1000, AT)
    inbox.fail_next = 1
    assert inbox.run()["action"] == "fire_failed"
    assert inbox.alerts == []
    assert inbox.run(NOW + timedelta(minutes=30))["action"] == "fire_sent"
    assert len(inbox.alerts) == 1
    assert inbox.run(NOW + timedelta(minutes=60))["action"] == "fire_deduped"
    assert len(inbox.alerts) == 1


def test_resolved_sent_once_per_incident(inbox):
    inbox.rows = _rows(50, 1000, AT)
    inbox.run()
    inbox.rows = _rows(0, 1000, AT)
    t = NOW + timedelta(hours=1)
    assert inbox.run(t)["action"] == "resolve_sent"
    assert inbox.alerts[-1]["severity"] == "info"
    assert inbox.alerts[-1]["title"].startswith("[해소]")
    assert inbox.run(t + timedelta(minutes=30))["action"] == "none"
    assert inbox.run(t + timedelta(days=1))["action"] == "none"
    assert len(inbox.alerts) == 2


def test_resolved_failure_is_retried(inbox):
    inbox.rows = _rows(50, 1000, AT)
    inbox.run()
    inbox.rows = _rows(0, 1000, AT)
    inbox.fail_next = 1
    assert inbox.run(NOW + timedelta(hours=1))["action"] == "resolve_failed"
    assert inbox.run(NOW + timedelta(hours=2))["action"] == "resolve_sent"
    assert len([a for a in inbox.alerts if a["severity"] == "info"]) == 1


def test_no_resolved_without_prior_alert_or_with_insufficient_sample(inbox):
    inbox.rows = _rows(0, 1000, AT)
    assert inbox.run()["action"] == "none"
    inbox.rows = _rows(50, 1000, AT)
    inbox.run()
    inbox.rows = _rows(0, 10, AT)  # 표본 부족 — 해소 판단 불가
    assert inbox.run(NOW + timedelta(hours=1))["action"] == "none"


# ── 같은 날 재발: 일일 중복 제한과 장애 생명주기의 분리 ──

def _noresp(inbox, on):
    inbox.rows = _rows(50 if on else 0, 1000, AT)


def _step(inbox, minutes, on):
    _noresp(inbox, on)
    return inbox.run(NOW + timedelta(minutes=minutes))["action"]


def test_same_day_fire_resolve_recur_resolve_again_sends_every_stage(inbox):
    """검수가 재현한 순서(50→0→50→0, 30분 간격). 예전에는 fire_sent, resolve_sent,
    fire_deduped, none 이라 두 번째 장애 동안 상태가 INFO 로 남고 두 번째 해소가 누락됐다."""
    got = [_step(inbox, 0, True), _step(inbox, 30, False), _step(inbox, 60, True),
           _step(inbox, 90, False), _step(inbox, 120, False)]
    assert got == ["fire_sent", "resolve_sent", "fire_sent", "resolve_sent", "none"]
    assert [a["severity"] for a in inbox.alerts] == ["warning", "info", "warning", "info"]
    titles = [a["title"] for a in inbox.alerts]
    assert len(set(titles)) == 4                      # 제목이 겹치지 않아 dedupe 에 안 막힌다
    assert "재발 1회" in titles[2] and "2회차" in titles[3]
    assert "재발 1회째" in inbox.alerts[2]["message"]


def test_recurrence_in_progress_is_not_misread_as_info(inbox):
    _step(inbox, 0, True)
    _step(inbox, 30, False)
    assert _step(inbox, 60, True) == "fire_sent"
    # 재발 중에 다시 평가해도 새 알림이 쌓이지 않고(deduped), 상태는 경보 중
    assert _step(inbox, 90, True) == "fire_deduped"
    assert inbox.alerts[-1]["severity"] == "warning"
    assert len(inbox.alerts) == 3


def test_daily_cap_limits_sent_alerts_but_persists_suppressed_incident_state(inbox):
    actions = []
    for i in range(4):                                  # 4번 장애→해소
        actions.append(_step(inbox, i * 60, True))
        if i == 3:
            # 상한 뒤 재발: 사용자에게는 안 보내지만 장애 중 상태는 영속돼야 한다(예전: info 로 정상 오판)
            assert inbox.label().startswith("장애 중 (일일 상한으로 경보 억제됨")
            assert inbox.state_rows()[-1]["severity"] == "warning"
            assert inbox.state_rows()[-1]["acknowledged"] is True
        actions.append(_step(inbox, i * 60 + 30, False))
    assert actions == ["fire_sent", "resolve_sent"] * 3 + ["fire_suppressed", "resolve_suppressed"]
    assert len(inbox.sent()) == 6                       # 발송은 하루 3회차 × (경보+해소) 그대로
    assert [a["severity"] for a in inbox.state_rows()] == ["warning", "info"]
    assert inbox.label() == "정상"
    assert _step(inbox, 4 * 60, False) == "none"        # 억제 장애의 해소도 한 번만
    assert len(inbox.state_rows()) == 2
    # 다음 KST 날짜에도 장애가 이어지면 날짜가 바뀐 새 제목으로 다시 경보가 나간다
    _noresp(inbox, True)
    assert inbox.run(NOW + timedelta(days=1))["action"] == "fire_sent"
    assert "2026-10-23" in inbox.sent()[-1]["title"]
    assert not inbox.sent()[-1]["title"].count("재발")  # 새 날짜는 1회차


def test_suppressed_state_is_never_recorded_as_a_sent_alert(inbox):
    cfg = nr.AlertConfig(max_daily_incidents=1)
    for m, on in ((0, True), (30, False), (60, True)):
        _noresp(inbox, on); inbox.run(NOW + timedelta(minutes=m), cfg)
    sent_titles = [a["title"] for a in inbox.sent()]
    assert len(sent_titles) == 2 and not any("억제" in t for t in sent_titles)
    rec = inbox.state_rows()[0]
    assert rec["category"] == nr.STATE_CATEGORY != nr.ALERT_CATEGORY
    assert rec["title"].startswith("[억제·미발송]") and "보내지 않았습니다" in rec["message"]
    assert rec["dedupe"] == 0


def test_suppressed_incident_closes_once_without_orphan_resolve_notification(inbox):
    cfg = nr.AlertConfig(max_daily_incidents=1)
    got = []
    for m, on in ((0, True), (30, False), (60, True), (90, True), (120, True),
                  (150, False), (180, False)):
        _noresp(inbox, on)
        got.append(inbox.run(NOW + timedelta(minutes=m), cfg)["action"])
    assert got == ["fire_sent", "resolve_sent", "fire_suppressed", "fire_suppressed",
                   "fire_suppressed", "resolve_suppressed", "none"]
    assert len(inbox.sent()) == 2                       # 마지막 발송은 해소 — 인지 상태와 모순 없음
    assert [a["severity"] for a in inbox.state_rows()] == ["warning", "info"]   # 반복 평가가 행을 쌓지 않는다
    assert "억제" in inbox.state_rows()[1]["title"] and "해소 알림도 보내지 않습니다" in inbox.state_rows()[1]["message"]


def test_budget_counts_sent_fires_of_that_kst_day_not_resolutions_p1_2(inbox):
    """전날 장애의 오늘 해소가 오늘 경보 예산을 소진하던 결함. cap=1, KST 10/22 23:30 → 10/23 00:30."""
    cfg = nr.AlertConfig(max_daily_incidents=1)
    t = datetime(2026, 10, 22, 23, 30, tzinfo=KST)
    got = []
    for m, on in ((0, True), (30, False), (60, True), (90, False)):
        _noresp(inbox, on)
        got.append(inbox.run(t + timedelta(minutes=m), cfg)["action"])
    # 10/23 첫 경보가 억제되지 않는다(성공 경보 0건). 같은 날 두 번째 장애부터 상한(1)이 적용된다.
    assert got == ["fire_sent", "resolve_sent", "fire_sent", "resolve_sent"]
    titles = [a["title"] for a in inbox.sent()]
    assert "2026-10-22" in titles[0] and all("2026-10-23" in x for x in titles[1:])
    assert len(set(titles)) == 4 and inbox.state_rows() == []
    _noresp(inbox, True)
    assert inbox.run(t + timedelta(minutes=120), cfg)["action"] == "fire_suppressed"   # 10/23 의 두 번째 경보


def test_default_cap_with_prior_day_unresolved_incident_boundary(inbox):
    """기본 상한 3: 전날 경보 3회를 쓰고 미해소로 자정을 넘긴 장애. 오늘 해소는 예산을 쓰지 않는다."""
    t = datetime(2026, 10, 22, 20, 0, tzinfo=KST)
    got = []
    for m, on in ((0, True), (30, False), (60, True), (90, False), (120, True)):   # 20:00~22:00, 경보 3회
        _noresp(inbox, on)
        got.append(inbox.run(t + timedelta(minutes=m))["action"])
    assert got == ["fire_sent", "resolve_sent", "fire_sent", "resolve_sent", "fire_sent"]
    # 자정을 넘겨 계속되는 장애 → 새 날짜 예산으로 경보. 이어서 오늘 해소, 오늘 재발 3회까지 모두 발송.
    plan = [(240, True, "fire_sent"), (270, False, "resolve_sent"), (300, True, "fire_sent"),
            (330, False, "resolve_sent"), (360, True, "fire_sent"), (390, False, "resolve_sent"),
            (420, True, "fire_suppressed"), (450, False, "resolve_suppressed")]
    for m, on, want in plan:
        _noresp(inbox, on)
        assert inbox.run(t + timedelta(minutes=m))["action"] == want, (m, on)
    today = [a for a in inbox.sent() if "2026-10-23" in a["title"]]
    assert [a["severity"] for a in today] == ["warning", "info"] * 3
    assert len({a["title"] for a in inbox.sent()}) == len(inbox.sent())


def test_budget_boundary_prior_day_open_incident_resolved_today_default_cap(inbox):
    t = datetime(2026, 10, 22, 20, 0, tzinfo=KST)
    seq = [(0, True), (30, False), (60, True), (90, False), (120, True)]    # 전날 경보 3회, 마지막은 미해소(22:00)
    for m, on in seq:
        _noresp(inbox, on); inbox.run(t + timedelta(minutes=m))
    assert len([a for a in inbox.sent() if a["severity"] == "warning"]) == 3
    _noresp(inbox, False)
    assert inbox.run(t + timedelta(hours=5))["action"] == "resolve_sent"     # 10/23 01:00 에 해소
    for k in range(3):                                                          # 오늘 예산 3회가 온전히 남아 있다
        _noresp(inbox, True)
        assert inbox.run(t + timedelta(hours=6 + 2 * k))["action"] == "fire_sent"
        _noresp(inbox, False)
        assert inbox.run(t + timedelta(hours=7 + 2 * k))["action"] == "resolve_sent"
    _noresp(inbox, True)
    assert inbox.run(t + timedelta(hours=12))["action"] == "fire_suppressed"


@pytest.mark.parametrize("failing_stage", [0, 1, 2, 3])
def test_each_stage_failure_is_retried_without_duplicate_or_loss(inbox, failing_stage):
    plan = [True, False, True, False]
    for stage, on in enumerate(plan):
        inbox.fail_next = 1 if stage == failing_stage else 0
        first = _step(inbox, stage * 60, on)
        if stage == failing_stage:
            assert first.endswith("_failed")
            assert len(inbox.alerts) == stage           # 실패는 흔적을 남기지 않는다
            assert _step(inbox, stage * 60 + 30, on).endswith("_sent")   # 다음 주기 재시도
        else:
            assert first.endswith("_sent")
        # 같은 상태를 한 번 더 평가해도 성공한 알림은 다시 안 나간다
        again = _step(inbox, stage * 60 + 45, on)
        assert again in ("fire_deduped", "none")
    assert [a["severity"] for a in inbox.alerts] == ["warning", "info", "warning", "info"]
    assert len({a["title"] for a in inbox.alerts}) == 4


@pytest.mark.parametrize("failing_stage", [2, 3])
def test_suppressed_state_record_failure_is_retried_without_loss_or_duplicate(inbox, failing_stage):
    cfg = nr.AlertConfig(max_daily_incidents=1)
    plan = [True, False, True, False]
    want = ["fire_sent", "resolve_sent", "fire_suppressed", "resolve_suppressed"]
    for stage, on in enumerate(plan):
        inbox.fail_next = 1 if stage == failing_stage else 0
        _noresp(inbox, on)
        got = inbox.run(NOW + timedelta(minutes=stage * 60), cfg)["action"]
        if stage == failing_stage:
            assert got == want[stage] + "_failed"
            assert len(inbox.alerts) == stage           # 기록 실패는 흔적이 없다 → 상태 불변
            _noresp(inbox, on)
            assert inbox.run(NOW + timedelta(minutes=stage * 60 + 30), cfg)["action"] == want[stage]
        else:
            assert got == want[stage]
    assert [(a["category"], a["severity"]) for a in inbox.alerts] == [
        (nr.ALERT_CATEGORY, "warning"), (nr.ALERT_CATEGORY, "info"),
        (nr.STATE_CATEGORY, "warning"), (nr.STATE_CATEGORY, "info")]
    assert inbox.label() == "정상"


def test_kst_day_boundary_resets_ordinal_and_counts_by_kst_not_utc(inbox):
    # KST 08:30 = UTC 전날 23:30. UTC 일 경계로 세면 이 해소가 "오늘"에서 빠진다.
    t0 = datetime(2026, 10, 22, 7, 0, tzinfo=KST)
    for off, on in ((0, True), (90, False), (180, True)):
        _noresp(inbox, on)
        inbox.run(t0 + timedelta(minutes=off))
    assert inbox.alerts[1]["at"].astimezone(KST).hour == 8
    assert "재발 1회" in inbox.alerts[2]["title"]       # 해소(08:30 KST)가 오늘로 집계됨
    # 장애가 자정을 넘기면 새 날짜 1회차 경보 → 해소도 1회차
    late = datetime(2026, 10, 22, 23, 50, tzinfo=KST)
    _noresp(inbox, True)
    assert inbox.run(late + timedelta(minutes=20))["action"] == "fire_sent"
    assert "2026-10-23" in inbox.alerts[-1]["title"] and "재발" not in inbox.alerts[-1]["title"]
    _noresp(inbox, False)
    assert inbox.run(late + timedelta(minutes=50))["action"] == "resolve_sent"
    assert inbox.alerts[-1]["title"].startswith("[해소]") and "회차" not in inbox.alerts[-1]["title"]


def test_state_lives_only_in_alert_history_not_in_module_memory(inbox, monkeypatch):
    """프로세스 재시작을 흉내낸다 — 모듈을 다시 읽어도 알림함만으로 상태가 복구된다."""
    import importlib

    _step(inbox, 0, True)
    _step(inbox, 30, False)
    _step(inbox, 60, True)                              # 두 번째 장애 진행 중
    importlib.reload(nr)
    inbox.install(monkeypatch)                          # 재적재된 모듈에 같은 알림함을 다시 연결
    assert _step(inbox, 90, False) == "resolve_sent"
    assert _step(inbox, 120, False) == "none"
    assert [a["severity"] for a in inbox.alerts] == ["warning", "info", "warning", "info"]


def test_check_sql_window_follows_config():
    assert "INTERVAL '7 days'" in nr.check_sql(7)
    assert "INTERVAL '14 days'" in nr.check_sql(14)
    cfg = nr.AlertConfig(window_days=14)
    s = nr.compute_noresp_rate(_rows(5, 200, AT), NOW, cfg.window_days)
    msg = nr.build_alert_message(s, cfg)
    assert "INTERVAL '14 days'" in msg and "INTERVAL '7 days'" not in msg


def test_zero_noresp_rows_never_alert(inbox):
    inbox.rows = _rows(0, 5000, AT)
    assert inbox.run()["action"] == "none"
    assert inbox.alerts == []


def test_send_without_notify_falls_through_to_failed_when_no_row(monkeypatch):
    async def always_false(*a, **k):
        return False

    async def nothing(pool, title, minutes):
        return False

    monkeypatch.setattr(ohvis_alert, "notify", always_false)
    monkeypatch.setattr(nr, "_alert_exists", nothing)
    assert asyncio.run(nr._send(object(), "t", "m", "warning")) == "failed"


def test_lifecycle_label_distinguishes_real_and_suppressed_incident():
    t = NOW
    assert nr.lifecycle_label(None) == "정상"
    assert nr.lifecycle_label(nr.LifecycleState(nr.ALERT_CATEGORY, "info", t)) == "정상"
    assert nr.lifecycle_label(nr.LifecycleState(nr.STATE_CATEGORY, "info", t)) == "정상"
    assert nr.lifecycle_label(nr.LifecycleState(nr.ALERT_CATEGORY, "critical", t)) == "장애 중 (경보 발송됨)"
    suppressed = nr.lifecycle_label(nr.LifecycleState(nr.STATE_CATEGORY, "warning", t))
    assert suppressed.startswith("장애 중") and "억제" in suppressed and "미통지" in suppressed


def test_record_state_writes_acknowledged_row_in_state_category_and_swallows_failure():
    calls = []

    class Pool:
        fail = False

        async def execute(self, sql, *args):
            if self.fail:
                raise RuntimeError("db down")
            calls.append((sql, args))

    pool = Pool()
    assert asyncio.run(nr._record_state(pool, "t", "m", "warning")) is True
    sql, args = calls[0]
    assert "INSERT INTO alert_history" in sql and "acknowledged" in sql and "true" in sql
    assert args[0] == "warning" and args[1] == nr.STATE_CATEGORY
    pool.fail = True
    assert asyncio.run(nr._record_state(pool, "t", "m", "warning")) is False


# ── 시험 발송 구분 ──

def test_test_alert_is_distinct_from_real_alert(monkeypatch):
    sent = []

    async def fake_notify(title, message="", **kw):
        sent.append((title, kw))
        return True

    monkeypatch.setattr(ohvis_alert, "notify", fake_notify)
    assert asyncio.run(nr.send_test_alert(NOW)) is True
    title, kw = sent[0]
    assert title.startswith("[시험]")
    assert kw["category"] == "review_noresp_rate_test" != nr.ALERT_CATEGORY
    assert kw["severity"] == ohvis_alert.INFO
    # 상태 판정은 실제 경보·미발송 기록 category 만 본다
    assert kw["category"] not in nr.STATE_CATEGORIES
    assert "category = ANY($1::text[])" in inspect.getsource(nr._latest_state)
    assert "category = $1" in inspect.getsource(nr._delivered_today)


# ── 설정·스케줄 ──

def test_config_env_overrides_and_invalid_fallback(monkeypatch):
    monkeypatch.setenv("REVIEW_NORESP_ALERT_PCT", "4.5")
    monkeypatch.setenv("REVIEW_NORESP_ALERT_MIN_SAMPLES", "abc")
    monkeypatch.setenv("REVIEW_NORESP_ALERT_WINDOW_DAYS", "-3")
    cfg = nr.load_config()
    assert (cfg.threshold_pct, cfg.min_samples, cfg.window_days) == (4.5, 100, 7)
    monkeypatch.setenv("REVIEW_NORESP_ALERT_PCT", "12")  # 임계가 CRITICAL 보다 커지면 CRITICAL 도 끌어올림
    assert nr.load_config().critical_pct == 12.0


class _FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, **kw):
        self.jobs.append((func, trigger, kw))


def test_scheduler_registration_and_kill_switch(monkeypatch):
    sch = _FakeScheduler()
    monkeypatch.delenv("REVIEW_NORESP_ALERT_ENABLED", raising=False)
    assert nr.register_scheduler_job(sch) is True
    _, trigger, kw = sch.jobs[0]
    assert (trigger, kw["minutes"], kw["id"], kw["max_instances"]) == (
        "interval", 30, "review_noresp_alert", 1)
    off = _FakeScheduler()
    monkeypatch.setenv("REVIEW_NORESP_ALERT_ENABLED", "0")
    assert nr.register_scheduler_job(off) is False
    assert off.jobs == []


def test_job_wrapper_swallows_errors(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(nr, "evaluate_and_notify", boom)
    asyncio.run(nr.run_review_noresp_alert())  # 예외가 스케줄러로 새지 않는다


# ── 60일 이력 백테스트 (지시서 2026-10-22 실측 요약을 고정 픽스처로) ──
# 일별 값이 지시서에 있는 날은 그대로, 구간 서술("약 16~21%", "0.7~1.1%")만 있는 날은
# 구간 안의 대표 (분자, 분모) 를 둔다. 분모가 명시되지 않은 날은 600/1000 으로 가정했다.
# 9/14~9/20, 9/23~9/27, 10/13, 10/15~10/17 은 일별 값이 없어 양 끝점 사이 선형 보간(가정).
# 이 날들은 양 끝점보다 낮아질 수 없는 단조 구간이라 발화 판정에는 영향이 없다.

def _lerp(d0, r0, d1, r1, den=1000):
    n = (d1 - d0).days
    return {
        d0 + timedelta(days=i): (round(den * (r0 + (r1 - r0) * i / n) / 100), den)
        for i in range(1, n)
    }


def _d(m, d):
    return datetime(2026, m, d).date()


def _build_history():
    h = {}
    for day, num in zip(range(23, 28), (100, 105, 110, 115, 126)):
        h[_d(8, day)] = (num, 600)                                  # 16.7~21%
    for i in range((_d(9, 11) - _d(8, 31)).days + 1):
        h[_d(8, 31) + timedelta(days=i)] = (0, 600)                  # 0%
    h[_d(9, 12)] = (3, 600)                                          # 0.5%
    h[_d(9, 13)] = (81, 1000)                                        # 8.1%
    h.update(_lerp(_d(9, 13), 8.1, _d(9, 21), 31.0))
    h[_d(9, 21)] = (310, 1000)
    h[_d(9, 22)] = (645, 1848)                                       # 34.9%
    h.update(_lerp(_d(9, 22), 34.9, _d(9, 28), 19.8))
    h[_d(9, 28)] = (198, 1000)                                       # 19.8%
    for day, (n, d) in zip(
        [_d(9, 29), _d(9, 30), _d(10, 1), _d(10, 2), _d(10, 3), _d(10, 4), _d(10, 5), _d(10, 6)],
        [(4, 550), (5, 550), (6, 550), (5, 500), (4, 500), (3, 430), (4, 450), (6, 545)],
    ):
        h[day] = (n, d)                                              # 0.7~1.1%, 3~6건
    for day in range(7, 12):
        h[_d(10, day)] = (0, 600)
    h[_d(10, 12)] = (27, 542)                                        # 5.0%
    h.update(_lerp(_d(10, 12), 5.0, _d(10, 14), 5.9, den=600))
    h[_d(10, 14)] = (37, 629)                                        # 5.9%
    h.update(_lerp(_d(10, 14), 5.9, _d(10, 18), 4.9, den=700))
    h[_d(10, 18)] = (37, 757)                                        # 4.9%
    h[_d(10, 19)] = (10, 654)                                        # 1.5%
    h[_d(10, 20)] = (8, 650)                                         # 1.2%
    h[_d(10, 21)] = (0, 626)
    h[_d(10, 22)] = (0, 626)
    return h


HISTORY = _build_history()


def _expected_fire(day):
    if day in (_d(8, d) for d in range(23, 28)):
        return True
    if _d(9, 13) <= day <= _d(9, 28):
        return True
    if _d(10, 12) <= day <= _d(10, 18):
        return True
    return False


def test_backtest_fixture_matches_stated_history():
    wrong = []
    for day, (num, den) in sorted(HISTORY.items()):
        fired = nr.decide(num, den, CFG) in (nr.DECISION_WARNING, nr.DECISION_CRITICAL)
        if fired != _expected_fire(day):
            wrong.append((day.isoformat(), num, den, round(100 * num / den, 2)))
    assert wrong == []


def test_backtest_default_threshold_has_margin_both_sides():
    fired = [100 * n / d for day, (n, d) in HISTORY.items() if _expected_fire(day)]
    quiet = [100 * n / d for day, (n, d) in HISTORY.items() if not _expected_fire(day)]
    assert min(fired) - CFG.threshold_pct >= 1.5     # 가장 낮은 장애일 4.9%
    assert CFG.threshold_pct - max(quiet) >= 1.0     # 가장 높은 비발화일 1.5%


@pytest.mark.parametrize("day", sorted(d for d in HISTORY if d.day % 3 == 0 or d in (
    _d(8, 23), _d(9, 13), _d(9, 22), _d(9, 28), _d(10, 12), _d(10, 18), _d(10, 19), _d(10, 22))))
def test_replay_through_row_pipeline_matches_fixture(day):
    """집계 수치를 실제 행으로 풀어 compute→decide 전 경로를 KST 일 경계로 재생."""
    num, den = HISTORY[day]
    noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=KST)
    rows = _rows(num, den, noon)
    end = datetime(day.year, day.month, day.day, tzinfo=KST) + timedelta(days=1)
    out = nr.replay_backtest(rows, end, days=1, cfg=CFG)[0]
    assert out["date"] == day.isoformat()
    assert out["fired"] == _expected_fire(day)
    assert out["false_positive"] is False
