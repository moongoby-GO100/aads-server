from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import aag_tools


ROOT = Path(__file__).resolve().parents[2]
TOOL_NAMES = ("aag_findings", "aag_brief")


def test_aag_tools_are_wired_in_all_required_locations():
    registry = (ROOT / "app/services/tool_registry.py").read_text(encoding="utf-8")
    executor = (ROOT / "app/services/tool_executor.py").read_text(encoding="utf-8")
    exposure = (ROOT / "app/api/ceo_chat_tools.py").read_text(encoding="utf-8")

    for name in TOOL_NAMES:
        assert f'"{name}": True' in registry
        assert f'"{name}": {{' in registry
        assert name in registry[registry.index('"research": ['):]
        assert f'"{name}":' in executor
        assert name in exposure[exposure.index("tool_executor 위임"):]


def test_findings_filters_rule_severity_and_path_prefix():
    findings = [
        {"rule": "DOUBLE_MOUNT", "severity": "P1", "module": "app/api/ops.py"},
        {"rule": "DOUBLE_MOUNT", "severity": "P2", "module": "app/api/other.py"},
        {"rule": "TABLE_NO_MODEL", "severity": "P1", "module": "app/api/ops.py"},
    ]

    assert aag_tools.filter_findings(findings, rule="double_mount") == findings[:2]
    assert aag_tools.filter_findings(findings, severity="p2") == findings[1:2]
    assert aag_tools.filter_findings(findings, path_prefix="app/api/ops") == [findings[0], findings[2]]
    assert aag_tools.filter_findings(
        findings, rule="DOUBLE_MOUNT", severity="P1", path_prefix="app/api/ops"
    ) == findings[:1]


@pytest.mark.asyncio
async def test_missing_snapshot_and_graph_returns_empty_result_with_reason(monkeypatch):
    async def no_snapshot(project):
        return None

    monkeypatch.setattr(aag_tools, "latest_snapshot", no_snapshot)
    monkeypatch.setattr(aag_tools, "local_graph", lambda project: (None, None, "그래프가 없습니다."))

    result = await aag_tools.get_findings_data("MISSING")

    assert result["findings"] == []
    assert result["reason"] == "그래프가 없습니다."


def test_stale_minutes_uses_generated_at_age():
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    generated_at = now - timedelta(minutes=42, seconds=30)

    assert aag_tools.stale_minutes(generated_at, now) == 42.5


@pytest.mark.asyncio
async def test_aag_brief_returns_canonical_renderer_stdout_unchanged(monkeypatch):
    expected = "# AAG 착수 브리프\n\n원문 그대로\n"
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout=expected, stderr="")

    monkeypatch.setattr(aag_tools.subprocess, "run", fake_run)

    actual = await aag_tools.aag_brief_text("AADS", "app/api/ops.py를 수정한다")

    assert actual == expected
    assert captured["command"][1].endswith("tools/aag/brief.py")
    assert "--instruction-file" in captured["command"]


def test_api_mount_is_single_and_uses_expected_prefix():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert main.count("app.include_router(aag_router") == 1
    assert 'app.include_router(aag_router, prefix="/api/v1", tags=["aag"])' in main


def test_migration_stores_summary_not_graph_nodes_and_edges():
    sql = (ROOT / "migrations/20260918_aag_graph_snapshots.sql").read_text(encoding="utf-8")
    assert "UNIQUE (project, generated_at)" in sql
    assert "(project, created_at DESC)" in sql
    assert "node_count INTEGER" in sql
    assert "edge_count INTEGER" in sql
    assert "nodes JSONB" not in sql
    assert "edges JSONB" not in sql
