"""R-DOC 문서 3종(기획서·PRD·spec) 보존·내용 검증.

지키려는 것
  1. 기획서·PRD 는 DB 승인본 content 를 글자 그대로 앞에 두고 뒤에 절을 덧붙인 것이다.
     승인본 원문은 이 파일에 상수로 넣지 않는다 — DB(project_document_revisions)에서 읽는다.
     DB 에 닿지 못하는 환경(운영 이미지 임시 컨테이너 등)에서는 DB 대조만 건너뛰고,
     문서가 스스로 선언한 sha256 과 앞부분의 해시를 대조하는 오프라인 검사는 항상 돈다.
  2. spec 에 게이트 단계(off/shadow)와 fail-open 이 적혀 있다.
  3. enforce 는 모든 줄이 "미구현" 맥락이고, 차단·강제는 현재 동작처럼 쓰이지 않는다.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

PLAN = _REPO / "docs/plans/20261003_AADS_RDOC_STORAGE_RULE_PLAN.md"
PRD = _REPO / "docs/prd/20261003_AADS_RDOC_STORAGE_RULE_PRD.md"
SPEC = _REPO / "docs/specs/rdoc-storage-rule/spec.md"
THIS = Path(__file__).resolve()

# 승인본 revision id (원문이 아니라 식별자만 둔다)
PLAN_REVISION_ID = "6dfc2221-6219-4f75-bc11-47e505dd2b4c"
PRD_REVISION_ID = "686c817c-c69e-41d3-a89a-0cba301e447b"

APPENDIX_MARKER = "\n## R-DOC 문서 저장 규칙"
_DECLARED_SHA_RE = re.compile(r"승인본 원문은 글자 그대로이며 sha256 `([0-9a-f]{64})`")

# 차단·강제가 현재 동작이 아님을 보여 주는 표지
_NEGATION_MARKERS = ("않", "미구현", "아니", "없", "금지")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _approved_prefix(path: Path) -> bytes:
    data = path.read_bytes()
    idx = data.find(APPENDIX_MARKER.encode("utf-8"))
    assert idx > 0, f"{path.name}: 덧붙인 절 머리({APPENDIX_MARKER.strip()})가 없다"
    return data[:idx]


def _fetch_revision_content(revision_id: str) -> str | None:
    """DB 에서 승인본 content 를 읽는다. 닿지 못하면 None."""
    sql = "SELECT content FROM project_document_revisions WHERE id = %s"
    try:
        import psycopg2  # type: ignore

        conn = psycopg2.connect(connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (revision_id,))
                row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import asyncpg  # type: ignore

        async def _read() -> str | None:
            conn = await asyncpg.connect(timeout=5)
            try:
                return await conn.fetchval(
                    "SELECT content FROM project_document_revisions WHERE id = $1::uuid", revision_id
                )
            finally:
                await conn.close()

        return asyncio.run(asyncio.wait_for(_read(), 10))
    except Exception:
        return None


# ── 존재·파일명 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [PLAN, PRD, SPEC], ids=lambda p: p.name)
def test_three_docs_exist(path: Path):
    assert path.is_file(), f"{path} 없음"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("path", [PLAN, PRD, SPEC, THIS], ids=lambda p: p.name)
def test_paths_are_ascii(path: Path):
    """셸 러너가 비ASCII 경로의 신규 파일을 diff 에서 빠뜨렸다(2026-10-03)."""
    rel = str(path.relative_to(_REPO))
    assert rel.isascii(), rel


def test_versions_declared():
    assert "version 1.1.0" in PLAN.read_text(encoding="utf-8")
    assert "version 1.1.0" in PRD.read_text(encoding="utf-8")
    assert "version 1.0.0" in SPEC.read_text(encoding="utf-8")


# ── 승인본 보존 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [PLAN, PRD], ids=lambda p: p.name)
def test_prefix_matches_declared_sha256(path: Path):
    """오프라인: 문서가 선언한 승인본 sha256 == 덧붙인 절 앞부분의 sha256."""
    text = path.read_text(encoding="utf-8")
    m = _DECLARED_SHA_RE.search(text)
    assert m, f"{path.name}: 승인본 sha256 선언이 없다"
    assert _sha(_approved_prefix(path)) == m.group(1)


@pytest.mark.parametrize(
    "path,revision_id",
    [(PLAN, PLAN_REVISION_ID), (PRD, PRD_REVISION_ID)],
    ids=["plan", "prd"],
)
def test_prefix_matches_db_approved_content(path: Path, revision_id: str):
    """DB 승인본 content 의 sha256 == 파일 앞부분의 sha256 (원문 상수 없이 DB 에서 읽는다)."""
    content = _fetch_revision_content(revision_id)
    if content is None:
        pytest.skip(f"DB 에 닿지 못했다(PG* 환경 없음) — 오프라인 해시 검사만 유효: {revision_id}")
    expected = content.encode("utf-8")
    data = path.read_bytes()
    assert data.startswith(expected), f"{path.name}: 승인본 원문이 글자 그대로 앞에 있지 않다"
    assert _sha(_approved_prefix(path)) == _sha(expected)


# ── spec 내용 ─────────────────────────────────────────────────────────


def test_spec_documents_gate_modes_and_fail_open():
    text = SPEC.read_text(encoding="utf-8")
    assert re.search(r"\boff\b", text)
    assert re.search(r"\bshadow\b", text)
    assert "fail-open" in text
    assert "300ms" in text


def test_spec_documents_registration_procedure():
    text = SPEC.read_text(encoding="utf-8")
    for needle in ("canonical_documents.py", "document_key", "project_key", "kind", "kebab", "초안"):
        assert needle in text, needle


# ── enforce / 차단 / 강제 는 현재 동작처럼 쓰지 않는다 ─────────────────


def _appended_lines(path: Path) -> list[str]:
    data = path.read_bytes()
    idx = data.find(APPENDIX_MARKER.encode("utf-8"))
    body = data if idx < 0 else data[idx:]
    return body.decode("utf-8").splitlines()


@pytest.mark.parametrize("path", [PLAN, PRD, SPEC], ids=lambda p: p.name)
def test_every_enforce_mention_says_unimplemented(path: Path):
    lines = _appended_lines(path) if path != SPEC else SPEC.read_text(encoding="utf-8").splitlines()
    hits = [ln for ln in lines if "enforce" in ln.lower()]
    bad = [ln for ln in hits if "미구현" not in ln]
    assert not bad, f"{path.name}: '미구현' 없는 enforce 언급 {bad}"


def test_enforce_is_mentioned_somewhere():
    """검사가 빈 집합을 통과하지 않도록 — 세 문서 모두 enforce 를 미구현으로 명시한다."""
    for path in (PLAN, PRD, SPEC):
        lines = _appended_lines(path) if path != SPEC else SPEC.read_text(encoding="utf-8").splitlines()
        assert any("enforce" in ln.lower() for ln in lines), path.name


@pytest.mark.parametrize("path", [PLAN, PRD, SPEC], ids=lambda p: p.name)
def test_block_or_force_never_described_as_current_behavior(path: Path):
    lines = _appended_lines(path) if path != SPEC else SPEC.read_text(encoding="utf-8").splitlines()
    bad = [
        ln for ln in lines
        if ("차단" in ln or "강제" in ln) and not any(mk in ln for mk in _NEGATION_MARKERS)
    ]
    assert not bad, f"{path.name}: 현재 동작처럼 읽히는 차단/강제 {bad}"


def test_environment_does_not_leak_into_docs():
    """문서에 비밀값이 섞이지 않았다."""
    pw = os.getenv("PGPASSWORD")
    for path in (PLAN, PRD, SPEC):
        text = path.read_text(encoding="utf-8")
        assert "sk-ant-" not in text
        if pw:
            assert pw not in text
