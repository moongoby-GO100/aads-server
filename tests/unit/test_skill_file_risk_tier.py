"""파일 스킬(SKILL.md) 위험등급 판정 계약 (AADS-SKILL-FILE-RISK-TIER-20261028)."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
from app.services import skill_file_import as sfi  # noqa: E402

_PATH = Path(__file__).resolve().parents[2] / "app/services/ohvis_harness.py"
_SPEC = importlib.util.spec_from_file_location("ohvis_harness_risk_tier_under_test", _PATH)
harness = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = harness
_SPEC.loader.exec_module(harness)


@pytest.mark.parametrize("front,text,expected", [
    ({}, "본문\n", "read"),
    ({"allowed-tools": "Read, Grep"}, "본문", "read"),
    ({"allowed-tools": "Bash, Edit"}, "본문", "write"),
    ({"allowed-tools": ["Read", "Write"]}, "본문", "write"),
    ({"allowed_tools": ["Read", "Bash(git status:*)"]}, "본문", "write"),
    ({"allowed-tools": "Read Glob"}, "본문", "read"),
    ({"allowed-tools": "Read"}, "run bash deploy.sh now", "deploy"),
    ({"allowed-tools": "Read"}, "git push origin main", "deploy"),
    ({"allowed-tools": "Bash"}, "docker compose up", "deploy"),
    ({"allowed-tools": "Bash", "risk_tier": "read"}, "본문", "write"),
    ({"risk-tier": "financial"}, "본문", "financial"),
    ({"risk_tier": "auth", "allowed-tools": "Bash"}, "본문", "auth"),
    ({"risk_tier": "bogus"}, "본문", "read"),
])
def test_infer_risk_tier(front, text, expected):
    assert sfi.infer_risk_tier(front, text)[0] == expected


def test_missing_allowed_tools_is_recorded_in_reasons():
    assert "allowed-tools 미기재" in sfi.infer_risk_tier({}, "x")[1]
    assert "allowed-tools 미기재" not in sfi.infer_risk_tier({"allowed-tools": "Read"}, "x")[1]


def test_parse_allowed_tools_forms():
    assert sfi.parse_allowed_tools({"allowed-tools": "Bash, Read,  Glob"}) == ["Bash", "Read", "Glob"]
    assert sfi.parse_allowed_tools({"allowed_tools": ["Read", "Grep"]}) == ["Read", "Grep"]
    assert sfi.parse_allowed_tools({"allowed-tools": "Bash(git add:*) Read"}) == ["Bash(git add:*)", "Read"]
    assert sfi.parse_allowed_tools({}) == []


_SALES_FRONT = (
    "---\nname: sales-channel-collector\ndescription: 판매채널 수집\n"
    "allowed-tools: Bash, Read, Glob, Grep, Edit, Write\n---\n# 스킬\n"
)


def test_scan_skill_files_uses_inferred_tier(tmp_path):
    path = tmp_path / "sales-channel-collector" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(_SALES_FRONT, encoding="utf-8")
    plain = tmp_path / "plain" / "SKILL.md"
    plain.parent.mkdir(parents=True)
    plain.write_text("---\nname: plain\n---\nbody\n", encoding="utf-8")
    entries = {e["slug"]: e for e in sfi.scan_skill_files([tmp_path])}
    assert entries["sales-channel-collector"]["risk_tier"] == "write"
    assert len(entries["sales-channel-collector"]["allowed_tools"]) == 6
    assert entries["plain"]["risk_tier"] == "read"
    assert entries["plain"]["allowed_tools"] == []


def test_scan_repository_skills_returns_write_and_tools(tmp_path, monkeypatch):
    path = tmp_path / ".claude" / "skills" / "sales-channel-collector" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(_SALES_FRONT, encoding="utf-8")
    monkeypatch.setattr(harness, "_repo_root", lambda: tmp_path)
    (item,) = harness.scan_repository_skills()
    assert item["risk_tier"] == "write"
    assert item["policy"] == harness.RISK_POLICIES["write"]
    assert item["policy"]["approval_required"] is True
    assert item["allowed_tools"] == ["Bash", "Read", "Glob", "Grep", "Edit", "Write"]
    assert {"slug", "title", "projects", "intents", "source", "validation", "content_preview"} <= set(item)
