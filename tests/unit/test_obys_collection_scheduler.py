"""클로브 수집 1시간 스케줄러: 회사별 격리, 백오프·재인증 건너뛰기, 기간, 래퍼 판별, unit 경로, lease 충돌.

실 DB·실 클로브 호출은 없다. DB 와 run_collection 은 모킹하고, 래퍼는 가짜 docker 로 돌린다.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import db_pool
from app.core import obys_runtime
from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_core as core
from app.services import obys_collection_scheduler as sched
from app.services import obys_collection_service as svc

ROOT = Path(__file__).parents[2]
WRAPPER = ROOT / "deploy/acct/obys_clobe_collect_tick.sh"
SERVICE = ROOT / "deploy/acct/systemd/obys-clobe-collect.service"
TIMER = ROOT / "deploy/acct/systemd/obys-clobe-collect.timer"
INSTALLER = ROOT / "scripts/install_obys_clobe_timer_cafe24.sh"

NOW = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)  # KST 2026-10-12 12:00
TODAY = date(2026, 10, 12)


def run(coro):
    return asyncio.run(coro)


def _ok_result(company_id, kinds):
    return {"company_id": company_id, "status": "succeeded",
            "runs": [{"data_kind": k, "status": "succeeded", "error_code": None} for k in kinds]}


@pytest.fixture
def world(monkeypatch):
    """links/states 를 채우고 run_collection 호출을 기록하는 가짜 세계."""
    w = type("W", (), {})()
    w.links, w.states, w.calls = [], {}, []
    w.behavior = {}  # company_id -> callable(kinds) | Exception
    w.reauth = False

    async def load_state():
        return [{"clobe_company_id": c} for c in w.links], w.states

    async def run_collection(company_id, **kw):
        w.calls.append({"company_id": company_id, **kw})
        b = w.behavior.get(company_id)
        if isinstance(b, Exception):
            raise b
        if callable(b):
            return b(kw["kinds"])
        return _ok_result(company_id, kw["kinds"])

    async def get_status():
        return {"status": "reauth_required" if w.reauth else "connected", "reauth_required": w.reauth}

    monkeypatch.setattr(sched, "load_state", load_state)
    monkeypatch.setattr(svc, "run_collection", run_collection)
    monkeypatch.setattr(clobe, "get_status", get_status)
    return w


# (a) 링크 3개 중 1개가 예외여도 나머지 2개는 실행된다
def test_one_company_exception_does_not_block_others(world):
    world.links = ["c1", "c2", "c3"]
    world.behavior["c2"] = RuntimeError("boom secret-token")
    out = run(sched.run_due(NOW))
    assert [c["company_id"] for c in world.calls] == ["c1", "c2", "c3"]
    by = {c["company_id"]: c for c in out["companies"]}
    assert by["c1"]["status"] == by["c3"]["status"] == "succeeded"
    assert by["c2"]["status"] == "error"
    assert by["c2"]["detail"] == "RuntimeError"
    assert "secret-token" not in json.dumps(out)
    assert out["ok"] is False


def test_all_succeeded_is_ok_and_uses_scheduler_instance(world):
    world.links = ["c1"]
    out = run(sched.run_due(NOW))
    assert out["ok"] is True
    call = world.calls[0]
    assert call["mode"] == "live"
    assert call["instance"].startswith("obys-scheduler:")
    assert call["instance"] != svc.DEFAULT_INSTANCE


# (b) next_retry_at 이 미래인 종류만 건너뛴다
def test_future_next_retry_kind_skipped_others_run(world):
    world.links = ["c1"]
    world.states = {"c1": {core.KIND_TAX: {"status": "partial", "next_retry_at": NOW + timedelta(minutes=10),
                                            "last_success_at": None, "covered_to": None}}}
    run(sched.run_due(NOW))
    kinds = [k for c in world.calls for k in c["kinds"]]
    assert core.KIND_TAX not in kinds
    assert sorted(kinds) == sorted(set(core.COLLECTION_KINDS) - {core.KIND_TAX})


def test_past_next_retry_runs_and_all_deferred_skips_company(world):
    world.links = ["c1", "c2"]
    past = {"status": "partial", "next_retry_at": NOW - timedelta(seconds=1), "last_success_at": None, "covered_to": None}
    future = {**past, "next_retry_at": NOW + timedelta(hours=1)}
    world.states = {"c1": {k: past for k in core.COLLECTION_KINDS}, "c2": {k: future for k in core.COLLECTION_KINDS}}
    out = run(sched.run_due(NOW))
    assert {c["company_id"] for c in world.calls} == {"c1"}
    assert {c["company_id"]: c["status"] for c in out["companies"]}["c2"] == "skipped_backoff"
    assert out["ok"] is True


# (c) needs_reauth: 클로브 get_status 가 실제로 재인증 필요일 때만 건너뛴다
def test_needs_reauth_skipped_only_when_status_confirms(world):
    world.links = ["c1", "c2"]
    flagged = {core.KIND_BANK: {"status": "needs_reauth", "next_retry_at": None, "last_success_at": None, "covered_to": None}}
    world.states = {"c1": flagged}
    world.reauth = True
    out = run(sched.run_due(NOW))
    by = {c["company_id"]: c["status"] for c in out["companies"]}
    assert by["c1"] == "skipped_needs_reauth"
    assert by["c2"] == "skipped_needs_reauth"  # 연결 단위 재인증이므로 이후 회사도 호출하지 않는다
    assert world.calls == []
    assert out["ok"] is True


def test_needs_reauth_flag_alone_does_not_skip_after_reconnect(world):
    world.links = ["c1"]
    world.states = {"c1": {core.KIND_BANK: {"status": "needs_reauth", "next_retry_at": None,
                                             "last_success_at": None, "covered_to": None}}}
    world.reauth = False  # 사용자가 재인증을 마쳤다
    out = run(sched.run_due(NOW))
    assert len(world.calls) == 1
    assert out["companies"][0]["status"] == "succeeded"


def test_needs_reauth_status_check_failure_does_not_skip(world, monkeypatch):
    async def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(clobe, "get_status", broken)
    world.links = ["c1"]
    world.states = {"c1": {core.KIND_BANK: {"status": "needs_reauth", "next_retry_at": None,
                                             "last_success_at": None, "covered_to": None}}}
    run(sched.run_due(NOW))
    assert len(world.calls) == 1


def test_reauth_hit_during_run_is_reported_once_then_rest_skipped(world):
    world.links = ["c1", "c2"]
    world.behavior["c1"] = lambda kinds: {"company_id": "c1", "status": "needs_reauth", "runs": [],
                                          "error_code": "reauth_required"}
    world.reauth = True
    out = run(sched.run_due(NOW))
    by = {c["company_id"]: c["status"] for c in out["companies"]}
    assert by == {"c1": "needs_reauth", "c2": "skipped_needs_reauth"}
    assert [c["company_id"] for c in world.calls] == ["c1"]
    assert out["ok"] is False


# (d) 기간 계산
def test_period_without_history_uses_core_default_window():
    start, end, clamped = sched.compute_period(None, TODAY)
    assert (start, end) == core.resolve_period(None, TODAY, today=TODAY)
    assert (end - start).days == core.DEFAULT_WINDOW_DAYS and not clamped


def test_period_with_history_starts_three_days_before_covered_to():
    assert sched.compute_period(date(2026, 10, 11), TODAY) == (date(2026, 10, 8), TODAY, False)


def test_period_future_covered_to_never_starts_after_today():
    start, end, _ = sched.compute_period(date(2026, 10, 20), TODAY)
    assert start == end == TODAY


def test_period_clamped_to_max_window():
    start, end, clamped = sched.compute_period(date(2025, 1, 1), TODAY)
    assert clamped and (end - start).days == core.MAX_WINDOW_DAYS
    core.resolve_period(start, end, today=TODAY)  # 서비스가 받아들이는 기간이다


def test_today_is_kst_not_utc(world):
    world.links = ["c1"]
    late_utc = datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)  # KST 10-12 01:00
    out = run(sched.run_due(late_utc))
    assert out["today_kst"] == "2026-10-12"
    assert world.calls[0]["period_end"] == date(2026, 10, 12)


def test_kinds_grouped_by_period_per_kind_history(world):
    world.links = ["c1"]
    hist = lambda d: {"status": "succeeded", "next_retry_at": None, "last_success_at": NOW, "covered_to": d}  # noqa: E731
    world.states = {"c1": {core.KIND_BANK: hist(date(2026, 10, 11)), core.KIND_TAX: hist(date(2026, 10, 11)),
                           core.KIND_CASH: hist(date(2026, 10, 5))}}
    run(sched.run_due(NOW))
    got = {(c["period_start"], tuple(c["kinds"])) for c in world.calls}
    assert (date(2026, 10, 8), (core.KIND_BANK, core.KIND_TAX)) in got
    assert (date(2026, 10, 2), (core.KIND_CASH,)) in got
    assert (date(2026, 7, 14), (core.KIND_CARD, core.KIND_CHANNEL)) in got  # 이력 없음 → 기본 90일(채널매출 포함)


def test_covered_to_ignored_without_a_success(world):
    world.links = ["c1"]
    world.states = {"c1": {core.KIND_BANK: {"status": "failed", "next_retry_at": None,
                                             "last_success_at": None, "covered_to": date(2026, 10, 11)}}}
    run(sched.run_due(NOW))
    assert all(c["period_start"] == date(2026, 7, 14) for c in world.calls)


# (g) lease 충돌 → skipped_busy (실패로 세지 않는다)
def test_lease_conflict_is_skipped_busy_and_not_a_failure(world):
    world.links = ["c1", "c2"]
    world.behavior["c1"] = svc.CollectionError("collection_in_progress", 409)
    out = run(sched.run_due(NOW))
    by = {c["company_id"]: c["status"] for c in out["companies"]}
    assert by == {"c1": "skipped_busy", "c2": "succeeded"}
    assert out["ok"] is True


@pytest.mark.parametrize("code", ["company_blocked", "tenant_not_in_collection_scope", "company_not_linked"])
def test_scope_refusals_are_skips(world, code):
    world.links = ["c1"]
    world.behavior["c1"] = svc.CollectionError(code, 403)
    out = run(sched.run_due(NOW))
    assert out["companies"][0]["status"] == "skipped_scope" and out["ok"] is True


def test_budget_exhaustion_marks_rest_and_fails(world):
    world.links = ["c1", "c2"]
    out = run(sched.run_due(NOW, budget_seconds=0))
    assert world.calls == []
    assert out["budget_exhausted"] is True and out["ok"] is False
    assert {c["status"] for c in out["companies"]} == {"skipped_budget"}


def test_slow_company_is_cut_by_budget(world, monkeypatch):
    world.links = ["c1"]

    async def slow(company_id, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(svc, "run_collection", slow)
    out = run(sched.run_due(NOW, budget_seconds=0.05))
    assert out["companies"][0]["status"] == "error" and out["companies"][0]["detail"] == "timeout"


@pytest.fixture(autouse=True)
def runtime_apply(monkeypatch):
    """CLI 가 부르는 RuntimeSettings.from_env().apply() 를 기록만 하는 가짜로 바꾼다(실 환경변수·볼트 변경 방지)."""
    events: list[str] = []

    class _Settings:
        def apply(self):
            events.append("apply")

    def from_env(env=None):
        events.append("from_env")
        return _Settings()

    monkeypatch.setattr(obys_runtime.RuntimeSettings, "from_env", staticmethod(from_env))
    return events


# (a) CLI 는 run_due 전에 오비서 독립 실행 설정을 적용한다
def test_cli_applies_runtime_settings_before_run_due(world, monkeypatch, runtime_apply):
    order = []

    async def due():
        order.append(tuple(runtime_apply))
        return {"ok": True, "companies": []}

    async def close():
        return None

    monkeypatch.setattr(sched, "run_due", due)
    monkeypatch.setattr(clobe, "close_store_pool", close)
    assert sched.main(["--once"]) == 0
    assert order == [("from_env", "apply")]


# (b) 설정 적용 실패 → JSON 한 줄 + exit 1, 수집 미시작
@pytest.mark.parametrize("stage", ["from_env", "apply"])
def test_cli_runtime_config_failure_is_json_exit_1_without_run_due(world, monkeypatch, capsys, stage):
    called = []

    class _Bad:
        def apply(self):
            raise obys_runtime.RuntimeConfigurationError("postgres://u:secret@h/db")

    def from_env(env=None):
        if stage == "from_env":
            raise obys_runtime.RuntimeConfigurationError("postgres://u:secret@h/db")
        return _Bad()

    async def due():
        called.append("run_due")
        return {"ok": True}

    monkeypatch.setattr(obys_runtime.RuntimeSettings, "from_env", staticmethod(from_env))
    monkeypatch.setattr(sched, "run_due", due)
    assert sched.main(["--once"]) == 1
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    body = json.loads(out)
    assert body["ok"] is False and body["error"] == "runtime_config"
    assert "secret" not in out
    assert called == []


# (c) AADS 공유 풀은 쓰지 않는다
def test_cli_never_touches_aads_shared_pool(world, monkeypatch):
    world.links = ["c1"]
    touched = []

    async def init_pool(*a, **k):
        touched.append("init_pool")

    async def close_pool(*a, **k):
        touched.append("close_pool")

    async def close():
        return None

    monkeypatch.setattr(db_pool, "init_pool", init_pool)
    monkeypatch.setattr(db_pool, "close_pool", close_pool)
    monkeypatch.setattr(clobe, "close_store_pool", close)
    assert sched.main(["--once"]) == 0
    assert touched == []
    src = (ROOT / "app/services/obys_collection_scheduler.py").read_text(encoding="utf-8")
    assert "init_pool" not in src and "close_pool" not in src.replace("close_store_pool", "")


def test_cli_exit_code_and_single_json_line(world, monkeypatch, capsys):
    world.links = ["c1"]
    world.behavior["c1"] = RuntimeError("x")
    closed = []

    async def close():
        closed.append(1)

    monkeypatch.setattr(clobe, "close_store_pool", close)
    assert sched.main(["--once"]) == 1
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1 and json.loads(out)["ok"] is False
    assert closed == [1]
    world.behavior.clear()
    assert sched.main(["--once"]) == 0


def test_cli_requires_once():
    with pytest.raises(SystemExit):
        sched.main([])


def test_cli_load_failure_is_json_and_exit_1(monkeypatch, capsys):
    async def boom():
        raise ConnectionError("db")

    async def close():
        return None

    monkeypatch.setattr(sched, "load_state", boom)
    monkeypatch.setattr(clobe, "close_store_pool", close)
    assert sched.main(["--once"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "ConnectionError"


def test_backfill_runs_each_company_in_order_with_the_requested_kind_and_period(world):
    world.links = ["c1", "c2"]
    out = run(sched.run_backfill([core.KIND_CHANNEL], date(2026, 7, 16), date(2026, 10, 16), pause_seconds=0))
    assert [(c["company_id"], c["kinds"], c["period_start"], c["period_end"], c["mode"]) for c in world.calls] == [
        ("c1", [core.KIND_CHANNEL], date(2026, 7, 16), date(2026, 10, 16), "live"),
        ("c2", [core.KIND_CHANNEL], date(2026, 7, 16), date(2026, 10, 16), "live")]
    assert out["ok"] is True and out["companies"] == 2 and out["period"] == ["2026-07-16", "2026-10-16"]


def test_backfill_dry_run_and_company_filter_make_no_calls_or_only_chosen_calls(world):
    world.links = ["c1", "c2", "c3"]
    dry = run(sched.run_backfill([core.KIND_CHANNEL], date(2026, 7, 16), date(2026, 10, 16), dry_run=True))
    assert world.calls == [] and dry["companies"] == 3 and dry["results"] == []
    run(sched.run_backfill([core.KIND_CHANNEL], date(2026, 7, 16), date(2026, 10, 16), company_ids=["c2"], pause_seconds=0))
    assert [c["company_id"] for c in world.calls] == ["c2"]


def test_backfill_isolates_failures_and_busy_lease_and_stops_on_reauth(world):
    world.links = ["c1", "c2", "c3"]
    world.behavior["c1"] = svc.CollectionError("collection_in_progress", 409)
    world.behavior["c2"] = RuntimeError("secret")
    out = run(sched.run_backfill([core.KIND_CHANNEL], date(2026, 10, 1), date(2026, 10, 2), pause_seconds=0))
    assert [r["status"] for r in out["results"]] == ["skipped_busy", "error", "succeeded"] and out["ok"] is False
    assert "secret" not in json.dumps(out)
    world.calls.clear()
    world.behavior = {"c1": lambda kinds: {"company_id": "c1", "status": "needs_reauth", "runs": [], "error_code": "reauth_required"}}
    out = run(sched.run_backfill([core.KIND_CHANNEL], date(2026, 10, 1), date(2026, 10, 2), pause_seconds=0))
    assert [c["company_id"] for c in world.calls] == ["c1"]
    assert [r["status"] for r in out["results"]] == ["needs_reauth", "skipped_needs_reauth", "skipped_needs_reauth"]


def test_backfill_rejects_bad_kind_and_too_long_period(world):
    with pytest.raises(ValueError, match="invalid_kind"):
        run(sched.run_backfill(["bogus"], date(2026, 10, 1), date(2026, 10, 2)))
    with pytest.raises(ValueError, match="period_too_long"):
        run(sched.run_backfill([core.KIND_CHANNEL], date(2025, 1, 1), date(2026, 10, 2)))


def test_cli_backfill_flag_prints_one_json_line_and_default_kind_is_channel(world, monkeypatch, capsys):
    world.links = ["c1"]

    async def close():
        return None

    monkeypatch.setattr(clobe, "close_store_pool", close)
    assert sched.main(["--once", "--backfill-from", "2026-07-16", "--backfill-to", "2026-10-16", "--pause-seconds", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kinds"] == [core.KIND_CHANNEL] and world.calls[0]["period_start"] == date(2026, 7, 16)
    with pytest.raises(SystemExit):
        sched.main(["--once", "--backfill-to", "2026-10-16"])


def test_scheduler_never_confirms_ledger_or_touches_app_startup():
    src = (ROOT / "app/services/obys_collection_scheduler.py").read_text(encoding="utf-8")
    assert "confirm_items" not in src and "review_items" not in src
    for entry in ("app/main.py", "app/yeoljeong_main.py"):
        assert "obys_collection_scheduler" not in (ROOT / entry).read_text(encoding="utf-8")


# (e) 래퍼: 활성 컨테이너 판별
def _fake_env(tmp_path, *, site_text, containers, docker_ps_rc=0):
    """containers: {이름: --port 값}. 가짜 docker 는 호출을 calls.log 에 남긴다."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    site = tmp_path / "fb.conf"
    site.write_text(site_text, encoding="utf-8")
    log = tmp_path / "calls.log"
    ports = "\n".join(f"{n}={p}" for n, p in containers.items())
    docker = bindir / "docker"
    docker.write_text(f"""#!/bin/bash
echo "docker $*" >> "{log}"
case "$1" in
  ps) [[ {docker_ps_rc} == 0 ]] || exit {docker_ps_rc}; printf '%s\\n' {" ".join(f"'{n}'" for n in containers)} other-container ;;
  inspect)
    name="${{@: -1}}"
    port=$(grep "^$name=" <<'EOF' | cut -d= -f2
{ports}
EOF
)
    echo "uvicorn app.main:app --host 0.0.0.0 --port $port "
    ;;
  exec) echo '{{"ok":true}}' ;;
esac
""", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "ACCT_APACHE_SITE": str(site),
           "OBYS_COLLECT_TIMEOUT": "20"}
    return env, log


def _execs(log):
    return [ln for ln in (log.read_text().splitlines() if log.exists() else []) if ln.startswith("docker exec")]


def _tick(env):
    return subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, timeout=60)


VHOST = "# example http://10.0.0.9:8111\nProxyPass / http://172.18.0.5:8112/\nProxyPassReverse / http://172.18.0.5:8112/\n"


def test_wrapper_runs_exec_in_the_one_active_candidate(tmp_path):
    env, log = _fake_env(tmp_path, site_text=VHOST, containers={"acct-app-candidate-r8": 8111, "acct-app-candidate-r9": 8112})
    res = _tick(env)
    assert res.returncode == 0, res.stderr
    assert _execs(log) == ["docker exec acct-app-candidate-r9 python3 -m app.services.obys_collection_scheduler --once"]


def test_wrapper_exit_2_when_no_candidate_matches_upstream_port(tmp_path):
    env, log = _fake_env(tmp_path, site_text=VHOST, containers={"acct-app-candidate-r8": 8111})
    res = _tick(env)
    assert res.returncode == 2 and "found 0" in res.stderr
    assert _execs(log) == []


def test_wrapper_exit_2_when_two_candidates_share_the_port(tmp_path):
    env, log = _fake_env(tmp_path, site_text=VHOST, containers={"acct-app-candidate-r9": 8112, "acct-app-candidate-r10": 8112})
    res = _tick(env)
    assert res.returncode == 2 and "found 2" in res.stderr
    assert _execs(log) == []


@pytest.mark.parametrize("site_text", [
    "ProxyPass / http://172.18.0.5:8112/\nProxyPass /x http://172.18.0.5:8111/\n",
    "# only comments http://1.2.3.4:5\n",
])
def test_wrapper_exit_2_when_vhost_upstream_ambiguous_or_missing(tmp_path, site_text):
    env, log = _fake_env(tmp_path, site_text=site_text, containers={"acct-app-candidate-r9": 8112})
    res = _tick(env)
    assert res.returncode == 2 and "upstream" in res.stderr
    assert _execs(log) == []


def test_wrapper_exit_2_when_docker_ps_fails_or_site_unreadable(tmp_path):
    env, log = _fake_env(tmp_path, site_text=VHOST, containers={"acct-app-candidate-r9": 8112}, docker_ps_rc=1)
    assert _tick(env).returncode == 2
    env["ACCT_APACHE_SITE"] = str(tmp_path / "missing.conf")
    assert _tick(env).returncode == 2


def test_wrapper_ignores_non_candidate_containers(tmp_path):
    env, log = _fake_env(tmp_path, site_text=VHOST, containers={"acct-pg": 8112, "acct-app-candidate-r9": 8112})
    res = _tick(env)
    assert res.returncode == 0
    assert any(ln.startswith("docker exec acct-app-candidate-r9 ") for ln in log.read_text().splitlines())


def test_wrapper_has_timeout_on_every_long_call():
    text = WRAPPER.read_text(encoding="utf-8")
    assert re.search(r"exec timeout -k \d+ \"\$TOTAL_TIMEOUT\" docker exec", text)
    assert 'TOTAL_TIMEOUT="${OBYS_COLLECT_TIMEOUT:-900}"' in text
    assert "timeout 30 docker ps" in text and "timeout 30 docker inspect" in text


# (f) unit 경로
def _unit_value(path: Path, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", path.read_text(encoding="utf-8"), re.M)
    assert m, key
    return m.group(1).strip()


def _installer_wrapper_dest() -> str:
    m = re.search(r'^WRAPPER_DEST="([^"]+)"', INSTALLER.read_text(encoding="utf-8"), re.M)
    assert m
    return m.group(1)


def test_unit_execstart_is_the_installed_wrapper_path_not_the_repo():
    exec_start = _unit_value(SERVICE, "ExecStart")
    assert not exec_start.startswith("/root/aads")  # cafe24_114 에는 저장소가 없다
    assert exec_start == _installer_wrapper_dest() == "/srv/acct/bin/obys_clobe_collect_tick.sh"
    assert exec_start.endswith(WRAPPER.name)


def test_unit_and_timer_contract():
    svc_text = SERVICE.read_text(encoding="utf-8")
    assert _unit_value(SERVICE, "Type") == "oneshot"
    assert "/root/aads" not in svc_text
    assert int(_unit_value(SERVICE, "TimeoutStartSec")) > 900  # 래퍼 상한(900) 뒤에 systemd 가 끊는다
    assert _unit_value(TIMER, "OnCalendar") == "hourly"
    assert _unit_value(TIMER, "RandomizedDelaySec") == "120"
    assert _unit_value(TIMER, "Persistent") == "true"
    assert _unit_value(TIMER, "Unit") == SERVICE.name


def test_installer_contract_static():
    text = INSTALLER.read_text(encoding="utf-8")
    assert '--dry-run' in text
    assert 'CAFE24_SSH="${CAFE24_SSH:-server-114}"' in text
    assert "-o BatchMode=yes" in text
    assert "timeout -k 10" in text and text.count("ssh ") >= 1
    for step in ("install -d -m 755 /srv/acct/bin", "systemctl daemon-reload",
                 "systemctl enable --now $TIMER_NAME", "systemctl list-timers $TIMER_NAME"):
        assert step in text
    assert text.index("daemon-reload") < text.index("enable --now") < text.index("list-timers")
    assert 'UNIT_DIR="/etc/systemd/system"' in text
    assert "install -m $mode" in text


def test_installer_dry_run_never_calls_ssh(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "ssh_called"
    ssh = bindir / "ssh"
    ssh.write_text(f"#!/bin/bash\ntouch {marker}\n", encoding="utf-8")
    ssh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    res = subprocess.run(["bash", str(INSTALLER), "--dry-run"], env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert not marker.exists()
    for needle in ("/srv/acct/bin/obys_clobe_collect_tick.sh", "/etc/systemd/system/obys-clobe-collect.timer",
                   "daemon-reload", "enable --now obys-clobe-collect.timer", "list-timers"):
        assert needle in res.stdout


def test_installer_refuses_unit_pointing_into_repo(tmp_path):
    src = tmp_path / "acct"
    (src / "systemd").mkdir(parents=True)
    for rel in ("obys_clobe_collect_tick.sh", "systemd/obys-clobe-collect.service", "systemd/obys-clobe-collect.timer"):
        (src / rel).write_text((ROOT / "deploy/acct" / rel).read_text(encoding="utf-8"), encoding="utf-8")
    bad = src / "systemd/obys-clobe-collect.service"
    bad.write_text(bad.read_text(encoding="utf-8").replace(
        "/srv/acct/bin/obys_clobe_collect_tick.sh", "/root/aads/aads-server/scripts/obys_clobe_collect_tick.sh"),
        encoding="utf-8")
    res = subprocess.run(["bash", str(INSTALLER), "--dry-run"], env={**os.environ, "OBYS_TIMER_SRC_DIR": str(src)},
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 1 and "ExecStart" in res.stderr


# (4) 수동 수집 버튼(POST /companies/{id}/runs)과 동시 실행 → lease 로 중복 수집 차단
class _LeaseConn:
    """acquire_lease 의 ON CONFLICT ... WHERE 조건(만료됐거나 같은 owner 일 때만 인수)을 메모리로 흉내 낸다."""

    def __init__(self, store, link):
        self.store, self.link = store, link
        self.released = []

    async def fetchrow(self, sql, *args):
        if "obys_clobe_company_link" in sql:
            return self.link
        return None

    async def fetchval(self, sql, company_id, instance, seconds):
        cur = self.store.get(company_id)
        now = time.monotonic()
        if cur and cur["expires"] >= now and cur["owner"] != instance:
            return None
        epoch = cur["epoch"] + 1 if cur else 1
        self.store[company_id] = {"owner": instance, "epoch": epoch, "expires": now + seconds}
        return epoch

    async def execute(self, sql, *args):
        if "UPDATE obys_clobe_collection_lease SET lease_expires_at = now() - interval" in sql:
            self.store[args[0]]["expires"] = 0
        self.released.append(sql)

    async def close(self):
        return None


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _lease_world(monkeypatch):
    link = {"link_status": "linked", "tenant_id": "15055cac-71b0-45ec-b714-7093dde189ff", "business_id": "biz-x"}
    store: dict = {}
    conns = []

    async def connect():
        c = _LeaseConn(store, link)
        conns.append(c)
        return c

    started = []

    async def preflight(conn, session, ctx):
        started.append(ctx.instance)
        return {}

    async def run_kind(conn, session, base, kind, start, end, payload):
        return {"data_kind": kind, "status": "succeeded"}

    monkeypatch.setattr(svc, "_connect", connect)
    monkeypatch.setattr(svc, "_preflight", preflight)
    monkeypatch.setattr(svc, "_run_kind", run_kind)
    return store, started


def test_lease_sql_guard_is_in_the_service():
    src = Path(svc.__file__).read_text(encoding="utf-8")
    assert "WHERE obys_clobe_collection_lease.lease_expires_at < now()" in src
    assert "OR obys_clobe_collection_lease.owner_instance = EXCLUDED.owner_instance" in src


def test_manual_run_holding_lease_blocks_scheduler(monkeypatch):
    store, started = _lease_world(monkeypatch)
    manual_conn = _LeaseConn(store, None)
    run(svc.acquire_lease(manual_conn, "c1", svc.DEFAULT_INSTANCE))  # 수동 버튼이 수집 중
    with pytest.raises(svc.CollectionError) as err:
        run(svc.run_collection("c1", kinds=[core.KIND_BANK], instance=sched.instance_name(),
                               session_factory=_Session))
    assert err.value.code == "collection_in_progress"
    assert started == []  # 클로브 호출 전에 막혔다


def test_scheduler_holding_lease_blocks_manual_button(monkeypatch):
    store, started = _lease_world(monkeypatch)
    run(svc.acquire_lease(_LeaseConn(store, None), "c1", sched.instance_name()))
    with pytest.raises(svc.CollectionError) as err:
        run(svc.run_collection("c1", kinds=[core.KIND_BANK], session_factory=_Session))  # API 기본 인스턴스
    assert err.value.code == "collection_in_progress" and err.value.status == 409
    assert started == []


def test_scheduler_sees_manual_lease_as_skipped_busy_end_to_end(monkeypatch):
    store, started = _lease_world(monkeypatch)
    run(svc.acquire_lease(_LeaseConn(store, None), "c1", svc.DEFAULT_INSTANCE))

    async def load_state():
        return [{"clobe_company_id": "c1"}], {}

    monkeypatch.setattr(sched, "load_state", load_state)
    real = svc.run_collection

    async def with_session(company_id, **kw):
        return await real(company_id, session_factory=_Session, **kw)

    monkeypatch.setattr(svc, "run_collection", with_session)
    out = run(sched.run_due(NOW))
    assert out["companies"][0]["status"] == "skipped_busy" and out["ok"] is True
    assert started == []


def test_default_instance_name_would_have_hijacked_the_manual_lease(monkeypatch):
    """스케줄러가 기본 이름(aads-api)을 썼다면 같은 owner 재진입으로 임대를 가로챘을 것 — 이름 분리의 근거."""
    store, started = _lease_world(monkeypatch)
    run(svc.acquire_lease(_LeaseConn(store, None), "c1", svc.DEFAULT_INSTANCE))
    result = run(svc.run_collection("c1", kinds=[core.KIND_BANK], instance=svc.DEFAULT_INSTANCE, session_factory=_Session))
    assert result["status"] == "succeeded" and started == [svc.DEFAULT_INSTANCE]
    assert sched.instance_name() != svc.DEFAULT_INSTANCE
