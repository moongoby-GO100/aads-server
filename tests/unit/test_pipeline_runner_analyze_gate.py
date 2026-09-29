"""AADS-analyze-gate가 실제 정본 및 임시 버전 fixture를 읽는지 검증한다."""

import pytest

from app.services import pipeline_runner_service
from app.services.pipeline_runner_service import _run_analyze_gate
from app.services.pipeline_runner_service import _unresolved_owner_slices
from app.api.pipeline_runner import _enforce_owner_resolved_gate
from fastapi import HTTPException


@pytest.fixture
def spec_fixture_root(tmp_path, monkeypatch):
    """정본 디렉터리를 건드리지 않는 analyze-gate fixture 저장소 루트."""
    monkeypatch.setattr(
        pipeline_runner_service,
        "__file__",
        str(tmp_path / "app/services/pipeline_runner_service.py"),
    )
    return tmp_path


def _write_slice_documents(root, project, slice_name, documents):
    slice_dir = root / "docs/specs" / project / slice_name
    slice_dir.mkdir(parents=True)
    for filename, content in documents.items():
        (slice_dir / filename).write_text(content, encoding="utf-8")


def test_analyze_gate_is_noop_without_spec_reference(monkeypatch):
    monkeypatch.delenv("AADS_ANALYZE_GATE_ENABLED", raising=False)
    instruction = "TASK_ID: AADS-1\n일반적인 코드 수정 작업입니다.\n"

    assert _run_analyze_gate(instruction, "AADS") == instruction


def test_analyze_gate_blocks_real_conflicted_path_slice(monkeypatch):
    monkeypatch.delenv("AADS_ANALYZE_GATE_ENABLED", raising=False)
    instruction = "docs/specs/obys-v4/receipt-upload 정본에 따라 구현한다."

    result = _run_analyze_gate(instruction, "AADS")

    assert "이 작업은 구현에 착수하지 마라" in result


def test_analyze_gate_blocks_real_conflicted_slice_id(monkeypatch):
    monkeypatch.delenv("AADS_ANALYZE_GATE_ENABLED", raising=False)
    instruction = "obys-v4-receipt-upload 슬라이스를 구현한다."

    result = _run_analyze_gate(instruction, "AADS")

    assert "이 작업은 구현에 착수하지 마라" in result


def test_analyze_gate_is_noop_for_real_clear_slice(monkeypatch):
    monkeypatch.delenv("AADS_ANALYZE_GATE_ENABLED", raising=False)
    instruction = "docs/specs/obys-v4/vat-ledger 정본에 따라 구현한다."

    assert _run_analyze_gate(instruction, "AADS") == instruction


def test_analyze_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AADS_ANALYZE_GATE_ENABLED", "0")
    instruction = "docs/specs/obys-v4/receipt-upload 정본에 따라 구현한다."

    assert _run_analyze_gate(instruction, "AADS") == instruction


def test_analyze_gate_reports_missing_slice_without_raising(monkeypatch):
    monkeypatch.delenv("AADS_ANALYZE_GATE_ENABLED", raising=False)
    instruction = "docs/specs/obys-v4/no-such-slice 정본에 따라 구현한다."

    result = _run_analyze_gate(instruction, "AADS")

    assert "spec.md 없음" in result


@pytest.mark.parametrize("project", ["GO100", "SF", "NTV2"])
def test_analyze_gate_runs_for_every_pipeline_project(monkeypatch, project):
    monkeypatch.delenv("AADS_ANALYZE_GATE_ENABLED", raising=False)

    result = _run_analyze_gate(
        "docs/specs/obys-v4/receipt-upload 정본에 따라 구현한다.", project
    )

    assert "이 작업은 구현에 착수하지 마라" in result


def test_analyze_gate_blocks_version_mismatch_in_fixture(monkeypatch, spec_fixture_root):
    monkeypatch.delenv("AADS_ANALYZE_GATE_ENABLED", raising=False)
    _write_slice_documents(
        spec_fixture_root,
        "version-fixture",
        "conflict",
        {
            "spec.md": "<!-- spec-version: v1 updated: 2026-09-22 source: spec plan tasks -->\n# spec\n",
            "plan.md": "<!-- spec-version: v2 updated: 2026-09-22 source: spec plan tasks -->\n# plan\n",
            "tasks.md": "<!-- spec-version: v1 updated: 2026-09-22 source: spec plan tasks -->\n# tasks\n",
        },
    )

    result = _run_analyze_gate(
        "docs/specs/version-fixture/conflict 정본에 따라 구현한다.", "GO100"
    )

    assert "버전 불일치" in result
    assert "이 작업은 구현에 착수하지 마라" in result


def test_analyze_gate_reports_missing_version_metadata_without_stop(monkeypatch, spec_fixture_root):
    monkeypatch.delenv("AADS_ANALYZE_GATE_ENABLED", raising=False)
    _write_slice_documents(
        spec_fixture_root,
        "version-fixture",
        "missing-metadata",
        {"spec.md": "# spec\n", "plan.md": "# plan\n", "tasks.md": "# tasks\n"},
    )

    result = _run_analyze_gate(
        "docs/specs/version-fixture/missing-metadata 정본에 따라 구현한다.", "SF"
    )

    assert "버전 메타데이터 없음" in result
    assert "이 작업은 구현에 착수하지 마라" not in result


def test_owner_resolved_gate_rejects_unconverged_real_slice(monkeypatch):
    monkeypatch.delenv("AADS_OWNER_RESOLVED_GATE_ENABLED", raising=False)
    instruction = "docs/specs/obys-v4/receipt-upload 정본에 따라 구현한다."

    unresolved = _unresolved_owner_slices(instruction, "AADS")

    assert unresolved == [{
        "spec_dir": "docs/specs/obys-v4/receipt-upload",
        "slice_id": "obys-v4-receipt-upload",
        "status": "충돌 — 2개 이상 정본 메뉴가 동일 화면 참조, 소유권 재결정 필요",
    }]
    with pytest.raises(HTTPException) as exc_info:
        _enforce_owner_resolved_gate(instruction, "AADS")
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "owner_resolution_required"


def test_owner_resolved_gate_allows_converged_real_slice(monkeypatch):
    monkeypatch.delenv("AADS_OWNER_RESOLVED_GATE_ENABLED", raising=False)
    instruction = "docs/specs/obys-v4/vat-ledger 정본에 따라 구현한다."

    assert _unresolved_owner_slices(instruction, "AADS") == []
    _enforce_owner_resolved_gate(instruction, "AADS")


def test_owner_resolved_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AADS_OWNER_RESOLVED_GATE_ENABLED", "0")
    instruction = "docs/specs/obys-v4/receipt-upload 정본에 따라 구현한다."

    assert _unresolved_owner_slices(instruction, "AADS") == []
    _enforce_owner_resolved_gate(instruction, "AADS")


_ROADMAP_HEADER = (
    "| 슬라이스 | 설명 | 메뉴 | 담당 | 상태 |\n"
    "|---|---|---|---|---|\n"
)


def _write_roadmap(root, project, rows):
    roadmap_dir = root / "docs/specs" / project
    roadmap_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(f"| `{slice_id}` | d | m | o | {status} |\n" for slice_id, status in rows)
    (roadmap_dir / "roadmap.md").write_text(_ROADMAP_HEADER + body, encoding="utf-8")


def test_owner_gate_ignores_roadmap_document_file_path(monkeypatch, spec_fixture_root):
    monkeypatch.delenv("AADS_OWNER_RESOLVED_GATE_ENABLED", raising=False)
    _write_roadmap(spec_fixture_root, "gate-fixture", [("gate-fixture-alpha", "충돌")])
    instruction = "docs/specs/gate-fixture/roadmap.md 의 상태 열을 갱신한다."

    assert pipeline_runner_service._find_referenced_spec_dirs(instruction) == []
    assert _unresolved_owner_slices(instruction, "AADS") == []


def test_owner_gate_keeps_blocking_existing_unconverged_slice_dir(monkeypatch, spec_fixture_root):
    monkeypatch.delenv("AADS_OWNER_RESOLVED_GATE_ENABLED", raising=False)
    _write_roadmap(spec_fixture_root, "gate-fixture", [("gate-fixture-alpha", "충돌")])
    _write_slice_documents(spec_fixture_root, "gate-fixture", "alpha", {"spec.md": "# spec\n"})
    instruction = "docs/specs/gate-fixture/alpha/spec.md 를 구현한다."

    assert _unresolved_owner_slices(instruction, "AADS") == [{
        "spec_dir": "docs/specs/gate-fixture/alpha",
        "slice_id": "gate-fixture-alpha",
        "status": "충돌",
    }]


def test_owner_gate_blocks_roadmap_row_without_directory(monkeypatch, spec_fixture_root):
    monkeypatch.delenv("AADS_OWNER_RESOLVED_GATE_ENABLED", raising=False)
    _write_roadmap(spec_fixture_root, "gate-fixture", [("gate-fixture-beta", "충돌")])
    instruction = "docs/specs/gate-fixture/beta 정본에 따라 구현한다."

    assert _unresolved_owner_slices(instruction, "AADS") == [{
        "spec_dir": "docs/specs/gate-fixture/beta",
        "slice_id": "gate-fixture-beta",
        "status": "충돌",
    }]


def test_owner_gate_still_blocks_slice_without_row_and_without_directory(monkeypatch, spec_fixture_root):
    monkeypatch.delenv("AADS_OWNER_RESOLVED_GATE_ENABLED", raising=False)
    _write_roadmap(spec_fixture_root, "gate-fixture", [("gate-fixture-alpha", "충돌")])
    instruction = "docs/specs/gate-fixture/no-such 정본에 따라 구현한다."

    assert _unresolved_owner_slices(instruction, "AADS") == [{
        "spec_dir": "docs/specs/gate-fixture/no-such",
        "slice_id": "gate-fixture-no-such",
        "status": "roadmap_row_missing",
    }]


def test_owner_gate_ignores_arbitrary_md_file_reference(monkeypatch, spec_fixture_root):
    monkeypatch.delenv("AADS_OWNER_RESOLVED_GATE_ENABLED", raising=False)
    _write_roadmap(spec_fixture_root, "gate-fixture", [("gate-fixture-alpha", "충돌")])
    instruction = "docs/specs/gate-fixture/notes-2026.md 를 참고한다."

    assert _unresolved_owner_slices(instruction, "AADS") == []


def test_spec_dir_pattern_keeps_directory_reference_with_trailing_file():
    refs = pipeline_runner_service._find_referenced_spec_dirs(
        "docs/specs/obys-v4/vat-ledger/spec.md 와 docs/specs/obys-v4/roadmap.md"
    )

    assert refs == ["docs/specs/obys-v4/vat-ledger"]
