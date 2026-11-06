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


class PipelineCursor:
    def __init__(self, records):
        self.records = records

    def execute(self, query, params):
        self.query, self.params = query, params

    def fetchall(self):
        return self.records


def test_collect_pipeline_deployed_jobs():
    at = sync.datetime(2026, 11, 6, tzinfo=sync.timezone.utc)
    cur = PipelineCursor([("GO100", "runner-new", "abc1234", at),
                          ("NTV2", "runner-nt", "def1234", at)])
    rows = sync.collect_pipeline_deployments(cur, sync.SOURCES, at)
    assert cur.params == (list(sync.SOURCES), at)
    assert "status = 'done'" in cur.query
    assert "deployed_at IS NOT NULL" in cur.query
    assert "deployed_at >= %s" in cur.query
    assert [row["runner_job_id"] for row in rows] == ["runner-new", "runner-nt"]
    assert rows[0]["release_sha"] == "abc1234"
    assert rows[0]["completed_at"] == at
    assert rows[0]["request_source"] == "pipeline_runner_deployed"
    assert (rows[0]["status"], rows[0]["phase"]) == ("success", "completed")
    assert rows[0]["current_slot"] is None


@pytest.mark.parametrize("sha", [None, "", "  "])
def test_collect_pipeline_skips_missing_commit_hash(sha):
    at = sync.datetime(2026, 11, 6, tzinfo=sync.timezone.utc)
    cur = PipelineCursor([("GO100", "runner-no-sha", sha, at)])
    assert sync.collect_pipeline_deployments(cur, sync.SOURCES, at) == []


def test_collect_hotfix_state_and_idempotency(monkeypatch):
    note = "/opt/go100/backend-releases/a7eb5e8a8-hf-1b6551392 (1b6551392, OpsInfraOwner manual, both slots)"
    state = (
        "release_sha=a7eb5e8a8\nphase=deployed\n"
        "updated_at=2026-11-06T07:30:00+09:00\n"
        f"hotfix_release_dir={note}\n"
    )
    rows = _collect_with_fake_ssh(monkeypatch, NTV2_CFG, "", state)
    again = _collect_with_fake_ssh(monkeypatch, NTV2_CFG, "", state)
    assert len(rows) == 1
    row = rows[0]
    assert row["release_sha"] == "1b6551392"
    assert row["runner_job_id"] == "state-a7eb5e8a8-hf-1b6551392"
    assert row["phase"] == "deployed"
    assert row["completed_at"] == sync.to_dt("2026-11-06T07:30:00+09:00")
    assert row["payload"]["base_release_sha"] == "a7eb5e8a8"
    assert row["payload"]["release_dir"] == note.split()[0]
    assert row["payload"]["note"] == note
    existing = tuple(row.get(sync._ROW_KEYS.get(col, col)) for col in sync._COMPARE_COLUMNS)
    assert existing == sync._comparable(again[0])


@pytest.mark.parametrize("suffix", ["-hf-1b6551392", "-memory-live"])
def test_collect_matches_suffixed_release_dir(monkeypatch, suffix):
    cfg = sync.SOURCES["GO100"]
    at = sync.datetime(2026, 11, 6, tzinfo=sync.timezone.utc)
    raw = f"a7eb5e8a8{suffix}\t{at.timestamp()}\n"
    queue = "id1\ta7eb5e8a8\tapproved\towner\tsess\tTASK\t2026-11-05T00:00:00+00:00\tok"
    def fake_ssh(host, command):
        if "find " in command:
            return raw
        if cfg["queue_file"] in command:
            return queue
        return ""
    monkeypatch.setattr(sync, "ssh_read", fake_ssh)
    rows = sync.collect("GO100", cfg, at - sync.timedelta(days=1))
    assert len(rows) == 1
    assert rows[0]["release_sha"] == "a7eb5e8a8"
    assert rows[0]["completed_at"] == at
    assert rows[0]["phase"] == "released"
    queue = ""
    assert sync.collect("GO100", cfg, at - sync.timedelta(days=1)) == []


def test_release_dirs_use_latest_mtime_independent_of_order():
    assert sync.parse_release_dirs(
        "abc1234-hf-def1234\t200\nabc1234\t100\n"
    )["abc1234"] == sync.datetime.fromtimestamp(200, sync.timezone.utc)


@pytest.mark.parametrize("extra", [
    "hotfix_release_dir=/opt/release (invalid, note)\n",
    "hotfix_release_dir=/opt/release (abc1234, note)\nupdated_at=invalid\n",
    "hotfix_release_dir=/opt/release (abc1234, note)\nupdated_at=2025-01-01\n",
])
def test_hotfix_requires_sha_and_recent_timestamp(monkeypatch, extra):
    state = "release_sha=def1234\nphase=deployed\n" + extra
    assert _collect_with_fake_ssh(monkeypatch, NTV2_CFG, "", state) == []


def test_main_skips_upsert_for_unchanged_runner_and_hotfix(monkeypatch, capsys):
    at = sync.datetime.now(sync.timezone.utc)
    pipeline = ("GO100", "runner-new", "abc1234", at)
    row = sync.collect_pipeline_deployments(PipelineCursor([pipeline]), sync.SOURCES, at)[0]
    hotfix = dict(row, runner_job_id="state-abc1234-hf-def1234",
                  release_sha="def1234", request_source="external_release_sync")
    existing = [
        (r["project"], r["runner_job_id"], *sync._comparable(r))
        for r in (row, hotfix)
    ]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params):
            assert "INSERT" not in query, "unchanged rows must not consume a sequence id"
            self.query = query

        def fetchall(self):
            return ([pipeline] if "FROM pipeline_jobs" in self.query else
                    [] if "IS DISTINCT FROM" in self.query else existing)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return Cursor()

        def set_session(self, **kwargs):
            assert kwargs == {"readonly": True}

        def close(self):
            pass

    monkeypatch.setattr(sync, "connect_db", Connection)
    monkeypatch.setattr(sync, "collect", lambda project, cfg, cutoff: [hotfix])
    monkeypatch.setattr(sync.sys, "argv", ["sync", "--project", "GO100"])
    assert sync.main() == 0
    assert "변화없음 2건" in capsys.readouterr().out


@pytest.mark.parametrize('queue_id,queue_sha,existing', [
    ('runner-X', 'def1234', set()),
    ('runner-X-abc123456', 'def1234', set()),
    ('queue-other', 'def1234', {('GO100', 'runner-X')}),
    ('queue-other', 'abc123456', set()),
])
def test_runner_duplicates_prefer_queue(queue_id, queue_sha, existing):
    runner = dict(project='GO100', runner_job_id='runner-X', release_sha='abc123456789')
    queue = dict(project='GO100', runner_job_id=queue_id, release_sha=queue_sha)
    assert sync.filter_runner_deployments([runner], [queue], existing) == []


def test_runner_filter_keeps_other_project_and_short_sha():
    runner = dict(project='GO100', runner_job_id='runner-X', release_sha='abc123456789')
    queues = [dict(project='NTV2', runner_job_id='runner-X', release_sha='abc123456'),
              dict(project='GO100', runner_job_id='other', release_sha='abc123')]
    assert sync.filter_runner_deployments([runner], queues, set()) == [runner]


def _mock_ledger(monkeypatch, rows, fail_read=False):
    stored = {}
    attempts = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params):
            self.query = query
            if query == sync.UPSERT_SQL:
                key = (params['project'], params['runner_job_id'])
                self.is_new = key not in stored
                stored[key] = sync._comparable(params)
                attempts.append(key)

        def fetchall(self):
            if 'FROM pipeline_jobs' in self.query:
                return []
            if 'IS DISTINCT FROM' in self.query:
                return list(stored)
            return [(project, job, *values) for (project, job), values in stored.items()]

        def fetchone(self):
            return 1, self.is_new

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return Cursor()

        def set_session(self, **kwargs):
            assert kwargs == {'readonly': True}

        def close(self):
            pass

    calls = 0

    def connect():
        nonlocal calls
        calls += 1
        if fail_read and calls == 1:
            raise RuntimeError('read unavailable')
        return Connection()

    monkeypatch.setattr(sync, 'connect_db', connect)
    monkeypatch.setattr(sync, 'collect', lambda *args: rows)
    monkeypatch.setattr(sync.sys, 'argv', ['sync', '--project', 'GO100'])
    return stored, attempts


def _queue_row():
    at = sync.datetime.now(sync.timezone.utc)
    row = sync.collect_pipeline_deployments(
        PipelineCursor([('GO100', 'queue-X', 'abc123456', at)]), sync.SOURCES, at)[0]
    return dict(row, request_source='external_release_sync')


def test_main_second_identical_run_has_zero_updates(monkeypatch, capsys):
    stored, attempts = _mock_ledger(monkeypatch, [_queue_row()])
    assert sync.main() == 0
    assert sync.main() == 0
    assert len(stored) == len(attempts) == 1
    assert '신규 0건 / 갱신 0건 / 변화없음 1건' in capsys.readouterr().out


@pytest.mark.parametrize('dry_run', [False, True])
def test_connect_failure_keeps_queue_processing(monkeypatch, capsys, dry_run):
    stored, attempts = _mock_ledger(monkeypatch, [_queue_row()], fail_read=True)
    if dry_run:
        monkeypatch.setattr(sync.sys, 'argv', ['sync', '--project', 'GO100', '--dry-run'])
    assert sync.main() == 0
    assert len(attempts) == (0 if dry_run else 1)
    output = capsys.readouterr()
    assert '[warn] pipeline_jobs 읽기 실패: RuntimeError' in output.err
    assert ('[dry-run] 1건' if dry_run else '[apply] 신규 1건') in output.out


@pytest.mark.parametrize('dry_run', [False, True])
def test_existing_queue_source_blocks_runner_in_main(monkeypatch, capsys, dry_run):
    row = _queue_row()
    stored, attempts = _mock_ledger(monkeypatch, [])
    stored[(row['project'], row['runner_job_id'])] = sync._comparable(row)
    monkeypatch.setattr(sync, 'collect_pipeline_deployments', lambda *args: [
        dict(row, request_source='pipeline_runner_deployed')])
    if dry_run:
        monkeypatch.setattr(sync.sys, 'argv', ['sync', '--project', 'GO100', '--dry-run'])
    assert sync.main() == 0
    assert attempts == []
    assert '[collect-runner] GO100: 0건 (중복 제외 1건)' in capsys.readouterr().out
