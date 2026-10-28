"""scripts/goal_grant_suspend_reconcile.py 의 복원 계획·적용(--apply-restore) 단위 테스트. DB 는 쓰지 않는다."""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "goal_grant_suspend_reconcile_under_test", REPO / "scripts" / "goal_grant_suspend_reconcile.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

G1 = "40cfdfc5-0000-4000-8000-000000000001"
G2 = "11111111-0000-4000-8000-000000000002"
T = "22222222-0000-4000-8000-0000000000aa"


def _plan(*goal_ids):
    return [{"goal_id": g, "tenant_id": T} for g in goal_ids]


def test_restore_plan_sql_is_read_only_and_uses_restore_conditions():
    sql = mod._restore_plan_sql()
    assert sql.strip().startswith("BEGIN READ ONLY;") and sql.strip().endswith("ROLLBACK;")
    assert "g.status = 'active' AND g.paused_at IS NULL" in sql
    assert "gr.status = 'paused'" in sql
    for reason in mod.RULES.RESTORABLE_REASONS:
        assert "'%s'" % reason in sql
    assert "starts_with(gr.revoke_reason, '%s')" % mod.RULES.RESTORABLE_REASON_PREFIX in sql
    for blocker in ("expired", "assignment_inactive", "newer_policy"):
        assert "'%s'" % blocker in sql
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", sql)


def test_build_restore_sql_uses_rules_restore_sql_in_goal_id_order_single_tx():
    out = mod.build_restore_sql(_plan(G1, G2))
    lines = out.split("\n")
    assert lines[0] == "BEGIN;" and lines[-1] == "COMMIT;"
    assert out.count("BEGIN;") == 1 and out.count("COMMIT;") == 1
    lock = lines[1]
    assert "FOR UPDATE" in lock and lock.index(G2) < lock.index(G1)
    expected = [
        mod.render(mod.RULES.RESTORE_SQL.strip().rstrip(";"), g, T, mod.RESTORE_SOURCE) + ";"
        for g in (G2, G1)
    ]
    assert out.split("\n", 2)[2].rsplit("\n", 1)[0] == "\n".join(expected)
    assert "'%s'" % mod.RESTORE_SOURCE in out
    assert mod.RESTORE_SOURCE != mod.SOURCE
    assert "SET status = 'paused'" not in out


def test_build_apply_sql_still_suspends_only():
    out = mod.build_apply_sql(_plan(G1))
    assert "SET status = 'paused'" in out
    assert "grant_restored" not in out
    assert "'%s'" % mod.SOURCE in out and mod.RESTORE_SOURCE not in out


@pytest.mark.parametrize("builder", ["build_apply_sql", "build_restore_sql"])
def test_non_uuid_values_rejected(builder):
    fn = getattr(mod, builder)
    with pytest.raises(SystemExit):
        fn([{"goal_id": "x'; DROP TABLE goals;--", "tenant_id": T}])
    with pytest.raises(SystemExit):
        fn([{"goal_id": G1, "tenant_id": "not-a-uuid"}])


def _run_main(monkeypatch, argv, *, suspend_plan, restore_plan):
    executed = []

    def fake_run_psql(sql):
        executed.append(sql)
        return [G1] if sql.startswith("\nBEGIN;") or sql.startswith("BEGIN;") else []

    monkeypatch.setattr(mod, "fetch_plan", lambda: suspend_plan)
    monkeypatch.setattr(mod, "fetch_restore_plan", lambda: restore_plan)
    monkeypatch.setattr(mod, "run_psql", fake_run_psql)
    monkeypatch.setattr(sys, "argv", ["goal_grant_suspend_reconcile.py"] + argv)
    return mod.main(), executed


_RESTORE_ROW = {
    "goal_id": G1, "tenant_id": T, "title": "t", "project": "GO100", "priority": "P1",
    "grants": 1, "grant_ids": "fb9262d8", "reasons": "goal_not_active:blocked",
    "earliest_expiry": "2026-11-14", "restorable": 1, "blocked_by": "",
}


def test_apply_does_not_restore(monkeypatch):
    suspend = [{"goal_id": G2, "tenant_id": T}]
    rc, executed = _run_main(monkeypatch, ["--apply"], suspend_plan=suspend, restore_plan=[_RESTORE_ROW])
    assert len(executed) == 1
    assert "SET status = 'paused'" in executed[0]
    assert "grant_restored" not in executed[0]
    assert G1 not in executed[0]
    assert rc in (0, 1)


def test_apply_restore_does_not_suspend(monkeypatch):
    calls = {"n": 0}

    def restore_plan():
        calls["n"] += 1
        return [_RESTORE_ROW] if calls["n"] == 1 else []

    executed = []
    monkeypatch.setattr(mod, "fetch_plan", lambda: pytest.fail("suspend plan must not be read"))
    monkeypatch.setattr(mod, "fetch_restore_plan", restore_plan)
    monkeypatch.setattr(mod, "run_psql", lambda sql: executed.append(sql) or [G1])
    monkeypatch.setattr(sys, "argv", ["x", "--apply-restore"])
    assert mod.main() == 0
    assert len(executed) == 1
    assert "grant_restored" in executed[0] and "SET status = 'paused'" not in executed[0]


def test_dry_run_prints_both_plans_and_writes_nothing(monkeypatch, capsys):
    rc, executed = _run_main(monkeypatch, ["--dry-run"], suspend_plan=[], restore_plan=[_RESTORE_ROW])
    out = capsys.readouterr().out
    assert rc == 0 and executed == []
    assert "40cfdfc5" in out and "fb9262d8" in out and "복원 안 됨 사유" in out


def test_dry_run_json_keeps_existing_keys(monkeypatch, capsys):
    _run_main(monkeypatch, ["--dry-run", "--json"], suspend_plan=[], restore_plan=[_RESTORE_ROW])
    import json
    data = json.loads(capsys.readouterr().out)
    assert {"targets", "grants", "plan"} <= set(data)
    assert data["restore_targets"] == 1 and data["restore_grants"] == 1


def test_modes_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "--apply", "--apply-restore"])
    with pytest.raises(SystemExit):
        mod.main()
