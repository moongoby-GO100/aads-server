"""외부 배포 원장 동기화기(scripts/sync_external_deploy_ledger.py) 단위 테스트.

무엇을 지키려는 테스트인가
  1. 슬롯 정본은 nginx 실측이다. state 파일이 뒤집혀 있어도 실측을 따른다.
  2. 슬롯 개념이 없는 프로젝트(NTV2)에 슬롯을 만들어 적지 않는다.
  3. 파일이 말하지 않는 실패를 만들어내지 않는다. `failed` 는 배포 스크립트가
     직접 남겼을 때만 원장에 실패로 들어간다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sync_external_deploy_ledger.py"
_spec = importlib.util.spec_from_file_location("sync_external_deploy_ledger", _MODULE_PATH)
assert _spec and _spec.loader
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


UPSTREAM_CONF = """
upstream go100_backend {
    zone go100_backend 64k;
    least_conn;
    server 127.0.0.1:8005 max_fails=0;
    server 127.0.0.1:8004 max_fails=3 fail_timeout=30s backup;
}
"""

STATE_GREEN = {
    "phase": "rollback",
    "release_sha": "0f245ae3c",
    "active_slot": "green",
    "active_port": "8005",
    "standby_slot": "blue",
    "standby_port": "8004",
}


def test_parse_nginx_active_port_ignores_backup():
    assert sync.parse_nginx_active_port(UPSTREAM_CONF) == "8005"


def test_parse_nginx_active_port_is_blank_when_ambiguous():
    ambiguous = "upstream x {\n server 127.0.0.1:8004;\n server 127.0.0.1:8005;\n}"
    assert sync.parse_nginx_active_port(ambiguous) == ""


def test_parse_nginx_active_port_ignores_comments():
    commented = "upstream x {\n #server 127.0.0.1:8004;\n server 127.0.0.1:8005; # green\n}"
    assert sync.parse_nginx_active_port(commented) == "8005"


def test_resolve_slots_agrees_with_state():
    current, candidate, note = sync.resolve_slots(STATE_GREEN, "8005")
    assert (current, candidate, note) == ("green", "blue", "")


def test_resolve_slots_prefers_nginx_when_state_is_stale():
    """state 는 green 이라고 하는데 nginx 는 blue(8004) 로 보내고 있다면 blue 가 정본."""
    current, candidate, note = sync.resolve_slots(STATE_GREEN, "8004")
    assert current == "blue"
    assert candidate == "green"
    assert "slot_mismatch" in note


def test_resolve_slots_reports_unreadable_upstream():
    current, candidate, note = sync.resolve_slots(STATE_GREEN, "")
    assert (current, candidate) == ("green", "blue")
    assert note == "nginx_upstream_unreadable"


def test_resolve_slots_reports_unmapped_port():
    _, _, note = sync.resolve_slots(STATE_GREEN, "9999")
    assert note == "nginx_port_unmapped=9999"


def test_parse_queue_keeps_last_row_per_sha():
    raw = (
        "id1\tabc1234\tapproved\towner\tsess\tTASK-1\t2026-09-15T00:00:00+09:00\tfirst\n"
        "id2\tabc1234\tdeployed\towner\tsess\tTASK-1\t2026-09-15T01:00:00+09:00\tsecond\n"
    )
    rows = sync.parse_queue(raw)
    assert set(rows) == {"abc1234"}
    assert rows["abc1234"]["status"] == "deployed"
    assert rows["abc1234"]["note"] == "second"


def _collect_with_fake_ssh(monkeypatch, cfg, queue_raw, state_raw="", upstream_raw=""):
    def fake_ssh(host, command):
        if "release-queue" in command or cfg.get("queue_file", "") in command:
            return queue_raw
        if cfg.get("state_file", "") and cfg["state_file"] in command:
            return state_raw
        if cfg.get("upstream_file", "") and cfg["upstream_file"] in command:
            return upstream_raw
        return ""

    monkeypatch.setattr(sync, "ssh_read", fake_ssh)
    cutoff = sync.datetime(2026, 1, 1, tzinfo=sync.timezone.utc)
    return sync.collect("NTV2", cfg, cutoff)


NTV2_CFG = {
    "host": "127.0.0.1",
    "component": "frontend",
    "deploy_type": "rolling",
    "queue_file": "/var/lib/aads-release-control/NTV2/release-queue.tsv",
    "state_file": "/etc/aads-release/ntv2-release-state",
}


def test_collect_records_failure_only_when_recorded(monkeypatch):
    raw = (
        "id1\tsha1\tfailed\towner\tsess\tNTV2-DEPLOY\t2026-09-15T10:00:00+09:00\t헬스체크 60초 초과\n"
        "id2\tsha2\tpending\towner\tsess\tNTV2-DEPLOY\t2026-09-15T11:00:00+09:00\t아직 아님\n"
    )
    rows = _collect_with_fake_ssh(monkeypatch, NTV2_CFG, raw)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert row["phase"] == "failed"
    assert row["error_summary"] == "헬스체크 60초 초과"


def test_collect_without_releases_root_accepts_deployed(monkeypatch):
    raw = "id1\tsha1\tdeployed\towner\tsess\tNTV2-DEPLOY\t2026-09-15T10:00:00+09:00\t헬스체크 통과\n"
    rows = _collect_with_fake_ssh(monkeypatch, NTV2_CFG, raw)
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["phase"] == "completed"
    assert rows[0]["error_summary"] is None


def test_collect_does_not_invent_slots_for_rolling_project(monkeypatch):
    raw = "id1\tsha1\tdeployed\towner\tsess\tNTV2-DEPLOY\t2026-09-15T10:00:00+09:00\tok\n"
    state = "phase=deployed\nrelease_sha=sha1\nupdated_at=2026-09-15T10:00:00+09:00\n"
    rows = _collect_with_fake_ssh(monkeypatch, NTV2_CFG, raw, state_raw=state)
    assert rows[0]["current_slot"] is None
    assert rows[0]["candidate_slot"] is None


def test_collect_skips_approved_without_release_dir(monkeypatch):
    """승인만 되고 릴리스가 설치되지 않은 것은 배포가 아니다."""
    cfg = dict(NTV2_CFG, releases_root="/opt/nowhere")
    raw = "id1\tsha1\tapproved\towner\tsess\tNTV2-DEPLOY\t2026-09-15T10:00:00+09:00\tok\n"
    rows = _collect_with_fake_ssh(monkeypatch, cfg, raw)
    assert rows == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
