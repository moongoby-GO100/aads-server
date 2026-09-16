"""ChromaDB 를 임베디드(PersistentClient)로만 쓴다는 것을 코드로 고정한다.

배경 (2026-09-17). OSV.dev 점검에서 chromadb 1.5.9 의 CRITICAL 2건이 나왔는데
**고쳐진 버전이 없다** — 1.5.9 가 PyPI 최신(2026-05-05)이고 advisory 는
`last_affected: 1.5.9` 로 끝난다. 올릴 곳이 없으므로 버전 상향으로는 못 막는다.

두 CVE 모두 성립 조건이 같다:

    CVE-2026-45829 (GHSA-f4j7-r4q5-qw2c, pre-auth)
    CVE-2026-45833 (GHSA-36p7-vc44-83pf, authenticated)
      → ChromaDB **HTTP 서버**의 /api/v2/.../collections 엔드포인트에
        trust_remote_code=true 로 악성 모델 저장소를 보낼 때 원격 코드 실행

AADS 는 `chromadb.PersistentClient` 로 로컬 파일만 쓰고 HTTP 서버를 띄우지
않는다(2026-09-17 실측: chroma 프로세스 0개, 8000/8001 리슨 없음). 그래서 지금은
성립하지 않는다. **그러나 그것은 지금 코드가 그렇다는 뜻일 뿐이다.**

R-ERRBOOK 3항: "두 번 이상 어긴 규칙은 코드 차단으로 옮긴다." 문서에 "HttpClient
쓰지 마라" 라고 적어두면 다음 세션이 성능이나 편의를 이유로 바꾸고, 그 순간
CRITICAL 2건이 실제 노출된다. 노출 조건 자체를 테스트로 고정한다.

이 테스트가 깨지면 버전을 올리는 것으로는 해결되지 않는다 —
아직 패치가 없기 때문이다. 아래 둘 중 하나를 해야 한다.
  1. 변경을 되돌린다(권장).
  2. HTTP 서버가 꼭 필요하면 인증·네트워크 격리를 먼저 설계하고 CEO 승인을 받는다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = ["app", "scripts"]

# 노출 조건 1: 원격 코드 신뢰 플래그
TRUST_REMOTE_CODE = re.compile(r"trust_remote_code")
# 노출 조건 2: chroma HTTP 클라이언트/서버
CHROMA_HTTP = re.compile(r"chromadb\.HttpClient|chromadb\.AsyncHttpClient|chroma\s+run\b")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts or p.name.endswith(".bak_aads"):
                continue
            files.append(p)
    return files


def _hits(pattern: re.Pattern[str]) -> list[str]:
    found: list[str] = []
    for path in _python_files():
        if path == Path(__file__):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not pattern.search(text):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                rel = path.relative_to(REPO)
                found.append(f"{rel}:{i}: {line.strip()[:120]}")
    return found


def test_no_trust_remote_code():
    """trust_remote_code 는 두 CVE 의 공통 트리거다. 어디에도 없어야 한다."""
    hits = _hits(TRUST_REMOTE_CODE)
    assert not hits, (
        "trust_remote_code 사용 발견 — chromadb CVE-2026-45829/45833 의 트리거다.\n"
        + "\n".join(hits)
    )


def test_no_chromadb_http_client():
    """chroma HTTP 서버 경로를 쓰면 pre-auth RCE 가 성립한다. 패치본이 없다."""
    hits = _hits(CHROMA_HTTP)
    assert not hits, (
        "chromadb HTTP 클라이언트/서버 사용 발견 — 고쳐진 버전이 없는 상태에서\n"
        "CVE-2026-45829(pre-auth RCE) 노출 조건이 성립한다.\n" + "\n".join(hits)
    )


def test_code_indexer_uses_persistent_client():
    """임베디드 사용이 유지되는지 양성 확인. 부정 검사만으로는 삭제를 못 잡는다."""
    target = REPO / "app" / "services" / "code_indexer_service.py"
    if not target.exists():
        pytest.skip("code_indexer_service.py 없음")
    text = target.read_text(encoding="utf-8", errors="ignore")
    if "chromadb" not in text:
        pytest.skip("code_indexer_service.py 가 더 이상 chromadb 를 쓰지 않음")
    assert "PersistentClient" in text, (
        "code_indexer_service.py 가 PersistentClient 를 쓰지 않는다 — "
        "임베디드 사용 전제가 깨졌다."
    )
