"""M5 — 일일 정본 등록률 계측: 일자·중복집계·분모 0·tenant 격리·누락 의미·멱등.

지표 A(문서 단위 등록률)와 B(게이트 ok 비율)는 섞지 않는다. 수집하지 않은 날짜는 0% 도 성공도 아니다.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


from app.services import rdoc_registration_metrics as m

_REPO = Path(__file__).resolve().parents[2]
T1 = "2d701a8c-0000-4000-8000-000000000001"
T2 = "99999999-0000-4000-8000-000000000002"
D = dt.date(2026, 10, 19)
NOW = dt.datetime(2026, 10, 20, 0, 20, tzinfo=m.KST)  # 19일이 끝난 직후


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def epoch(y, mo, d, h, mi, s=0) -> float:
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=m.KST).timestamp()


def f(path, ep, text="본문", head=None, size=None):
    return {"path": path, "added_epoch": ep, "added_sha256": sha(text),
            "head_sha256": sha(head) if head else None, "size": len(text) if size is None else size}


def rev(path, text, tenant=T1, project="AADS"):
    return {"tenant_id": tenant, "project_key": project, "source_path": path, "content_hash": sha(text)}


def a_rows(files, revisions, start=D, end=D, projects=("AADS",), now=NOW, verified=True, tenant=T1, known=("AADS", "GO100")):
    docs, excluded = m.classify_new_files(files, "AADS", known)
    return m.build_daily_a(
        docs=docs, excluded=excluded, revisions=revisions, tenant_id=tenant, projects=projects,
        start=start, end=end, collected_at=now, source_verified=verified)


def one(rows, day=D, project="AADS"):
    got = [r for r in rows if r["kst_date"] == day and r["project_key"] == project]
    assert len(got) == 1
    return got[0]


# ── 일자(KST 경계) ─────────────────────────────────────────────────────


def test_kst_day_boundaries():
    assert m.kst_date_of(epoch(2026, 10, 19, 23, 59, 59)) == dt.date(2026, 10, 19)
    assert m.kst_date_of(epoch(2026, 10, 20, 0, 0, 0)) == dt.date(2026, 10, 20)
    # UTC 15:00 은 이미 다음 날 KST 00:00 이다
    utc = dt.datetime(2026, 10, 19, 15, 0, tzinfo=dt.timezone.utc).timestamp()
    assert m.kst_date_of(utc) == dt.date(2026, 10, 20)


def test_new_docs_are_counted_on_their_own_kst_date():
    files = [
        f("docs/reports/20261019_AADS_가.md", epoch(2026, 10, 19, 23, 59, 59), "a"),
        f("docs/reports/20261020_AADS_나.md", epoch(2026, 10, 20, 0, 0, 1), "b"),
    ]
    rows = a_rows(files, [rev("docs/reports/20261019_AADS_가.md", "a")], start=D, end=D + dt.timedelta(days=1))
    r19, r20 = one(rows, D), one(rows, D + dt.timedelta(days=1))
    assert (r19["numerator"], r19["denominator"], r19["rate"]) == (1, 1, 1.0)
    assert (r20["numerator"], r20["denominator"], r20["rate"]) == (0, 1, 0.0)


# ── 분모 0 ─────────────────────────────────────────────────────────────


def test_zero_denominator_is_not_applicable_not_zero_percent():
    rows = a_rows([], [])
    r = one(rows)
    assert r["denominator"] == 0 and r["status"] == m.STATUS_NOT_APPLICABLE
    assert r["rate"] is None, "분모 0 을 0% 나 100% 로 만들면 안 된다"


def test_only_excluded_files_leave_denominator_zero():
    r = one(a_rows([f("docs/reports/shot.png", epoch(2026, 10, 19, 9, 0), "png")], []))
    assert r["denominator"] == 0 and r["rate"] is None and r["status"] == m.STATUS_NOT_APPLICABLE
    assert r["excluded_count"] == 1 and r["excluded_reasons"] == {m.EXCL_NON_MARKDOWN: 1}


# ── 중복 집계 ──────────────────────────────────────────────────────────


def test_same_content_copy_counts_once_and_is_listed_as_excluded():
    files = [
        f("docs/reports/20261019_AADS_원본.md", epoch(2026, 10, 19, 9, 0), "same"),
        f("docs/reports/20261019_AADS_사본.md", epoch(2026, 10, 19, 10, 0), "same"),
    ]
    r = one(a_rows(files, [rev("docs/reports/20261019_AADS_원본.md", "same")]))
    assert (r["numerator"], r["denominator"]) == (1, 1)
    assert r["excluded_reasons"] == {m.EXCL_DUP_CONTENT: 1}


def test_readded_path_counts_once_on_first_add_date():
    p = "docs/reports/20261018_AADS_재추가.md"
    files = [f(p, epoch(2026, 10, 18, 9, 0), "v1"), f(p, epoch(2026, 10, 19, 9, 0), "v2")]
    rows = a_rows(files, [], start=dt.date(2026, 10, 18), end=D)
    assert one(rows, dt.date(2026, 10, 18))["denominator"] == 1
    r19 = one(rows, D)
    assert r19["denominator"] == 0 and r19["excluded_reasons"] == {m.EXCL_READD: 1}


def test_empty_file_is_excluded():
    r = one(a_rows([f("docs/reports/20261019_AADS_빈.md", epoch(2026, 10, 19, 9, 0), "", size=0)], []))
    assert r["denominator"] == 0 and r["excluded_reasons"] == {m.EXCL_EMPTY: 1}


# ── 등록 판정(source_path + hash) ──────────────────────────────────────


def test_registered_requires_path_and_hash_match():
    p = "docs/reports/20261019_AADS_보고.md"
    files = [f(p, epoch(2026, 10, 19, 9, 0), "원문")]
    ok = one(a_rows(files, [rev(p, "원문")]))
    assert ok["numerator"] == 1
    # 같은 경로여도 내용이 다르면 등록이 아니다(path_only 로 따로 센다)
    drift = one(a_rows(files, [rev(p, "다른 내용")]))
    assert drift["numerator"] == 0 and drift["detail"]["path_only"] == 1
    # 같은 내용이어도 경로가 다르면 아니다
    other = one(a_rows(files, [rev("docs/reports/다른경로.md", "원문")]))
    assert other["numerator"] == 0 and other["detail"]["path_only"] == 0
    assert other["detail"]["unregistered_paths"] == [p]


def test_head_content_also_matches_when_file_was_edited_after_add():
    p = "docs/reports/20261019_AADS_수정.md"
    files = [f(p, epoch(2026, 10, 19, 9, 0), "초안", head="최종")]
    assert one(a_rows(files, [rev(p, "최종")]))["numerator"] == 1


# ── tenant / project 격리 ──────────────────────────────────────────────


def test_other_tenant_or_project_revision_does_not_count():
    p = "docs/reports/20261019_AADS_격리.md"
    files = [f(p, epoch(2026, 10, 19, 9, 0), "x")]
    assert one(a_rows(files, [rev(p, "x", tenant=T2)]))["numerator"] == 0
    assert one(a_rows(files, [rev(p, "x", project="GO100")]))["numerator"] == 0
    # 자기 tenant 의 등록은 그 tenant 행에서만 센다
    mine = one(a_rows(files, [rev(p, "x", tenant=T2)], tenant=T2))
    assert mine["tenant_id"] == T2 and mine["numerator"] == 1
    assert one(a_rows(files, [rev(p, "x", tenant=T1)], tenant=T2))["numerator"] == 0


def test_rows_carry_the_requested_tenant_only():
    rows = a_rows([], [rev("docs/reports/a.md", "x", tenant=T2)], tenant=T1)
    assert {r["tenant_id"] for r in rows} == {T1}


def test_project_follows_filename_only_when_known():
    files = [
        f("docs/reports/20261019_GO100_분석.md", epoch(2026, 10, 19, 9, 0), "g"),
        f("docs/reports/20261019_CHAT_기록.md", epoch(2026, 10, 19, 9, 5), "c"),
    ]
    rows = a_rows(files, [rev("docs/reports/20261019_GO100_분석.md", "g", project="GO100")],
                  projects=("AADS", "GO100"))
    assert one(rows, project="GO100")["numerator"] == 1
    r = one(rows, project="AADS")
    assert r["denominator"] == 1, "알려지지 않은 토큰(CHAT)은 저장소 기본 프로젝트로 본다"


# ── 멱등 ───────────────────────────────────────────────────────────────


def test_same_input_gives_same_rows_with_unique_keys():
    files = [f("docs/reports/20261019_AADS_a.md", epoch(2026, 10, 19, 9, 0), "a")]
    first = a_rows(files, [])
    second = a_rows(files, [])
    assert first == second
    keys = [(r["tenant_id"], r["project_key"], r["kst_date"], r["metric"]) for r in first + second[:0]]
    assert len(keys) == len(set(keys))


class _Conn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def test_upsert_is_keyed_and_never_downgrades_on_time_rows():
    rows = a_rows([], [])
    conn = _Conn()
    assert asyncio.run(m.upsert_rows(conn, rows)) == 1
    sql, args = conn.calls[0]
    assert "ON CONFLICT (tenant_id, project_key, kst_date, metric) DO UPDATE" in sql
    assert "first_collected_at" in sql.split("DO UPDATE")[0] and "first_collected_at =" not in sql
    assert "quality <> 'on_time' OR EXCLUDED.quality = 'on_time'" in sql
    assert args[3] == m.METRIC_A and args[8] is None, "N/A 는 NULL 로 저장"
    assert json.loads(args[7]) == {}


# ── 지표 B ─────────────────────────────────────────────────────────────


def _ev(day, hour, verdict, entry="runner_submit", project="AADS"):
    return {"occurred_at": dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=m.KST),
            "entrypoint": entry, "project": project, "verdict": verdict}


def test_b_is_separate_metric_and_counts_unknown_in_denominator():
    ev = [_ev(D, 9, "ok"), _ev(D, 10, "ok", "chat_tool"), _ev(D, 11, "missing_canonical"),
          _ev(D, 12, "unknown", "commit"), _ev(D, 13, "ok", project="GO100")]
    r = m.build_daily_b(events=ev, tenant_id=T1, project="AADS", start=D, end=D, collected_at=NOW)[0]
    assert r["metric"] == m.METRIC_B != m.METRIC_A
    assert (r["numerator"], r["denominator"], r["rate"]) == (2, 4, 0.5)
    assert r["detail"]["by_entrypoint"]["commit"] == {"unknown": 1}
    assert "project_only" in r["detail"]["tenant_scope"]


def test_b_zero_events_is_not_applicable():
    r = m.build_daily_b(events=[], tenant_id=T1, project="AADS", start=D, end=D, collected_at=NOW)[0]
    assert r["rate"] is None and r["status"] == m.STATUS_NOT_APPLICABLE


def test_a_and_b_do_not_share_numbers():
    files = [f("docs/reports/20261019_AADS_a.md", epoch(2026, 10, 19, 9, 0), "a")]
    a = one(a_rows(files, []))
    b = m.build_daily_b(events=[_ev(D, 9, "ok")], tenant_id=T1, project="AADS", start=D, end=D, collected_at=NOW)[0]
    assert (a["numerator"], a["denominator"]) == (0, 1) and (b["numerator"], b["denominator"]) == (1, 1)


# ── 수집 품질과 누락(서비스가 돌지 않은 날) ─────────────────────────────


def test_collection_quality_rules():
    day = D
    q = m.collection_quality
    assert q(day, dt.datetime(2026, 10, 19, 22, 0, tzinfo=m.KST), True) == m.QUALITY_PARTIAL
    assert q(day, dt.datetime(2026, 10, 20, 0, 20, tzinfo=m.KST), True) == m.QUALITY_ON_TIME
    assert q(day, dt.datetime(2026, 10, 20, 23, 59, tzinfo=m.KST), True) == m.QUALITY_ON_TIME
    assert q(day, dt.datetime(2026, 10, 21, 0, 0, tzinfo=m.KST), True) == m.QUALITY_LATE
    assert q(day, dt.datetime(2026, 10, 20, 0, 20, tzinfo=m.KST), False) == m.QUALITY_UNVERIFIED


def _stored(day, quality, metric=m.METRIC_A, tenant=T1, project="AADS", rate=1.0, status="measured"):
    return {"tenant_id": tenant, "project_key": project, "kst_date": day, "metric": metric,
            "numerator": 1, "denominator": 1, "rate": rate, "status": status, "quality": quality}


def test_no_service_run_means_not_collected_never_zero_percent():
    rep = m.coverage_report([], tenant_id=T1, project="AADS", metric=m.METRIC_A,
                            start=D - dt.timedelta(days=6), end=D)
    assert rep["not_collected_dates"] == m.date_range(D - dt.timedelta(days=6), D)
    assert all(d["state"] == m.STATE_NOT_COLLECTED and d["rate"] is None for d in rep["days"])
    assert rep["consecutive_on_time_days"] == 0 and rep["ready_for_m5"] is False


def test_gap_breaks_the_consecutive_run():
    days = m.date_range(D - dt.timedelta(days=6), D)
    rows = [_stored(d, m.QUALITY_ON_TIME) for d in days if d != D - dt.timedelta(days=3)]
    rep = m.coverage_report(rows, tenant_id=T1, project="AADS", metric=m.METRIC_A, start=days[0], end=D)
    assert rep["not_collected_dates"] == [D - dt.timedelta(days=3)]
    assert rep["consecutive_on_time_days"] == 3 and rep["ready_for_m5"] is False


def test_seven_on_time_days_are_required_and_other_qualities_do_not_count():
    days = m.date_range(D - dt.timedelta(days=6), D)
    full = [_stored(d, m.QUALITY_ON_TIME) for d in days]
    assert m.coverage_report(full, tenant_id=T1, project="AADS", metric=m.METRIC_A,
                             start=days[0], end=D)["ready_for_m5"] is True
    for bad in (m.QUALITY_LATE, m.QUALITY_PARTIAL, m.QUALITY_UNVERIFIED):
        rows = [_stored(d, bad if d == days[0] else m.QUALITY_ON_TIME) for d in days]
        rep = m.coverage_report(rows, tenant_id=T1, project="AADS", metric=m.METRIC_A, start=days[0], end=D)
        assert rep["ready_for_m5"] is False and rep["consecutive_on_time_days"] == 6, bad


def test_coverage_ignores_other_tenant_and_other_metric():
    rows = [_stored(D, m.QUALITY_ON_TIME, tenant=T2), _stored(D, m.QUALITY_ON_TIME, metric=m.METRIC_B)]
    rep = m.coverage_report(rows, tenant_id=T1, project="AADS", metric=m.METRIC_A, start=D, end=D)
    assert rep["not_collected_dates"] == [D]


# ── git 수집(실제 임시 저장소) ──────────────────────────────────────────


def _g(cwd, *a, when=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.com")
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, timeout=60)


def test_collect_new_reports_from_real_git_history(tmp_path):
    _g(tmp_path, "init", "-q", "-b", "main")
    rep = tmp_path / "docs" / "reports"
    rep.mkdir(parents=True)
    (rep / "20261019_AADS_신규.md").write_text("초안", encoding="utf-8")
    (rep / "20261019_AADS_삭제될것.md").write_text("temp", encoding="utf-8")
    (rep / "old.md").write_text("old", encoding="utf-8")
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-q", "-m", "add", when="2026-10-19T23:30:00+09:00")
    (rep / "20261019_AADS_신규.md").write_text("최종", encoding="utf-8")
    (rep / "20261019_AADS_삭제될것.md").unlink()
    (rep / "old.md").rename(rep / "renamed.md")
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-q", "-m", "edit", when="2026-10-20T09:00:00+09:00")

    files = {x["path"]: x for x in m.collect_new_reports(str(tmp_path), "HEAD", dt.date(2026, 10, 19))}
    assert set(files) == {"docs/reports/20261019_AADS_신규.md", "docs/reports/20261019_AADS_삭제될것.md",
                          "docs/reports/old.md"}, "이름 바꿈(renamed.md)은 신규가 아니다"
    new = files["docs/reports/20261019_AADS_신규.md"]
    assert m.kst_date_of(new["added_epoch"]) == D
    assert new["added_sha256"] == sha("초안") and new["head_sha256"] == sha("최종")
    assert files["docs/reports/20261019_AADS_삭제될것.md"]["head_sha256"] is None
    # 이전 날짜 창이면 아무것도 안 잡힌다
    assert m.collect_new_reports(str(tmp_path), "HEAD", dt.date(2026, 10, 21)) == []


def _commit_all(tmp_path, msg, when):
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-q", "-m", msg, when=when)


def _a_over_git(repo, since, start, end, ref="HEAD", known=("AADS",)):
    """수집기가 하는 그대로: git 이력 → 분류 → 일별 A 행."""
    files = m.collect_report_history(str(repo), ref, since)
    docs, excluded = m.classify_new_files(files, "AADS", known, since=since)
    rows = m.build_daily_a(
        docs=docs, excluded=excluded, revisions=[], tenant_id=T1, projects=["AADS"],
        start=start, end=end, collected_at=NOW, source_verified=True)
    return {r["kst_date"]: r for r in rows}


def test_readded_path_after_delete_is_not_new_when_window_starts_after_first_add(tmp_path):
    """최초추가 10-01 → 삭제 10-18 → 같은 경로 재추가 10-19. since=10-19 창에서도 신규가 아니다."""
    _g(tmp_path, "init", "-q", "-b", "main")
    rep = tmp_path / "docs" / "reports"
    rep.mkdir(parents=True)
    p = rep / "20261001_AADS_재추가.md"
    p.write_text("v1", encoding="utf-8")
    _commit_all(tmp_path, "first", "2026-10-01T10:00:00+09:00")
    p.unlink()
    _commit_all(tmp_path, "delete", "2026-10-18T10:00:00+09:00")
    p.write_text("v2", encoding="utf-8")
    _commit_all(tmp_path, "readd", "2026-10-19T10:00:00+09:00")

    r = _a_over_git(tmp_path, D, D, D)[D]
    assert r["denominator"] == 0 and r["status"] == m.STATUS_NOT_APPLICABLE
    assert r["excluded_reasons"] == {m.EXCL_READD: 1}


def test_same_day_result_is_identical_for_one_day_and_multi_day_windows(tmp_path):
    _g(tmp_path, "init", "-q", "-b", "main")
    rep = tmp_path / "docs" / "reports"
    rep.mkdir(parents=True)
    old, readd = rep / "20261001_AADS_옛문서.md", rep / "20261019_AADS_재추가.md"
    fresh, copy = rep / "20261019_AADS_진짜신규.md", rep / "20261019_AADS_사본.md"
    old.write_text("옛 본문", encoding="utf-8")
    readd.write_text("재추가 v1", encoding="utf-8")
    _commit_all(tmp_path, "first", "2026-10-01T10:00:00+09:00")
    readd.unlink()
    _commit_all(tmp_path, "delete", "2026-10-18T10:00:00+09:00")
    readd.write_text("재추가 v2", encoding="utf-8")
    fresh.write_text("신규 본문", encoding="utf-8")
    copy.write_text("옛 본문", encoding="utf-8")  # 과거 문서와 동일 내용 사본
    _commit_all(tmp_path, "d19", "2026-10-19T10:00:00+09:00")

    one_day = _a_over_git(tmp_path, D, D, D)[D]
    multi = _a_over_git(tmp_path, dt.date(2026, 10, 1), dt.date(2026, 10, 1), D)
    for key in ("denominator", "excluded_count", "excluded_reasons"):
        assert one_day[key] == multi[D][key], key
    assert one_day["denominator"] == 1
    assert one_day["excluded_reasons"] == {m.EXCL_READD: 1, m.EXCL_DUP_CONTENT: 1}
    # 과거 날짜의 분모도 창 시작과 무관하게 같다
    assert multi[dt.date(2026, 10, 1)]["denominator"] == 2
    later = _a_over_git(tmp_path, dt.date(2026, 10, 10), dt.date(2026, 10, 10), D)
    assert later[D]["excluded_reasons"] == one_day["excluded_reasons"]


def test_history_does_not_spawn_git_show_per_file(tmp_path, monkeypatch):
    _g(tmp_path, "init", "-q", "-b", "main")
    rep = tmp_path / "docs" / "reports"
    rep.mkdir(parents=True)
    for i in range(12):
        (rep / f"20261001_AADS_문서{i}.md").write_text(f"본문{i}", encoding="utf-8")
    _commit_all(tmp_path, "many", "2026-10-01T10:00:00+09:00")
    calls = []
    real = m._git
    monkeypatch.setattr(m, "_git", lambda repo, *a, **k: (calls.append(a[0]), real(repo, *a, **k))[1])
    m.collect_report_history(str(tmp_path), "HEAD", dt.date(2026, 10, 1))
    assert "show" not in calls and len(calls) <= 5, calls


def test_shallow_clone_fails_closed(tmp_path):
    """얕은 clone 은 최초추가를 알 수 없다 — 신규로 오인하지 말고 실패한다."""
    src = tmp_path / "src"
    src.mkdir()
    _g(src, "init", "-q", "-b", "main")
    (src / "docs" / "reports").mkdir(parents=True)
    (src / "docs" / "reports" / "a.md").write_text("a", encoding="utf-8")
    _commit_all(src, "c1", "2026-10-01T10:00:00+09:00")
    (src / "docs" / "reports" / "b.md").write_text("b", encoding="utf-8")
    _commit_all(src, "c2", "2026-10-19T10:00:00+09:00")
    dst = tmp_path / "dst"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{src}", str(dst)], check=True,
                   capture_output=True, timeout=60)
    try:
        m.collect_report_history(str(dst), "HEAD", D)
    except RuntimeError as exc:
        assert "shallow" in str(exc)
    else:
        raise AssertionError("shallow 저장소에서 통과했다")


# ── 수집기 스크립트: DB 에 닿지 못하면 행도 성공도 만들지 않는다 ─────────


def test_collector_script_without_db_emits_error_and_no_rows():
    env = {**os.environ, "PGHOST": "127.0.0.1", "PGPORT": "1", "PGDATABASE": "x", "PGUSER": "x"}
    env.pop("DATABASE_URL", None)
    res = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "rdoc_registration_collect.py"), "collect", "--apply"],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 1
    out = json.loads(res.stdout)
    assert "error" in out and "rows" not in out


def test_collector_defaults_to_dry_run():
    src = (_REPO / "scripts" / "rdoc_registration_collect.py").read_text(encoding="utf-8")
    assert 'action="store_true", help="rdoc_registration_daily 에 upsert (기본: 쓰지 않음)"' in src
    assert 'result["written"] = await m.upsert_rows' in src
    # 쓰기 연결은 --apply 분기 안에서만 연다
    assert src.index("if args.apply:") < src.index("_connect(read_only=False)")


def test_read_queries_are_tenant_scoped():
    for sql in (m.KNOWN_PROJECTS_SQL, m.REVISIONS_BY_PATH_SQL, m.DAILY_ROWS_SQL):
        assert "tenant_id = $1::uuid" in sql


# ── 운영 설치는 별도 승인: 자동 적용 경로에 두지 않는다 ─────────────────


def test_schema_and_units_are_staged_outside_auto_apply_paths():
    assert not list((_REPO / "migrations").glob("*rdoc_registration_daily*"))
    assert not list((_REPO / "migrations" / "rollback").glob("*rdoc_registration_daily*"))
    up = (_REPO / "scripts" / "pending_migrations" / "20261020_rdoc_registration_daily.sql").read_text(encoding="utf-8")
    down = (_REPO / "scripts" / "pending_migrations" / "20261020_rdoc_registration_daily.down.sql").read_text(encoding="utf-8")
    assert "PRIMARY KEY (tenant_id, project_key, kst_date, metric)" in up
    assert "(status = 'not_applicable') = (denominator = 0)" in up and "(rate IS NULL) = (denominator = 0)" in up
    assert "DROP TABLE IF EXISTS rdoc_registration_daily" in down
    timer = (_REPO / "scripts" / "pending_units" / "aads-rdoc-registration-daily.timer").read_text(encoding="utf-8")
    assert "Persistent=false" in timer, "꺼져 있던 날을 몰아 수집해 on_time 으로 위장하면 안 된다"
    service = (_REPO / "scripts" / "pending_units" / "aads-rdoc-registration-daily.service").read_text(encoding="utf-8")
    assert "--fetch --apply" in service
