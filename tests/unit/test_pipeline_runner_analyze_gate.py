"""AADS-analyze-gate가 실제 obys-v4 정본을 읽는지 검증한다."""

from app.services.pipeline_runner_service import _run_analyze_gate


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
