"""폴링 엔드포인트 응답 축소 회귀 테스트.

2026-09-14 nginx 24시간 실측. 같은 기간 실제로 보낸 메시지는 111건이었다.

    GET /chat/sessions/{id}/todos    24,092회   2,674MB   평균 116KB
    GET /chat/sessions/{id}/memory-context
                                      3,259회   1,510MB   평균 474KB

두 응답 모두 화면이 안 읽는 데이터가 대부분이었고, 둘 다 상한이 없어서
**세션이 늘수록 계속 커지는** 구조였다. 이 테스트는 그 상한이 도로 풀리는
것을 막는다. 풀려도 기능은 멀쩡해 보이고 트래픽으로만 드러나므로 추적이
오래 걸린다.
"""
import inspect
import uuid

import pytest

from app.models.chat import ChatTodoItemOut, ChatTodoListItemOut
from app.routers import chat as chat_router


def _todo_row(**over):
    row = {
        "id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "message_id": uuid.uuid4(),
        "execution_id": uuid.uuid4(),
        "title": "할 일",
        "status": "pending",
        "sort_order": 1,
        "source": "todo_write",
        "metadata": {"audit": "가" * 400, "message_excerpt": "나" * 350},
        "created_at": "2026-09-14T00:00:00+09:00",
        "updated_at": "2026-09-14T00:00:00+09:00",
        "completed_at": None,
    }
    row.update(over)
    return row


def test_todo_list_model_drops_metadata():
    lean = ChatTodoListItemOut.model_validate(_todo_row()).model_dump(mode="json")
    assert "metadata" not in lean, "목록 응답에 metadata 가 돌아왔다"
    # 화면이 실제로 읽는 필드는 남아 있어야 한다.
    for field in ("id", "status", "title", "created_at", "updated_at"):
        assert field in lean


def test_todo_detail_model_keeps_metadata():
    """단건/생성/수정 응답은 계약을 바꾸지 않는다."""
    full = ChatTodoItemOut.model_validate(_todo_row()).model_dump(mode="json")
    assert full["metadata"]["audit"]


def test_todo_list_is_much_smaller():
    import json

    rows = [_todo_row() for _ in range(100)]
    big = len(json.dumps([ChatTodoItemOut.model_validate(r).model_dump(mode="json") for r in rows],
                         ensure_ascii=False, default=str).encode())
    small = len(json.dumps([ChatTodoListItemOut.model_validate(r).model_dump(mode="json") for r in rows],
                           ensure_ascii=False, default=str).encode())
    assert small < big * 0.5, f"감축이 부족하다: {big:,} → {small:,}"


def test_todo_get_does_not_write_by_default():
    """조회가 쓰기를 겸하면 안 된다.

    `cleanup_stale` 기본값이 True 여서 GET 이 호출될 때마다 DML 이 돌았다.
    하루 24,092회. 게다가 **열어본 세션만** 정리돼서, 아무도 안 보는 세션의
    in_progress 는 영원히 남았다. 정리는 능동 슬롯 워커가 한다.
    """
    sig = inspect.signature(chat_router.get_session_todos)
    assert sig.parameters["cleanup_stale"].default.default is False
    assert sig.parameters["include_metadata"].default.default is False


def test_stale_todo_cleanup_worker_exists():
    """GET 에서 떼어낸 정리가 갈 곳이 실제로 있어야 한다."""
    from app.services.chat_todo_service import (
        cleanup_stale_in_progress_todos_all_sessions,
    )

    assert inspect.iscoroutinefunction(cleanup_stale_in_progress_todos_all_sessions)
    from app.services import chat_service

    loop_src = inspect.getsource(chat_service._stale_placeholder_cleanup_loop)
    assert "cleanup_stale_in_progress_todos_all_sessions" in loop_src, (
        "정리 함수가 워커에 연결돼 있지 않다 — GET 에서만 빼면 정리가 아예 안 돈다"
    )


def test_memory_context_queries_are_bounded():
    """상한 없는 두 쿼리가 다시 풀리는 것을 막는다.

    `ai_observations` 242KB, `session_notes` 75KB 였다. 둘 다 LIMIT 이 없어
    행이 쌓이는 만큼 응답이 커졌다.
    """
    from app.services import chat_service

    src = inspect.getsource(chat_service.get_memory_context_info)
    assert src.count("FROM ai_observations") == 2, "관측 쿼리 갈래 수가 바뀌었다 — 확인 필요"
    for chunk in src.split("FROM ai_observations")[1:]:
        head = chunk[: chunk.index('"""')]
        assert "LIMIT" in head, "ai_observations 쿼리에 LIMIT 이 없다"
    notes = src.split("FROM session_notes")[1]
    assert "LIMIT" in notes[: notes.index('"""')], "session_notes 쿼리에 LIMIT 이 없다"
    # count/tokens 는 잘린 목록이 아니라 전체 집계로 보고해야 한다.
    assert "COUNT(*) OVER ()" in src
    assert "obs_total" in src and "ss_total" in src
