from pathlib import Path

import pytest

from app.services import design_audit_service as service


def test_audit_source_text_detects_design_smells():
    source = """
    <button className="px-3 py-2 bg-[#123456] text-white">🚀 Run</button>
    <div style={{ color: "#abc", background: "rgba(10, 20, 30, 0.5)" }} />
    """

    findings = service.audit_source_text(source, "sample.tsx")
    by_kind = {finding.kind for finding in findings}

    assert "raw_hex_color" in by_kind
    assert "raw_rgb_color" in by_kind
    assert "tailwind_arbitrary_color" in by_kind
    assert "emoji_icon" in by_kind
    assert all(finding.file_path == "sample.tsx" for finding in findings)
    assert all(finding.line >= 1 for finding in findings)
    assert all(finding.column >= 1 for finding in findings)


def test_extract_button_class_patterns_counts_repeated_classes():
    source = """
    <button className="px-2   py-1 text-sm">A</button>
    <button className="px-2 py-1 text-sm">B</button>
    <button class="inline-flex items-center">C</button>
    """

    patterns = service.extract_button_class_patterns(source, "buttons.tsx")
    counts = {pattern.classes: pattern.count for pattern in patterns}

    assert counts["px-2 py-1 text-sm"] == 2
    assert counts["inline-flex items-center"] == 1


def test_resolve_allowed_project_path_rejects_escape(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setitem(service.ALLOWED_PROJECT_ROOTS, "TEST", root)

    assert service.resolve_allowed_project_path("TEST") == root.resolve()

    with pytest.raises(ValueError, match="escapes"):
        service.resolve_allowed_project_path("TEST", "../outside")


def test_audit_project_preview_handles_empty_allowed_directory(monkeypatch, tmp_path):
    monkeypatch.setitem(service.ALLOWED_PROJECT_ROOTS, "EMPTY", tmp_path)

    result = service.audit_project_preview("EMPTY", max_files=10)

    assert result["project_key"] == "EMPTY"
    assert result["read_only"] is True
    assert result["scanned_files"] == 0
    assert result["summary"]["total_findings"] == 0


def test_audit_project_preview_scans_bounded_source_files(monkeypatch, tmp_path):
    project = tmp_path / "project"
    source_dir = project / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "page.tsx").write_text(
        '<button className="bg-[#111111] text-white">✅ Save</button>',
        encoding="utf-8",
    )
    (source_dir / "ignored.png").write_bytes(b"not source")
    monkeypatch.setitem(service.ALLOWED_PROJECT_ROOTS, "TEST_SCAN", project)

    result = service.audit_project_preview("TEST_SCAN", requested_path="src", max_files=10)

    assert result["scanned_files"] == 1
    assert result["summary"]["total_findings"] >= 2
    assert result["summary"]["by_kind"]["tailwind_arbitrary_color"] == 1
    assert result["summary"]["by_kind"]["emoji_icon"] == 1
    assert all(Path(item["file_path"]).suffix == ".tsx" for item in result["findings"])
