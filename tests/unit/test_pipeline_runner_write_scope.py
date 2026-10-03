"""러너 파일 충돌 판정: 실제 수정 범위(TARGET_FILES)와 읽기 참조를 구분한다.

지시문 전체에서 경로처럼 생긴 토큰을 모두 쓰기 대상으로 봐서, 서로 다른 파일을 고치는
작업이 공통 읽기 참조(규칙 문서·기존 구현 파일) 하나 때문에 직렬 대기하던 사고의 회귀 검증.
오류사전: runner.readonly_reference_false_dependency, runner.readonly_rules_false_dependency.
"""
import asyncio
import inspect
import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

import pytest

from app.api import pipeline_runner
from app.api.pipeline_runner import (
    _batch_scope_owners,
    _extract_target_files,
    _find_active_file_conflict,
    _normalize_target_file_path,
    _parse_write_scope,
    _scope_overlap,
)

GOAL_ID = "11111111-1111-4111-8111-111111111111"
M1_ID = "22222222-2222-4222-8222-222222222221"
M7_ID = "22222222-2222-4222-8222-222222222227"
TENANT_ID = "33333333-3333-4333-8333-333333333333"


def _job(target: str, *, read: str = "", prose: str = "", extra: str = "") -> str:
    parts = ["TASK_ID: T", f"TARGET_FILES: {target}"]
    if read:
        parts.append(f"READ_ONLY_FILES: {read}")
    if extra:
        parts.append(extra)
    if prose:
        parts.append(prose)
    parts.append("bash scripts/run_unit_tests.sh tests/unit/test_x.py")
    return "\n".join(parts) + "\n"


class _Conn:
    def __init__(self, existing_instruction: str, orders: dict[str, int] | None = None):
        self.existing_instruction = existing_instruction
        self.orders = orders or {}

    async def fetchrow(self, query, milestone_id, goal_id, tenant_id):
        seq = self.orders.get(milestone_id)
        if seq is None:
            return None
        return {"goal_id": goal_id, "milestone_id": milestone_id, "sequence_order": seq}

    async def fetch(self, query, *args):
        if "FROM pipeline_jobs" in query:
            return [{
                "job_id": "runner-active",
                "instruction": self.existing_instruction,
                "status": "running",
                "phase": "editing",
            }]
        if "FROM chat_workspace_change_ledger" in query:
            return []
        raise AssertionError(query)


def _conflict(existing: str, incoming: str):
    return asyncio.run(_find_active_file_conflict(
        _Conn(existing),
        project="AADS",
        target_files=_extract_target_files(incoming),
        tenant_id=TENANT_ID,
        incoming_instruction=incoming,
    ))


# 1. 서로 다른 쓰기 + 공통 읽기 → 독립
def test_different_writes_with_shared_read_refs_are_independent():
    shared_read = "app/core/shared_rules.py"
    a = _job("app/services/a.py", read=shared_read, prose="`app/core/shared_rules.py` 의 패턴을 따른다.")
    b = _job("app/services/b.py", read=shared_read, prose="app/core/shared_rules.py 를 참고만 한다.")
    assert _extract_target_files(a) == {"server:app/services/a.py"}
    assert _extract_target_files(b) == {"server:app/services/b.py"}
    assert _conflict(a, b) is None


def test_prose_only_read_mentions_do_not_serialize_when_target_files_declared():
    a = _job("app/services/a.py", prose="기존 구현 app/api/pipeline_runner.py 를 먼저 읽어라.")
    b = _job("app/services/b.py", prose="기존 구현 app/api/pipeline_runner.py 를 먼저 읽어라.")
    assert _conflict(a, b) is None


# 2. 동일 쓰기는 차단 유지
def test_same_write_file_still_conflicts():
    a = _job("app/services/a.py", read="app/core/x.py")
    b = _job("app/services/a.py", prose="별도 참조 app/core/y.py")
    result = _conflict(a, b)
    assert result["job_id"] == "runner-active"
    assert result["overlap"] == ["server:app/services/a.py"]


# 3. 읽기/쓰기 혼합 — 쓰기 우선
def test_write_wins_when_same_file_also_declared_read_only():
    a = _job("app/services/a.py", read="app/services/b.py")
    b = _job("app/services/b.py", read="app/services/a.py")
    assert _extract_target_files(b) == {"server:app/services/b.py"}
    assert _conflict(a, b) is None  # a 는 b.py 를 읽기만 한다

    c = _job("app/services/a.py, app/services/b.py", read="app/services/b.py")
    assert _extract_target_files(c) == {"server:app/services/a.py", "server:app/services/b.py"}
    assert _conflict(a, c) is not None


def test_reference_files_marker_is_read_only_too():
    text = "TARGET_FILES: app/a.py\nREFERENCE_FILES: app/ref.py, docs/guide.md\n"
    scope = _parse_write_scope(text)
    assert scope.explicit
    assert scope.write == {"server:app/a.py"}
    assert scope.read == {"server:app/ref.py", "server:docs/guide.md"}


# 4. 명시 쓰기 없는 legacy — 보수적 유지
def test_legacy_without_target_files_treats_all_prose_paths_as_writes():
    text = "app/a.py 를 고치고 app/core/ref.py 를 참고한다.\nREAD_ONLY_FILES: app/core/ref.py\n"
    scope = _parse_write_scope(text)
    assert not scope.explicit
    assert scope.write == {"server:app/a.py", "server:app/core/ref.py"}
    assert _conflict(_job("app/core/ref.py"), text) is not None


def test_empty_or_none_target_files_is_legacy():
    for value in ("", "없음", "none"):
        scope = _parse_write_scope(f"TARGET_FILES: {value}\napp/a.py 수정\n")
        assert not scope.explicit
        assert scope.write == {"server:app/a.py"}


# 5. malformed — 읽기로 추정하지 않고 보수적 판정
@pytest.mark.parametrize("target", ["TBD", "app/a.py 그리고 설명", "../outside.py", "/etc/passwd"])
def test_malformed_target_files_falls_back_to_conservative(target):
    text = f"TARGET_FILES: {target}\nREAD_ONLY_FILES: app/ref.py\n본문 app/body.py 도 언급\n"
    scope = _parse_write_scope(text)
    assert not scope.explicit
    assert scope.error
    assert "server:app/body.py" in scope.write
    assert "server:app/ref.py" in scope.write


def test_malformed_read_only_value_also_falls_back():
    scope = _parse_write_scope("TARGET_FILES: app/a.py\nREAD_ONLY_FILES: 아무거나\napp/body.py\n")
    assert not scope.explicit
    assert scope.error
    assert "server:app/body.py" in scope.write


def test_target_and_forbidden_contradiction_falls_back():
    scope = _parse_write_scope("TARGET_FILES: app/a.py\nFORBIDDEN_FILES: app/\n")
    assert not scope.explicit
    assert "모순" in scope.error


def test_forbidden_inside_write_directory_is_valid():
    scope = _parse_write_scope("TARGET_FILES: app/services/\nFORBIDDEN_FILES: app/services/secret.py\n")
    assert scope.explicit and not scope.error
    assert scope.write == {"server:app/services/"}
    assert scope.forbidden == {"server:app/services/secret.py"}


# 6. 공통 기록 파일은 충돌 유지
def test_common_record_file_mentioned_in_prose_stays_a_write():
    a = _job("app/services/a.py", prose="완료 후 HANDOVER.md 를 갱신한다.")
    b = _job("app/services/b.py", prose="완료 후 HANDOVER.md 를 갱신한다.")
    assert "server:HANDOVER.md" in _extract_target_files(a)
    assert _conflict(a, b)["overlap"] == ["server:HANDOVER.md"]


def test_common_record_file_declared_read_only_is_not_a_write():
    a = _job("app/services/a.py", prose="HANDOVER.md 를 갱신한다.")
    b = _job("app/services/b.py", read="HANDOVER.md", prose="HANDOVER.md 는 읽기만 한다.")
    assert "server:HANDOVER.md" not in _extract_target_files(b)
    assert _conflict(a, b) is None


def test_common_record_file_in_target_files_conflicts():
    a = _job("app/a.py, docs/HANDOVER.md")
    b = _job("app/b.py, docs/HANDOVER.md")
    assert _conflict(a, b)["overlap"] == ["server:docs/HANDOVER.md"]


# 7. 디렉터리 overlap
def test_directory_parent_and_child_file_conflict_both_directions():
    parent = _job("app/services/")
    child = _job("app/services/a.py")
    assert _conflict(parent, child)["overlap"] == ["server:app/services/a.py"]
    assert _conflict(child, parent)["overlap"] == ["server:app/services/a.py"]


def test_directory_without_trailing_slash_is_directory_scope():
    scope = _parse_write_scope("TARGET_FILES: app/services\n")
    assert scope.write == {"server:app/services/"}


def test_directory_overlap_helper_boundaries():
    assert _scope_overlap({"server:app/"}, {"server:app/x.py"}) == ["server:app/x.py"]
    assert _scope_overlap({"server:app/"}, {"server:app/sub/"}) == ["server:app/sub/"]
    assert _scope_overlap({"server:app/"}, {"server:app2/x.py"}) == []
    assert _scope_overlap({"server:app/"}, {"dashboard:app/x.py"}) == []
    assert _scope_overlap({"server:"}, {"server:any/x.py"}) == ["server:any/x.py"]
    assert _scope_overlap({"server:"}, {"dashboard:src/x.tsx"}) == []


def test_server_dashboard_boundary_for_same_relative_name():
    a = _job("/root/aads/aads-server/app/x.py")
    b = _job("/root/aads/aads-dashboard/app/x.py")
    assert _extract_target_files(a) == {"server:app/x.py"}
    assert _extract_target_files(b) == {"dashboard:app/x.py"}
    assert _conflict(a, b) is None


def test_path_normalization_collapses_dots_and_blocks_escape():
    assert _normalize_target_file_path("app/./a//b/../c.py") == "server:app/a/c.py"
    assert _normalize_target_file_path("./app/a.py") == "server:app/a.py"
    assert _normalize_target_file_path("aads-server/app/a.py") == "server:app/a.py"
    assert _normalize_target_file_path("src/app/page.tsx") == "dashboard:src/app/page.tsx"
    assert _normalize_target_file_path("../x.py") == ""
    assert _normalize_target_file_path("/root/aads/aads-server/") == "server:"


# 단건/batch 공통 추출기 + 구현 계약
def test_batch_owner_detection_uses_scope_overlap_including_directories():
    owners = {"server:app/services/": "runner-p", "server:app/other.py": "runner-q"}
    assert _batch_scope_owners({"server:app/services/a.py"}, owners) == [
        ("server:app/services/a.py", "runner-p"),
    ]
    assert _batch_scope_owners({"server:app/third.py"}, owners) == []
    assert _batch_scope_owners({"server:app/other.py"}, owners) == [("server:app/other.py", "runner-q")]


def test_single_and_batch_submit_share_the_scope_parser():
    single = inspect.getsource(pipeline_runner.submit_job)
    batch = inspect.getsource(pipeline_runner.submit_batch)
    assert "_parse_write_scope(req.instruction)" in single
    assert "_parse_write_scope(item.instruction)" in batch
    assert "_batch_scope_owners(" in batch
    assert "path in batch_file_owner" not in batch


def test_active_conflict_check_uses_directory_aware_overlap():
    src = inspect.getsource(_find_active_file_conflict)
    assert "_scope_overlap(target_files, existing_files)" in src
    assert "target_files & existing_files" not in src


# 실제 수정 범위 누락을 숨기지 않는다
def test_undeclared_prose_paths_are_reported_not_silently_dropped():
    text = _job("app/a.py", prose="app/b.py 도 같이 손본다.")
    scope = _parse_write_scope(text)
    assert scope.explicit
    assert "server:app/b.py" in scope.undeclared
    assert "server:app/b.py" not in scope.write


def test_declared_read_and_command_paths_are_not_undeclared():
    text = _job("app/a.py", read="app/ref.py", prose="app/ref.py 참고")
    scope = _parse_write_scope(text)
    assert scope.undeclared == frozenset()
    assert "server:tests/unit/test_x.py" not in scope.write


def test_block_form_bullets_are_parsed_and_not_double_counted_as_prose():
    text = (
        "TARGET_FILES:\n"
        "- `app/a.py` (수정)\n"
        "- tests/unit/test_a.py — 신규\n"
        "READ_ONLY_FILES:\n"
        "- app/ref.py\n"
        "\n"
        "- 이 bullet 은 마커 블록이 아님 app/stray.py\n"
    )
    scope = _parse_write_scope(text)
    assert scope.explicit and not scope.error
    assert scope.write == {"server:app/a.py", "server:tests/unit/test_a.py"}
    assert scope.read == {"server:app/ref.py"}
    assert scope.undeclared == {"server:app/stray.py"}


def test_marker_word_inside_sentence_is_not_a_marker():
    text = "원인: TARGET_FILES: 를 우선하지 않음. app/a.py 수정\n"
    scope = _parse_write_scope(text)
    assert not scope.explicit
    assert scope.write == {"server:app/a.py"}


# 기존 회귀: milestone 역전·명시 depends_on 은 explicit 범위에서도 유지
def test_milestone_inversion_still_detected_with_explicit_scope():
    def inst(mid: str) -> str:
        return f"GOAL_ID: {GOAL_ID}\nMILESTONE_ID: {mid}\nTARGET_FILES: app/a.py\n"

    conn = _Conn(inst(M7_ID), orders={M1_ID: 10, M7_ID: 70})
    result = asyncio.run(_find_active_file_conflict(
        conn,
        project="AADS",
        target_files=_extract_target_files(inst(M1_ID)),
        tenant_id=TENANT_ID,
        incoming_instruction=inst(M1_ID),
    ))
    assert result["dependency_inversion"] is True
    assert result["overlap"] == ["server:app/a.py"]


def test_explicit_depends_on_path_skips_file_conflict_lookup():
    src = inspect.getsource(pipeline_runner.submit_job)
    assert "if req.depends_on:" in src
    assert src.index("if req.depends_on:") < src.index("_find_active_file_conflict(")


def test_exec_command_paths_still_excluded_in_legacy_and_explicit_modes():
    legacy = "bash scripts/run_unit_tests.sh tests/unit/test_a.py\n"
    assert _extract_target_files(legacy) == set()
    explicit = "TARGET_FILES: app/a.py\n4. 확인 `bash scripts/run_unit_tests.sh tests/unit/test_a.py`\n"
    scope = _parse_write_scope(explicit)
    assert scope.write == {"server:app/a.py"}
    assert scope.undeclared == frozenset()
