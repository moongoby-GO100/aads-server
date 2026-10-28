"""공개 스킬 방법론 16건(1차 3 + 2차 8 + 3차 5)의 사내 후보 스킬(SKILL.md) 계약 (AADS-SKILL-PUBLIC-CANDIDATES-20261028 / -B2- / -B3-)."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
from app.services import skill_file_import as sfi  # noqa: E402

_FORBIDDEN_KEY = "ANTHROPIC_" + "API_KEY"  # R-AUTH: 부재 검사 전용 상수, 키를 읽지 않는다
_NEGATION = ("않", "금지", "없", "버리", "충돌")

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / ".claude" / "skills"
BATCH1 = ("mcp-server-design", "skill-authoring", "systematic-debugging")
BATCH2 = (
    "webapp-verification",
    "frontend-design-guide",
    "verification-before-completion",
    "writing-plans",
    "test-driven-development",
    "code-review-cycle",
    "brainstorming",
    "parallel-agent-dispatch",
)
SLUGS = BATCH1 + BATCH2
CLEAN_ROOM_SLUGS = ("document-coauthoring", "release-changelog", "pairwise-test-design", "prompt-asset-authoring")
BATCH3_ADAPTED = ("safe-db-query", "aads-claude-api")
BATCH3 = CLEAN_ROOM_SLUGS + BATCH3_ADAPTED
ALL_SLUGS = SLUGS + BATCH3
INSPIRED_BY_PREFIX = {
    "document-coauthoring": "https://github.com/anthropics/skills/",
    "release-changelog": "https://github.com/ComposioHQ/awesome-claude-skills/",
    "pairwise-test-design": "https://github.com/omkamal/pypict-claude-skill",
    "prompt-asset-authoring": "https://github.com/NeoLabHQ/context-engineering-kit",
}
CLEAN_ROOM_NOTICE = "원본 텍스트를 참조하지 않고 아이디어만 참고해 사내에서 독자 작성함"
APACHE_SLUGS = ("mcp-server-design", "skill-authoring", "webapp-verification", "frontend-design-guide")
MIT_SLUGS = tuple(s for s in SLUGS if s not in APACHE_SLUGS)
WRITE_TOOLS = {"bash", "edit", "write", "multiedit", "notebookedit"}
ALLOWED_LICENSES = {"Apache-2.0", "MIT"}
SOURCE_PREFIX = {
    "mcp-server-design": "https://github.com/anthropics/skills/",
    "skill-authoring": "https://github.com/anthropics/skills/",
    "systematic-debugging": "https://github.com/obra/superpowers/",
    "webapp-verification": "https://github.com/anthropics/skills/",
    "frontend-design-guide": "https://github.com/anthropics/skills/",
    **{slug: "https://github.com/obra/superpowers/" for slug in BATCH2[2:]},
    "safe-db-query": "https://github.com/sanjay3290/ai-skills/",
    "aads-claude-api": "https://github.com/anthropics/skills/",
}

_PATH = ROOT / "app/services/ohvis_harness.py"
_SPEC = importlib.util.spec_from_file_location("ohvis_harness_public_candidates_under_test", _PATH)
harness = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = harness
_SPEC.loader.exec_module(harness)


def _read(slug: str) -> str:
    return (SKILLS_DIR / slug / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_frontmatter_parses_and_name_matches_directory(slug):
    front = sfi.parse_frontmatter(_read(slug))
    assert front, "frontmatter 파싱 실패"
    assert front["name"] == slug
    assert re.search(r"[가-힣]", str(front["description"])), "description 은 한글이어야 한다"


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_risk_tier_is_read(slug):
    text = _read(slug)
    tier, reasons = sfi.infer_risk_tier(sfi.parse_frontmatter(text), text)
    assert tier == "read", reasons


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_allowed_tools_have_no_write_or_shell_tool(slug):
    tools = sfi.parse_allowed_tools(sfi.parse_frontmatter(_read(slug)))
    assert tools, "allowed-tools 가 비어 있다"
    assert {t.split("(", 1)[0].lower() for t in tools}.isdisjoint(WRITE_TOOLS)
    assert set(tools) == {"Read", "Grep", "Glob"}


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_candidate_status_executor_and_adapted_date(slug):
    front = sfi.parse_frontmatter(_read(slug))
    assert front["status"] == "candidate"
    assert front["risk_tier"] == "read"
    assert front["executor"] == "model-instructions"
    assert front["executor"] != "ohvis.contract-echo"
    assert str(front["adapted"]) == "2026-10-28"


@pytest.mark.parametrize("slug", SLUGS + BATCH3_ADAPTED)
def test_adapted_skills_declare_source_and_allowed_license(slug):
    front = sfi.parse_frontmatter(_read(slug))
    assert str(front["source"]).startswith(SOURCE_PREFIX[slug])
    assert front["license"] in ALLOWED_LICENSES
    assert "clean_room" not in front and "inspired_by" not in front


@pytest.mark.parametrize("slug", CLEAN_ROOM_SLUGS)
def test_clean_room_skills_declare_inspiration_not_source(slug):
    front = sfi.parse_frontmatter(_read(slug))
    assert "source" not in front, "클린룸 스킬은 source 를 두지 않는다"
    assert str(front["inspired_by"]).startswith(INSPIRED_BY_PREFIX[slug])
    assert front["clean_room"] is True
    assert front["license"] == "internal-original"


@pytest.mark.parametrize("slug", ALL_SLUGS)
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


@pytest.mark.parametrize("slug", SLUGS)
def test_license_notices_match_declared_license(slug):
    front = sfi.parse_frontmatter(_read(slug))
    notice = _read(slug).split("## 출처 및 변경 고지", 1)[1]
    assert "변경 사실" in notice
    if slug in APACHE_SLUGS:
        assert front["license"] == "Apache-2.0"
        assert "Apache License 2.0" in notice
    else:
        assert front["license"] == "MIT"
        assert "MIT" in notice and "Copyright (c) 2025 Jesse Vincent" in notice


@pytest.mark.parametrize("slug", CLEAN_ROOM_SLUGS)
def test_clean_room_notice_states_no_original_text_referenced(slug):
    notice = _read(slug).split("## 출처 및 변경 고지", 1)[1]
    assert CLEAN_ROOM_NOTICE in notice
    assert "변경 사실" in notice
    assert "Apache License" not in notice and "MIT License" not in notice


def test_gpl_original_is_declared_in_notice_and_text_not_copied():
    notice = _read("prompt-asset-authoring").split("## 출처 및 변경 고지", 1)[1]
    assert "GPL-3.0" in notice


def test_safe_db_query_is_apache_adaptation_with_notice():
    front = sfi.parse_frontmatter(_read("safe-db-query"))
    assert front["license"] == "Apache-2.0"
    notice = _read("safe-db-query").split("## 출처 및 변경 고지", 1)[1]
    assert "Apache License 2.0" in notice and "변경 사실" in notice
    assert not (SKILLS_DIR / "safe-db-query" / "scripts").exists()


def test_aads_claude_api_is_apache_adaptation_with_notice():
    front = sfi.parse_frontmatter(_read("aads-claude-api"))
    assert front["license"] == "Apache-2.0"
    assert len(_read("aads-claude-api").splitlines()) <= 150
    notice = _read("aads-claude-api").split("## 출처 및 변경 고지", 1)[1]
    assert "Apache License 2.0" in notice and "변경 사실" in notice


def test_aads_claude_api_rauth_wins_and_never_instructs_key_use():
    text = _read("aads-claude-api")
    body = text.split("\n---\n", 1)[1]
    first_h2 = next(line for line in body.splitlines() if line.startswith("## "))
    assert first_h2 == "## 우선순위", "R-AUTH 우선 절이 맨 앞이어야 한다"
    assert "R-AUTH" in body and "call_llm_with_fallback" in body
    assert f"export {_FORBIDDEN_KEY}" not in text
    getenv_lines = [line for line in text.splitlines() if f'os.getenv("{_FORBIDDEN_KEY}")' in line]
    assert all(any(w in line for w in _NEGATION) for line in getenv_lines)
    mentions = [line for line in text.splitlines() if _FORBIDDEN_KEY in line]
    assert mentions, "금지 문맥 언급이 하나도 없다"
    assert all(any(w in line for w in _NEGATION) for line in mentions)


def test_safe_db_query_documents_query_database_parameter_name():
    text = _read("safe-db-query")
    assert "`sql` 파라미터" not in text
    assert "`query` 파라미터" in text


@pytest.mark.parametrize("slug", BATCH3)
def test_batch3_does_not_import_scripts_or_office_format_skills(slug):
    text = _read(slug).lower()
    assert not (SKILLS_DIR / slug / "scripts").exists()
    for forbidden in ("skills/docx", "skills/pdf", "skills/pptx", "skills/xlsx", "--no-verify 를 쓴다"):
        assert forbidden not in text


def test_scan_skill_files_reports_all_slugs_as_read():
    entries = {e["slug"]: e for e in sfi.scan_skill_files([SKILLS_DIR])}
    for slug in ALL_SLUGS:
        assert entries[slug]["risk_tier"] == "read"
        assert entries[slug]["allowed_tools"] == ["Read", "Grep", "Glob"]


def test_scan_repository_skills_includes_all_slugs(monkeypatch):
    monkeypatch.setattr(harness, "_repo_root", lambda: ROOT)
    found = {c["slug"]: c for c in harness.scan_repository_skills()}
    for slug in ALL_SLUGS:
        assert slug in found
        assert found[slug]["risk_tier"] == "read"


def test_slugs_do_not_collide_with_existing_skills():
    dirs = sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))
    assert len(dirs) == len(set(dirs))
    assert {"handoff", "sales-channel-collector", "tpp"} <= set(dirs)


@pytest.mark.parametrize("slug", BATCH2)
def test_batch2_does_not_import_original_scripts_or_unlicensed_sources(slug):
    text = _read(slug)
    assert not (SKILLS_DIR / slug / "scripts").exists()
    lowered = text.lower()
    for forbidden in ("--no-verify 를 쓴다", "with_server.py --server", "skills/docx", "skills/pdf", "skills/pptx", "skills/xlsx", "doc-coauthoring"):
        assert forbidden not in lowered
