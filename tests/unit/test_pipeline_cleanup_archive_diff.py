"""아카이브 이관이 git_diff 를 무조건 버리지 않는지 정적 검증한다.

2026-09-18 실측: pipeline_jobs_archive 1,137건 중 987건은 commit_hash 없이
끝난(실패·반려) 잡이라 git 커밋으로 diff 를 복원할 수 없다. 그런데
cleanup_stale_jobs 의 아카이브 SQL 은 `to_jsonb(moved) - 'git_diff' - ...`
로 예외 없이 git_diff 를 지웠다 — runner-1f09e9e5 산출물이 이렇게 사라졌다.

DB 접속 없이 cleanup_stale_jobs 의 소스(SQL 문자열)만 검사한다 — 이 함수는
db_pool 을 함수 안에서 import 하므로 모듈 임포트 자체에는 DB 연결이 필요 없다.
"""
from __future__ import annotations

import inspect

from app.services import pipeline_cleanup


def _archive_sql() -> str:
    return inspect.getsource(pipeline_cleanup.cleanup_stale_jobs)


def test_commit_hash_branches_keep_diff_for_commitless_jobs():
    src = _archive_sql()
    assert "moved.commit_hash IS NULL OR moved.commit_hash = ''" in src


def test_diff_is_truncated_with_262144_cap():
    src = _archive_sql()
    assert "262144" in src
    assert "left(moved.git_diff, 262144)" in src
    assert "truncated" in src


def test_logs_and_result_output_always_dropped():
    src = _archive_sql()
    # commit_hash 가 있는 분기(기존 동작 유지): git_diff/logs/result_output 모두 제거.
    assert "to_jsonb(moved) - 'git_diff' - 'logs' - 'result_output'" in src
    # commit_hash 가 없는 분기: logs/result_output 만 제거하고 git_diff 는 유지.
    assert "to_jsonb(moved) - 'logs' - 'result_output'" in src


def test_delete_returning_insert_stays_single_statement():
    src = _archive_sql()
    assert "WITH moved AS (" in src
    assert "DELETE FROM pipeline_jobs" in src
    assert "RETURNING *" in src
    assert "INSERT INTO pipeline_jobs_archive" in src


def test_conflict_must_not_be_do_nothing():
    """ON CONFLICT DO NOTHING 으로 되돌아가면 조용한 유실이 되살아난다.

    DELETE 는 이미 실행된 뒤 INSERT 만 건너뛰므로, job_id 가 아카이브에 이미
    있으면 그 잡은 큐에서도 아카이브에서도 사라진다. 2026-09-21 수정.
    """
    src = _archive_sql()
    # 주석에는 'DO NOTHING' 이 설명으로 남아 있으므로 ON CONFLICT 절만 본다.
    assert "ON CONFLICT (job_id) DO NOTHING" not in src
    assert "ON CONFLICT (job_id) DO UPDATE SET" in src


def test_conflict_update_refreshes_payload_and_archive_time():
    """충돌 시 옛 행을 남기지 말고 최신 잡 내용으로 덮어써야 한다."""
    src = _archive_sql()
    assert "EXCLUDED.row_data" in src
    assert "EXCLUDED.status" in src
    assert "archived_at = NOW()" in src
