"""공개 스킬 방법론 3건의 사내 후보 스킬(SKILL.md) 계약 (AADS-SKILL-PUBLIC-CANDIDATES-20261028)."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
from app.services import skill_file_import as sfi  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / ".claude" / "skills"
SLUGS = ("mcp-server-design", "skill-authoring", "systematic-debugging")
WRITE_TOOLS = {"bash", "edit", "write", "multiedit", "notebookedit"}
ALLOWED_LICENSES = {"Apache-2.0", "MIT"}
SOURCE_PREFIX = {
    "mcp-server-design": "https://github.com/anthropics/skills/",
    "skill-authoring": "https://github.com/anthropics/skills/",
    "systematic-debugging": "https://github.com/obra/superpowers/",
}

_PATH = ROOT / "app/services/ohvis_harness.py"
_SPEC = importlib.util.spec_from_file_location("ohvis_harness_public_candidates_under_test", _PATH)
harness = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = harness
_SPEC.loader.exec_module(harness)


def _read(slug: str) -> str:
    return (SKILLS_DIR / slug / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("slug", SLUGS)
def test_frontmatter_parses_and_name_matches_directory(slug):
    front = sfi.parse_frontmatter(_read(slug))
    assert front, "frontmatter 파싱 실패"
    assert front["name"] == slug
    assert re.search(r"[가-힣]", str(front["description"])), "description 은 한글이어야 한다"


@pytest.mark.parametrize("slug", SLUGS)
def test_risk_tier_is_read(slug):
    text = _read(slug)
    tier, reasons = sfi.infer_risk_tier(sfi.parse_frontmatter(text), text)
    assert tier == "read", reasons


@pytest.mark.parametrize("slug", SLUGS)
def test_allowed_tools_have_no_write_or_shell_tool(slug):
    tools = sfi.parse_allowed_tools(sfi.parse_frontmatter(_read(slug)))
    assert tools, "allowed-tools 가 비어 있다"
    assert {t.split("(", 1)[0].lower() for t in tools}.isdisjoint(WRITE_TOOLS)
    assert set(tools) == {"Read", "Grep", "Glob"}


@pytest.mark.parametrize("slug", SLUGS)
def test_candidate_status_executor_and_provenance(slug):
    front = sfi.parse_frontmatter(_read(slug))
    assert front["status"] == "candidate"
    assert front["risk_tier"] == "read"
    assert front["executor"] == "model-instructions"
    assert front["executor"] != "ohvis.contract-echo"
    assert str(front["source"]).startswith(SOURCE_PREFIX[slug])
    assert front["license"] in ALLOWED_LICENSES
    assert str(front["adapted"]) == "2026-10-28"


@pytest.mark.parametrize("slug", SLUGS)
def test_body_h1_is_korean_and_has_promotion_and_notice_sections(slug):
    text = _read(slug)
    body = text.split("\n---\n", 1)[1]
    h1 = next(line for line in body.splitlines() if line.startswith("# "))
    assert re.search(r"[가-힣]", h1)
    assert "## 승격 조건" in body
    assert "사용 증거 2건" in body or "사용 증거 **2건**" in body
    assert "## 출처 및 변경 고지" in body
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings[-1] == "## 출처 및 변경 고지", "고지는 파일 끝 절이어야 한다"


def test_license_notices_match_declared_license():
    for slug in ("mcp-server-design", "skill-authoring"):
        notice = _read(slug).split("## 출처 및 변경 고지", 1)[1]
        assert "Apache License 2.0" in notice and "변경 사실" in notice
    notice = _read("systematic-debugging").split("## 출처 및 변경 고지", 1)[1]
    assert "MIT" in notice and "Copyright (c) 2025 Jesse Vincent" in notice and "변경 사실" in notice


def test_scan_skill_files_reports_three_slugs_as_read():
    entries = {e["slug"]: e for e in sfi.scan_skill_files([SKILLS_DIR])}
    for slug in SLUGS:
        assert entries[slug]["risk_tier"] == "read"
        assert entries[slug]["allowed_tools"] == ["Read", "Grep", "Glob"]


def test_scan_repository_skills_includes_three_slugs(monkeypatch):
    monkeypatch.setattr(harness, "_repo_root", lambda: ROOT)
    found = {c["slug"]: c for c in harness.scan_repository_skills()}
    for slug in SLUGS:
        assert slug in found
        assert found[slug]["risk_tier"] == "read"


def test_slugs_do_not_collide_with_existing_skills():
    dirs = sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))
    assert len(dirs) == len(set(dirs))
    assert {"handoff", "sales-channel-collector", "tpp"} <= set(dirs)
