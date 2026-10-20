"""M5 수집기 — 내부(trusted) tenant fail-closed.

--tenant-id 는 신뢰 tenant(public.aads_internal_tenant_id())와 정확히 같을 때만 쓴다.
없음·불일치·UUID 아님이면 지표 SELECT 도 쓰기도 0회로 실패한다. DB 읽기 전용 모드는 이 검증을 대신하지 않는다.
CLI 진입점(main)을 거쳐 가짜 DB 연결로 호출 횟수를 센다.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from app.services import rdoc_registration_metrics as m

_REPO = Path(__file__).resolve().parents[2]
TRUSTED = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "rdoc_registration_collect_under_test", _REPO / "scripts" / "rdoc_registration_collect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Tx:
    def __init__(self, kw):
        self.kw = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, trusted):
        self.trusted = trusted
        self.fetch_calls: list[tuple] = []
        self.write_calls: list[tuple] = []
        self.closed = False

    def transaction(self, **kw):
        return _Tx(kw)

    async def fetchval(self, sql, *args):
        assert "aads_internal_tenant_id" in sql, sql
        return self.trusted

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return []

    async def execute(self, sql, *args):
        self.write_calls.append((sql, args))

    async def close(self):
        self.closed = True


@pytest.fixture
def env(monkeypatch):
    mod = _load_script()
    state = {"read": None, "write": None, "connects": []}

    def install(trusted):
        async def fake_connect(read_only):
            conn = FakeConn(trusted)
            state["connects"].append(read_only)
            state["read" if read_only else "write"] = conn
            return conn

        monkeypatch.setattr(mod, "_connect", fake_connect)
        monkeypatch.setattr(m, "collect_report_history", lambda *a, **k: [])
        monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **k: types.SimpleNamespace(stdout="0" * 40 + "\n"))

    state["install"] = install
    state["mod"] = mod
    return state


def _run(env, capsys, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["rdoc_registration_collect.py", *argv])
    rc = env["mod"].main()
    return rc, json.loads(capsys.readouterr().out)


def _no_metric_io(env):
    r, w = env["read"], env["write"]
    assert not (r and r.fetch_calls), r.fetch_calls
    assert not (w and w.write_calls)
    assert env["connects"].count(False) == 0, "쓰기 연결을 열었다"


# ── report ────────────────────────────────────────────────────────────


def test_report_rejects_requested_tenant_that_differs_from_trusted(env, capsys, monkeypatch):
    env["install"](TRUSTED)
    rc, out = _run(env, capsys, monkeypatch, "report", "--tenant-id", OTHER, "--start", "2026-10-13", "--end", "2026-10-19")
    assert rc == 1 and "tenant" in out["error"] and "reports" not in out
    _no_metric_io(env)


def test_report_without_trusted_tenant_fails_even_when_tenant_is_requested(env, capsys, monkeypatch):
    env["install"](None)
    rc, out = _run(env, capsys, monkeypatch, "report", "--tenant-id", OTHER)
    assert rc == 1 and "error" in out
    _no_metric_io(env)


def test_report_without_trusted_or_requested_tenant_fails(env, capsys, monkeypatch):
    env["install"](None)
    rc, out = _run(env, capsys, monkeypatch, "report")
    assert rc == 1 and "error" in out
    _no_metric_io(env)


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "11111111-1111-1111-1111", "11111111111111111111111111111111x"])
def test_invalid_requested_uuid_fails_before_any_db_connection(env, capsys, monkeypatch, bad):
    env["install"](TRUSTED)
    for cmd in ("report", "collect"):
        rc, out = _run(env, capsys, monkeypatch, cmd, "--tenant-id", bad)
        assert rc == 1 and "error" in out
    assert env["connects"] == [], "UUID 형식 오류는 연결 전에 거부한다"


def test_report_fails_closed_when_trusted_value_is_not_a_uuid(env, capsys, monkeypatch):
    env["install"]("not-a-uuid")
    rc, out = _run(env, capsys, monkeypatch, "report")
    assert rc == 1 and "error" in out
    _no_metric_io(env)


def test_report_with_matching_tenant_queries_both_metrics_for_that_tenant(env, capsys, monkeypatch):
    env["install"](TRUSTED)
    rc, out = _run(env, capsys, monkeypatch, "report", "--tenant-id", TRUSTED.upper(),
                   "--start", "2026-10-13", "--end", "2026-10-19")
    assert rc == 0 and out["tenant_id"] == TRUSTED
    calls = env["read"].fetch_calls
    assert len(calls) == 2 and all(c[0] == m.DAILY_ROWS_SQL and c[1][0] == TRUSTED for c in calls)
    assert [r["metric"] for r in out["reports"]] == list(m.METRICS)


def test_report_defaults_to_trusted_tenant(env, capsys, monkeypatch):
    env["install"](TRUSTED)
    rc, out = _run(env, capsys, monkeypatch, "report")
    assert rc == 0 and out["tenant_id"] == TRUSTED
    assert all(c[1][0] == TRUSTED for c in env["read"].fetch_calls)


# ── collect ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("trusted,requested", [(TRUSTED, OTHER), (None, OTHER), (None, None)])
def test_collect_apply_with_untrusted_tenant_never_selects_metrics_or_writes(env, capsys, monkeypatch, trusted, requested):
    env["install"](trusted)
    argv = ["collect", "--apply", "--ref", "HEAD", "--start", "2026-10-19", "--end", "2026-10-19"]
    if requested:
        argv += ["--tenant-id", requested]
    rc, out = _run(env, capsys, monkeypatch, *argv)
    assert rc == 1 and "error" in out and "rows" not in out
    _no_metric_io(env)


def test_collect_dry_run_with_matching_tenant_reads_only(env, capsys, monkeypatch):
    env["install"](TRUSTED)
    rc, out = _run(env, capsys, monkeypatch, "collect", "--ref", "HEAD", "--tenant-id", TRUSTED,
                   "--start", "2026-10-19", "--end", "2026-10-19")
    assert rc == 0, out
    assert out["mode"] == "dry-run" and out["written"] == 0
    assert {r["tenant_id"] for r in out["rows"]} == {TRUSTED}
    assert env["connects"] == [True]
    assert env["read"].fetch_calls[0] == (m.KNOWN_PROJECTS_SQL, (TRUSTED,))


def test_collect_apply_writes_rows_for_trusted_tenant_only(env, capsys, monkeypatch):
    env["install"](TRUSTED)
    rc, out = _run(env, capsys, monkeypatch, "collect", "--apply", "--ref", "HEAD",
                   "--start", "2026-10-19", "--end", "2026-10-19")
    assert rc == 0 and out["written"] == len(out["rows"]) > 0
    assert env["connects"] == [True, False]
    assert {c[1][0] for c in env["write"].write_calls} == {TRUSTED}


def test_collect_uses_history_not_window_only_listing(env, capsys, monkeypatch):
    env["install"](TRUSTED)
    seen = {}

    def fake_history(repo, ref, since):
        seen["since"] = since
        return []

    monkeypatch.setattr(m, "collect_report_history", fake_history)
    rc, _ = _run(env, capsys, monkeypatch, "collect", "--ref", "HEAD",
                 "--start", "2026-10-19", "--end", "2026-10-19")
    assert rc == 0 and seen["since"] == dt.date(2026, 10, 19)
