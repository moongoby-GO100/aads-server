"""문서 열람 참조·R-DOC 준수 판정의 공용 계약(AADS-RDOC-UNIFIED-STORAGE-20261006).

채팅 도구·러너 결과·서브에이전트가 "문서를 저장했다" 고 보고할 때 같은 모양을 쓰게 한다.
  - 원시 파일 경로를 웹 링크처럼 적지 않는다. 열리는 참조(view)만 링크로 쓴다.
  - 정본 DB 문서는 인증이 필요한 API 경로와 대시보드 '정본 문서' 탭 안내를 준다.
    대시보드 딥링크가 아직 없으므로 viewer_url 은 None 이다(거짓 링크를 만들지 않는다).
  - 새 문서가 정본에 등록되지 않았으면 '미완료' 를 명시한다(경고가 아니라 표지).
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional
from urllib.parse import quote

CANONICAL_TAB_HINT = "대시보드 '문서' > '정본 문서' 탭에서 문서 키로 찾기"
INCOMPLETE_MARK = "정본 미등록(미완료)"

# 새 문서로 취급하는 위치. 소스·테스트·스크립트 변경은 문서가 아니다.
_DOC_ROOTS = ("docs/", "reports/")
_DOC_EXTS = (".md", ".markdown", ".html", ".htm", ".txt")
# 정본 등록 대상이 아닌 자동 산출·인덱스 성격 위치.
_EXEMPT_PARTS = ("docs/shared-lessons/", "docs/knowledge/", "docs/_index", "reports/_", "/_todo/", "/evidence/")
_EXEMPT_NAMES = ("HANDOVER.md", "INDEX.md", "README.md", "CHANGELOG.md")

# 호스트·컨테이너 절대 경로의 저장소 루트. 이것만 떼어 저장소 기준 상대 경로로 맞춘다.
_ABS_ROOT_RE = re.compile(r"^(?:/root/aads/[A-Za-z0-9._-]+|/host/aads-server|/app)/")
_DATE_PREFIX_RE = re.compile(r"^\d{8}_")
_HANGUL_RE = re.compile(r"[ㄱ-ㆎ가-힣]")

RDOC_PROMPT_BLOCK = (
    "[R-DOC 문서 저장 규칙 — 모든 프로젝트 공통]\n"
    "- 새 문서·리포트는 화면 제목·첫 제목(H1)·파일명의 제목 부분을 한글로 쓰고, 영문 kebab-case document_key 를 붙인다.\n"
    "- 새 문서는 정본(project_document_heads)에 초안으로 등록한다. 기존 문서의 key·경로는 바꾸지 않는다.\n"
    "- 보고에는 파일 경로를 링크처럼 적지 말고, 등록 결과의 열람 참조(view/report_line)를 그대로 쓴다.\n"
    "  열람 참조를 못 받았으면 '정본 미등록(미완료)' 또는 '열람 링크 미제공'이라고 적는다.\n"
)

RDOC_BLOCK_MARKER = "[R-DOC 문서 저장 규칙"


def with_rdoc_block(instruction: str) -> str:
    """러너 지시서 저장본 끝에 R-DOC 블록을 한 번만 덧붙인다(멱등). 해시·쓰기 범위 계산은 원문으로 한다."""
    text = instruction or ""
    if RDOC_BLOCK_MARKER in text:
        return text
    return text.rstrip("\n") + "\n\n" + RDOC_PROMPT_BLOCK


def canonical_content_api_path(
    project: str, document_key: str, *, revision: Optional[int] = None, approved_only: bool = False
) -> str:
    """정본 본문을 DB 에서 읽는 API 경로(테넌트·프로젝트 권한 필요)."""
    path = f"/api/v1/projects/{quote(project.upper(), safe='')}/documents/{quote(document_key, safe='')}/content"
    query = []
    if revision is not None:
        query.append(f"revision={int(revision)}")
    if approved_only:
        query.append("approved_only=true")
    return path + ("?" + "&".join(query) if query else "")


def canonical_view_ref(
    project: str,
    document_key: str,
    *,
    title: Optional[str] = None,
    revision: Optional[int] = None,
    status: str = "draft",
    authoritative: bool = False,
) -> dict[str, Any]:
    """도구 반환에 붙이는 정본 열람 참조. viewer_url 은 딥링크가 생기기 전까지 None."""
    label = (title or "").strip() or document_key
    rev = f" rev{revision}" if revision is not None else ""
    state = "승인됨" if authoritative else ("초안" if status == "draft" else status)
    return {
        "kind": "canonical",
        "project": project.upper(),
        "document_key": document_key,
        "revision": revision,
        "title": title,
        "status": status,
        "authoritative": bool(authoritative),
        "api_path": canonical_content_api_path(project, document_key, revision=revision),
        "viewer_url": None,
        "viewer_hint": CANONICAL_TAB_HINT,
        "report_line": (
            f"정본 {state}: 「{label}」 (document_key={document_key}{rev}) — 열람: {CANONICAL_TAB_HINT}"
        ),
    }


def file_view_ref(abs_path: str, *, title: Optional[str] = None) -> dict[str, Any]:
    """파일 경로 → 대시보드 뷰어 링크. 뷰어가 못 여는 경로면 url=None 이고 '열람 링크 미제공'."""
    from app.services.session_documents import _view_url

    url = _view_url(abs_path)
    name = (title or "").strip() or abs_path.rsplit("/", 1)[-1]
    return {
        "kind": "file",
        "path": abs_path,
        "viewer_url": url,
        "report_line": f"[{name}]({url})" if url else f"{name} — 열람 링크 미제공",
    }


def _norm(path: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    m = _ABS_ROOT_RE.match(p)
    if m:
        p = p[m.end():]
    return p.lstrip("/")


def is_new_document_path(path: str) -> bool:
    """docs/·reports/ 아래의 날짜 접두 문서 파일이면 R-DOC 정본 등록 대상이다."""
    p = _norm(path)
    if not p.startswith(_DOC_ROOTS) or not p.lower().endswith(_DOC_EXTS):
        return False
    name = p.rsplit("/", 1)[-1]
    if name in _EXEMPT_NAMES:
        return False
    # 러너 결과의 changed_files 는 신규·수정을 구분하지 못한다. 새 문서 이름 규칙(YYYYMMDD_)을 가진
    # 파일만 대상으로 삼아 기존 문서 수정을 소급해 '미완료' 로 만들지 않는다(재현율보다 정밀도).
    if not _DATE_PREFIX_RE.match(name):
        return False
    return not any(part in "/" + p for part in _EXEMPT_PARTS)


def title_part(filename: str) -> str:
    """YYYYMMDD_{PROJECT}_{제목}.ext 에서 제목 부분만 뽑는다(없으면 확장자 뗀 이름)."""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = _DATE_PREFIX_RE.sub("", stem)
    head, sep, tail = stem.partition("_")
    return tail if sep and head.isupper() else stem


def evaluate_rdoc_compliance(
    changed_files: Iterable[str],
    registered_paths: Iterable[str] = (),
    *,
    added_files: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """러너/세션 결과의 R-DOC 준수 판정(순수 함수, I/O 없음).

    changed_files: 이번 작업이 쓴 파일(상대·절대 모두). added_files 를 따로 주면 신규 파일만
    '미등록' 판정에 쓰고(기존 문서 수정은 소급 차단하지 않는다), 없으면 changed_files 전체를 쓴다.
    미등록 → status='incomplete'. 파일명 한글 제목 미준수는 warnings 로만 둔다.
    """
    registered = {_norm(p) for p in registered_paths}
    registered_names = {p.rsplit("/", 1)[-1] for p in registered}
    candidates = [_norm(p) for p in (added_files if added_files is not None else changed_files)]
    docs = sorted({p for p in candidates if is_new_document_path(p)})
    missing = [p for p in docs if p not in registered and p.rsplit("/", 1)[-1] not in registered_names]
    warnings = [
        f"{p}: 파일명 제목 부분이 한글이 아님(신규 문서는 한글 제목 권장)"
        for p in docs
        if not _HANGUL_RE.search(title_part(p))
    ]
    if not docs:
        return {"status": "not_applicable", "documents": [], "missing": [], "warnings": [], "marker": None}
    status = "incomplete" if missing else "complete"
    return {
        "status": status,
        "documents": docs,
        "missing": missing,
        "warnings": warnings,
        "marker": INCOMPLETE_MARK if missing else None,
    }


def format_rdoc_lines(result: dict[str, Any], *, limit: int = 5) -> list[str]:
    """완료 보고에 덧붙일 줄. 대상이 아니면 빈 목록."""
    if not result or result.get("status") in (None, "not_applicable"):
        return []
    if result["status"] == "complete":
        return [f"R-DOC: 신규 문서 {len(result['documents'])}건 정본 등록 확인"]
    names = ", ".join(result["missing"][:limit])
    more = f" 외 {len(result['missing']) - limit}건" if len(result["missing"]) > limit else ""
    return [f"R-DOC: {INCOMPLETE_MARK} — {names}{more}. 정본(draft) 등록 전에는 완료로 보고하지 않는다."]


def _iter_files(value: Any) -> list[str]:
    import json

    raw = value
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
    if isinstance(raw, dict):
        raw = raw.get("files") or raw.get("changed_files") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            path = item.get("path") or item.get("file") or item.get("file_path")
            if isinstance(path, str):
                out.append(path)
    return out


async def job_rdoc_compliance(pool: Any, job_id: str) -> Optional[dict[str, Any]]:
    """러너 잡의 R-DOC 준수 판정. 같은 테넌트에 source_path 로 등록된 리비전만 '등록됨' 으로 본다.

    fail-open: 조회가 실패하면 None(보고 문구만 줄고 흐름은 막지 않는다).
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tenant_id::text AS tenant_id, actual_changed_files FROM pipeline_jobs WHERE job_id = $1",
                job_id,
            )
            if not row:
                return None
            files = _iter_files(row["actual_changed_files"])
            docs = sorted({_norm(f) for f in files if is_new_document_path(f)})
            if not docs:
                return evaluate_rdoc_compliance(files, [])
            rows = await conn.fetch(
                "SELECT DISTINCT source_path FROM project_document_revisions "
                "WHERE tenant_id = $1::uuid AND source_path = ANY($2::text[])",
                row["tenant_id"], docs,
            )
        return evaluate_rdoc_compliance(files, [r["source_path"] for r in rows])
    except Exception:  # noqa: BLE001
        return None
