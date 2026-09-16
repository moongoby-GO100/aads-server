"""러너 API 가 아카이브된 작업을 보여주는지 고정한다.

배경: `pipeline_cleanup` 이 1시간마다 종료 작업을 `pipeline_jobs_archive` 로
옮기는데, 러너 조회 API 는 큐 테이블(`pipeline_jobs`)만 읽었다.
그래서 두어 시간 지나면 **자기가 낸 작업이 통째로 사라진 것처럼** 보였고,
"러너가 세션에 연결이 안 된 것 같다" 로 읽혔다.

2026-09-16 실측: 세션 b749ff17 이 낸 5건이 18:26 에 전부 아카이브로 이관됐고
`chat_session_id` 는 아카이브 안에 그대로 남아 있었다. 링크는 멀쩡했고
**보는 테이블이 하나 모자랐을 뿐이다.**
"""

import inspect

import pytest

from app.api import pipeline_runner as pr


ARCHIVED_ROW = {
    "job_id": "runner-168bac82",
    "project": "AADS",
    "instruction": "TASK_ID: AADS-AAG-DEBT-002\n" + ("x" * 400),
    "status": "error",
    "phase": "build_fail",
    "cycle": 0,
    "max_cycles": 3,
    "error_detail": "aads-dashboard:isolated_worktree_required",
    "created_at": "2026-09-16T07:20:33.015376+00:00",
    "updated_at": "2026-09-16T07:49:14.718418+00:00",
    "started_at": "2026-09-16T07:20:50.571254+00:00",
    "chat_session_id": "b749ff17-43d3-4b87-b67f-482b1748b82b",
    "commit_hash": "de77e96d83b74c54d3c04270d933cb4f35c0d9ef",
    "model": "claude-opus-5",
    "size": "M",
    "tenant_id": "2d701a8c-9596-4757-8588-faa4f7837112",
}


def test_archived_item_keeps_the_session_link():
    """세션 연결이 끊긴 것처럼 보이던 원인 — 이 값이 응답에 실려야 한다."""
    item = pr._archived_job_item(ARCHIVED_ROW, detail=False)
    assert item["chat_session_id"] == ARCHIVED_ROW["chat_session_id"]
    assert item["job_id"] == "runner-168bac82"


def test_archived_item_is_marked_as_archived():
    """큐에 없는 이유를 응답이 스스로 설명해야 한다."""
    item = pr._archived_job_item(ARCHIVED_ROW, detail=False)
    assert item["archived"] is True
    assert "아카이브" in item["archive_note"]
    # 무엇이 없는지도 같이 밝힌다 — 다음 사람이 diff 를 찾아 헤매지 않도록
    for omitted in ("git_diff", "logs", "result_output"):
        assert omitted in item["archive_note"]


def test_archived_item_keeps_display_status_contract():
    """UI 가 쓰는 표시 상태 키가 살아 있는 행과 같은 모양이어야 한다."""
    item = pr._archived_job_item(ARCHIVED_ROW, detail=False)
    for key in ("display_status", "status_label"):
        assert key in item, f"{key} 누락 — 큐 행과 응답 모양이 달라진다"
    assert item["status"] == "error"
    assert item["phase"] == "build_fail"


def test_list_mode_truncates_instruction_detail_mode_does_not():
    short = pr._archived_job_item(ARCHIVED_ROW, detail=False)
    full = pr._archived_job_item(ARCHIVED_ROW, detail=True)
    assert len(short["instruction"]) == 200
    assert len(full["instruction"]) == len(ARCHIVED_ROW["instruction"])
    # 상세 조회에는 있어야 하고 목록에는 없어도 되는 것들
    assert full["commit_hash"] == ARCHIVED_ROW["commit_hash"]
    assert full["max_cycles"] == 3
    # 보관하지 않는 필드는 빈 값으로 자리만 지킨다(키 자체가 사라지면 호출부가 깨진다)
    assert full["git_diff"] == ""
    assert full["result_output"] == ""


def test_missing_fields_do_not_crash():
    """아카이브 행은 컬럼이 빠져 있을 수 있다 — KeyError 로 죽으면 안 된다."""
    item = pr._archived_job_item({"job_id": "runner-x", "status": "done"}, detail=True)
    assert item["job_id"] == "runner-x"
    assert item["size"] == "M"
    assert item["actual_changed_files"] == []


def test_get_job_falls_back_to_archive():
    """큐에서 못 찾았을 때 404 로 끝내지 않고 아카이브를 봐야 한다."""
    src = inspect.getsource(pr.get_job)
    assert "_fetch_archived_job" in src, "아카이브 폴백이 사라졌다"
    fallback = src.index("_fetch_archived_job")
    not_found = src.index("작업을 찾을 수 없습니다")
    assert fallback < not_found, "404 를 먼저 던지면 폴백이 의미 없다"


def test_list_jobs_tops_up_from_archive():
    """목록도 큐가 모자라면 아카이브에서 채워야 한다 — 세션 필터일 때 특히."""
    src = inspect.getsource(pr.list_jobs)
    assert "_fetch_archived_jobs" in src
    assert "session_id=session_id" in src, "세션 필터가 아카이브 조회로 전달되지 않는다"
    assert "exclude_ids" in src, "큐와 아카이브가 겹치면 같은 작업이 두 번 나온다"


def test_archive_queries_are_tenant_scoped():
    """테넌트 경계를 넘으면 안 된다."""
    for fn in (pr._fetch_archived_job, pr._fetch_archived_jobs):
        src = inspect.getsource(fn)
        assert "tenant_id" in src, f"{fn.__name__}: 테넌트 조건 없음"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
