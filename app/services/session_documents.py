"""이 대화가 만든 **문서 파일**을 여러 기록에서 모은다.

2026-09-15 대표님 지적: "오른쪽 아티팩트 문서파일 탭에 이 세션에서 만든
문서파일들이 보여야 하는데 안 보인다."

맞다. 그때까지 이 탭은 `chat_messages.tools_called` 의 tool_use 항목에서
`tool_input.file_path` 하나만 읽었다. 두 군데가 동시에 비어 있었다.

1. **tool_input 이 비어 저장된다.** 실측(2026-09-15, 최근 3일):
   intent=execute 인 턴의 tool_use 2,505건 중 **2,505건(100%)** 이
   `tool_input = {}` 였다. 같은 기간 status_check 는 3,133건 중 4건(0.1%).
   즉 write_remote_file 을 써도 경로가 남지 않는 턴이 있다.
2. **문서를 만드는 경로가 그 두 도구만이 아니다.** 실제로는
   `run_remote_command` 의 heredoc/tee, Pipeline Runner, 서브에이전트가
   만든다. 그 셋은 애초에 수집 대상이 아니었다.

그런데 **입력이 비어도 결과 본문에는 경로가 남아 있다** —
`[AADS 파일 쓰기 완료 — /root/.../x.html]`, `$ cat > /tmp/y.md`.
워크스페이스 원장(`chat_workspace_change_ledger`)과 러너의
`actual_changed_files` 도 같은 사실을 따로 적어 둔다.

**새 저장소를 만들지 않는다.** 이미 있는 세 곳을 합친다. tool_input 이
비는 문제 자체는 별건이고(`chat_service.py` 의 계측 블록이 추적 중),
그게 고쳐지면 이 모듈은 중복 없이 같은 항목을 한 번만 낸다.
"""

from __future__ import annotations

import functools
import logging
import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# ── 문서로 볼 확장자 ────────────────────────────────────────────────────────
DOC_EXT = {
    ".md", ".markdown", ".txt", ".html", ".htm", ".pdf", ".xlsx", ".xls",
    ".csv", ".tsv", ".docx", ".doc", ".hwp", ".hwpx", ".pptx", ".rtf",
}

ICON = {
    ".md": "📄", ".markdown": "📄", ".txt": "📄",
    ".html": "🌐", ".htm": "🌐", ".pdf": "📕",
    ".xlsx": "📊", ".xls": "📊", ".csv": "📊", ".tsv": "📊",
    ".docx": "📝", ".doc": "📝", ".hwp": "📝", ".hwpx": "📝",
    ".pptx": "📽", ".rtf": "📝",
}

SOURCE_LABEL = {
    "tool_input": "직접 저장",
    "tool_result": "명령 실행",
    "ledger": "작업 원장",
    "runner": "러너 산출물",
}

# ── /docs 뷰어가 열 수 있는 base_path ───────────────────────────────────────
# `app/api/project_docs.py` 의 SERVER_CONFIG["AADS"] + LOCAL_BASE_ALIASES 를
# **호스트 경로 → 뷰어가 받는 base_path** 방향으로 뒤집은 것이다.
# 여기에 없는 경로(예: 244 서버 /srv/biseo/…)는 링크를 만들지 않는다 —
# 열리지 않는 링크를 주는 것이 안 주는 것보다 나쁘다.
_VIEW_BASES: List[tuple[str, str]] = [
    ("/root/aads/aads-server/app/static/docs", "/app/app/static/docs"),
    ("/root/aads/aads-server/app/static/reports", "/app/app/static/reports"),
    ("/root/aads/aads-server/app/static/preview", "/app/app/static/preview"),
    ("/root/aads/aads-server/app/static/gallery", "/app/app/static/gallery"),
    ("/root/aads/aads-server/docs", "/app/docs"),
    ("/root/aads/aads-server/reports", "/app/reports"),
    ("/root/aads/aads-server/app", "/app/app"),
    # 운영 컨테이너에서 도구가 보는 최신 문서 마운트. 뷰어(project_docs)가 이 경로를 먼저 읽는다.
    ("/host/aads-server/docs", "/app/docs"),
    ("/host/aads-server/reports", "/app/reports"),
    ("/app/app/static/docs", "/app/app/static/docs"),
    ("/app/app/static/reports", "/app/app/static/reports"),
    ("/app/app/static/preview", "/app/app/static/preview"),
    ("/app/app/static/gallery", "/app/app/static/gallery"),
    ("/app/docs", "/app/docs"),
    ("/app/reports", "/app/reports"),
    ("/app/app", "/app/app"),
    ("/root/aads/aads-dashboard/public/reports", "/root/aads/aads-dashboard/public/reports"),
    ("/root/aads/aads-dashboard/public/exports", "/root/aads/aads-dashboard/public/exports"),
    ("/root/aads/aads-dashboard/docs", "/root/aads/aads-dashboard/docs"),
    ("/root/aads/aads-dashboard/reports", "/root/aads/aads-dashboard/reports"),
    ("/root/aads/aads-dashboard/src", "/root/aads/aads-dashboard/src"),
    ("/root/aads/aads-docs/docs", "/root/aads/aads-docs/docs"),
    ("/root/aads/aads-docs/reports", "/root/aads/aads-docs/reports"),
    ("/root/aads/aads-core/docs", "/root/aads/aads-core/docs"),
    ("/root/aads/aads-core/reports", "/root/aads/aads-core/reports"),
]
_VIEW_BASES_SORTED = sorted(_VIEW_BASES, key=lambda x: len(x[0]), reverse=True)

# 저장소 이름 → 호스트 루트. 원장·도구가 남기는 상대 경로를 절대 경로로 편다.
_REPO_ROOTS = {
    "aads-server": "/root/aads/aads-server",
    "aads-dashboard": "/root/aads/aads-dashboard",
    "aads-docs": "/root/aads/aads-docs",
    "aads-core": "/root/aads/aads-core",
    "default": "/root/aads/aads-server",
}

# AADS 저장소 루트 바로 아래에 있는 디렉터리. 상대 경로의 첫 조각이 이 중
# 하나일 때만 aads-server 기준으로 편다. 아니면 상대 경로 그대로 둔다 —
# 엉뚱한 루트를 붙이면 "있지도 않은 파일"을 링크로 주게 된다.
_AADS_TOP = ("app", "docs", "reports", "scripts", "tests", "migrations")

# ── 결과 본문에서 경로를 뽑는 정규식 ───────────────────────────────────────
# 중첩 반복을 쓰지 않는다(R-BG 3항). 전부 한 줄짜리 단순 패턴이다.
_EXT_ALT = "md|markdown|txt|html|htm|pdf|xlsx|xls|csv|tsv|docx|doc|hwp|hwpx|pptx|rtf"

# write_remote_file / patch_remote_file 이 결과에 직접 적는 경로.
_MARK_RE = re.compile(r"파일 (?:쓰기|패치) 완료 — ([^\]\n]+)\]")
# 셸 리다이렉트와 tee. 확장자가 문서일 때만 받는다.
_REDIRECT_RE = re.compile(r"\s>>?\s*['\"]?([^\s'\"<>|;&`]+\.(?:" + _EXT_ALT + r"))\b")
_TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?['\"]?([^\s'\"<>|;&`]+\.(?:" + _EXT_ALT + r"))\b")
# 러너 완료 메시지에 붙는 git diffstat 한 줄: ` 작업/결과/x/index.html | 683 +++`
_DIFFSTAT_RE = re.compile(
    r"^\s*([^\s|]+\.(?:" + _EXT_ALT + r"))\s+\|\s+\d+", re.MULTILINE
)
_JOB_ID_RE = re.compile(r"runner-[0-9a-f]{6,}")

# 스크래치 경로. 여기 쓴 것은 검증용 임시 파일이지 대표님께 드릴 문서가
# 아니다. 세지만 목록에는 올리지 않는다 — 몇 개를 뺐는지는 알린다.
_SCRATCH_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/")


def _ext_of(path: str) -> str:
    tail = path.rsplit("/", 1)[-1]
    if "." not in tail:
        return ""
    return "." + tail.rsplit(".", 1)[-1].lower()


def _looks_like_path(path: str) -> bool:
    """셸 변수·와일드카드가 섞인 조각은 경로가 아니다."""
    if not path or len(path) > 400:
        return False
    if any(ch in path for ch in ("$", "*", "`", "\n", "\t")):
        return False
    return True


def _absolutize(path: str, repo: str = "") -> str:
    p = path.replace("\\", "/").strip().strip("'\"")
    if not p:
        return ""
    if p.startswith("/"):
        return str(PurePosixPath(p))
    if p.startswith("./"):
        p = p[2:]
    root = _REPO_ROOTS.get(repo, "")
    if root:
        return f"{root}/{p}"
    first = p.split("/", 1)[0]
    if first in _AADS_TOP:
        return f"{_REPO_ROOTS['aads-server']}/{p}"
    return p


def _view_url(abs_path: str) -> Optional[str]:
    if not abs_path.startswith("/"):
        return None
    p = str(PurePosixPath(abs_path))
    for host_prefix, base in _VIEW_BASES_SORTED:
        if p.startswith(host_prefix + "/"):
            rel = p[len(host_prefix) + 1:]
            if not rel or ".." in rel.split("/"):
                return None
            return "/docs?" + urlencode(
                {"project": "AADS", "base_path": base, "file_path": rel}
            )
    # 원격 서버 문서(예: NTV2 114 의 /srv/newtalk-v2/docs). 2026-10-06 대표님 지적
    # "오른쪽 아티팩트에서 문서가 안 열린다" — 뷰어는 SSH 로 열 수 있는데
    # 위 표가 AADS 경로뿐이라 링크가 비어 있었다.
    for base, project in _remote_view_bases():
        if p.startswith(base + "/"):
            rel = p[len(base) + 1:]
            if not rel or ".." in rel.split("/"):
                return None
            return "/docs?" + urlencode(
                {"project": project, "base_path": base, "file_path": rel}
            )
    return None


@functools.lru_cache(maxsize=1)
def _remote_view_bases() -> tuple:
    """원격 문서 루트 → 뷰어 project. 정본은 `project_docs.SERVER_CONFIG` 다.

    여기에 따로 적으면 두 벌이 되어 한쪽이 낡는다. 뷰어 모듈을 못 읽으면
    링크를 만들지 않는다 — 열리지 않는 링크보다 없는 링크가 낫다.
    """
    try:
        from app.api.project_docs import SERVER_CONFIG
    except Exception as exc:
        logger.warning("session_documents remote view bases unavailable: %s", exc)
        return ()
    pairs: List[tuple[str, str]] = []
    for project, cfg in SERVER_CONFIG.items():
        if not cfg.get("host"):
            continue
        for path_cfg in cfg.get("paths", []):
            pairs.append((str(PurePosixPath(path_cfg["base"])), project))
    return tuple(sorted(pairs, key=lambda x: len(x[0]), reverse=True))


class _Collector:
    """경로별로 한 줄씩 모은다. 같은 파일이 여러 기록에 나오면 합친다."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.others = 0
        self._other_seen: set[str] = set()

    def add(
        self,
        raw_path: str,
        *,
        source: str,
        tool: str = "",
        at: Any = None,
        repo: str = "",
        writes: int = 1,
    ) -> None:
        if not _looks_like_path(raw_path):
            return
        path = _absolutize(raw_path, repo)
        if not path:
            return
        ext = _ext_of(path)
        if ext in DOC_EXT and path.startswith(_SCRATCH_PREFIXES):
            if path not in self._other_seen:
                self._other_seen.add(path)
                self.others += 1
            return
        if ext not in DOC_EXT:
            if path not in self._other_seen:
                self._other_seen.add(path)
                self.others += 1
            return

        at_iso = at.isoformat() if hasattr(at, "isoformat") else (at or None)
        cur = self.items.get(path)
        if cur is None:
            self.items[path] = {
                "path": path,
                "name": path.rsplit("/", 1)[-1],
                "dir": path.rsplit("/", 1)[0] if "/" in path else "",
                "icon": ICON.get(ext, "📄"),
                "at": at_iso,
                "writes": max(1, int(writes or 1)),
                "tool": tool or source,
                "source": source,
                "source_label": SOURCE_LABEL.get(source, source),
                "repo": repo or "",
                "view_url": _view_url(path),
            }
            return

        cur["writes"] = cur["writes"] + max(1, int(writes or 1))
        if at_iso and (not cur["at"] or at_iso > cur["at"]):
            cur["at"] = at_iso
            if tool:
                cur["tool"] = tool
        if repo and not cur["repo"]:
            cur["repo"] = repo

    def result(self, limit: int) -> List[Dict[str, Any]]:
        rows = sorted(
            self.items.values(),
            key=lambda d: (d.get("at") or "", d.get("name") or ""),
            reverse=True,
        )
        return rows[:limit]


async def collect_session_documents(session_id: Any, limit: int = 80) -> Dict[str, Any]:
    """세션이 만든 문서 목록. 한 수집원이 실패해도 나머지는 낸다."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    col = _Collector()
    sid_text = str(session_id)
    used: List[str] = []

    # ① 도구 입력 — 원래 경로. 살아 있을 때가 가장 정확하다.
    try:
        rows = await pool.fetch(
            """
            SELECT COALESCE(
                       t->'tool_input'->>'file_path',
                       t->'tool_input'->>'path',
                       t->'tool_input'->>'target_path'
                   ) AS path,
                   t->>'tool_name' AS tool,
                   m.created_at AS at
            FROM chat_messages m,
                 LATERAL jsonb_array_elements(m.tools_called) t
            WHERE m.session_id = $1
              AND m.deleted_at IS NULL
              AND jsonb_typeof(m.tools_called) = 'array'
              AND t->>'type' = 'tool_use'
              AND t->>'tool_name' IN ('write_remote_file', 'patch_remote_file', 'export_data')
            ORDER BY m.created_at DESC
            LIMIT 600
            """,
            session_id,
        )
        hit = 0
        for r in rows:
            if r["path"]:
                hit += 1
                col.add(r["path"], source="tool_input", tool=r["tool"], at=r["at"])
        if hit:
            used.append("tool_input")
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_documents tool_input source failed: %s", exc)

    # ② 도구 결과 본문 — 입력이 비어도 경로는 여기 남는다.
    try:
        rows = await pool.fetch(
            """
            SELECT t->>'tool_name' AS tool,
                   t->>'content' AS content,
                   m.created_at AS at
            FROM chat_messages m,
                 LATERAL jsonb_array_elements(m.tools_called) t
            WHERE m.session_id = $1
              AND m.deleted_at IS NULL
              AND jsonb_typeof(m.tools_called) = 'array'
              AND t->>'type' = 'tool_result'
              AND t->>'content' IS NOT NULL
            ORDER BY m.created_at DESC
            LIMIT 1200
            """,
            session_id,
        )
        hit = 0
        for r in rows:
            content = r["content"] or ""
            if not content:
                continue
            found: List[str] = []
            found.extend(_MARK_RE.findall(content))
            found.extend(_REDIRECT_RE.findall(content))
            found.extend(_TEE_RE.findall(content))
            for p in found:
                hit += 1
                col.add(p, source="tool_result", tool=r["tool"] or "", at=r["at"])
        if hit:
            used.append("tool_result")
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_documents tool_result source failed: %s", exc)

    # ③ 워크스페이스 변경 원장 — 이 세션이 건드린 파일의 정본 기록.
    try:
        rows = await pool.fetch(
            """
            SELECT repo, file_path, source_tool,
                   COALESCE(last_modified_at, updated_at, created_at) AS at
            FROM chat_workspace_change_ledger
            WHERE session_id = $1
            ORDER BY COALESCE(last_modified_at, updated_at, created_at) DESC
            LIMIT 400
            """,
            sid_text,
        )
        if rows:
            used.append("ledger")
        for r in rows:
            col.add(
                r["file_path"] or "",
                source="ledger",
                tool=r["source_tool"] or "ledger",
                at=r["at"],
                repo=(r["repo"] or "").strip(),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_documents ledger source failed: %s", exc)

    # ④ Pipeline Runner 산출물 — 러너가 만든 문서도 이 대화의 산출물이다.
    try:
        rows = await pool.fetch(
            """
            SELECT job_id, project, actual_changed_files,
                   COALESCE(completed_at, created_at) AS at
            FROM pipeline_jobs
            WHERE chat_session_id = $1
              AND actual_changed_files IS NOT NULL
            ORDER BY COALESCE(completed_at, created_at) DESC
            LIMIT 80
            """,
            sid_text,
        )
        if rows:
            used.append("runner")
        for r in rows:
            for entry in _iter_changed_files(r["actual_changed_files"]):
                col.add(
                    entry,
                    source="runner",
                    tool=f"runner:{r['job_id']}",
                    at=r["at"],
                    repo="",
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_documents runner source failed: %s", exc)

    # ⑤ 러너 완료 메시지의 diffstat — 러너가 **다른 서버 저장소**에 만든
    #    산출물은 ④(actual_changed_files)로 안 잡히는 경우가 있다. 완료
    #    메시지는 이 세션에 남으므로 거기서 파일명을 읽는다.
    try:
        rows = await pool.fetch(
            """
            SELECT content, created_at AS at
            FROM chat_messages
            WHERE session_id = $1
              AND deleted_at IS NULL
              AND content LIKE '%Pipeline Runner%'
            ORDER BY created_at DESC
            LIMIT 80
            """,
            session_id,
        )
        hit = 0
        for r in rows:
            content = r["content"] or ""
            paths = _DIFFSTAT_RE.findall(content)
            if not paths:
                continue
            job_match = _JOB_ID_RE.search(content)
            job = job_match.group(0) if job_match else "runner"
            for p in paths:
                # git 이 줄인 경로(`.../폴더/파일.html`)는 앞을 떼고 보여준다.
                if p.startswith(".../"):
                    p = p[4:]
                hit += 1
                col.add(p, source="runner", tool=job, at=r["at"])
        if hit and "runner" not in used:
            used.append("runner")
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_documents runner_message source failed: %s", exc)

    docs = col.result(limit)

    # ⑥ 정본 등록 현황 — 이 세션이 등록했거나(source_session_id / chat:<sid8>),
    #    같은 테넌트에서 이 세션 문서와 같은 source_path 로 이미 등록된 리비전.
    #    조회는 세션 테넌트로 고정한다(다른 테넌트 행은 절대 섞이지 않는다).
    canonical_docs: List[Dict[str, Any]] = []
    doc_rel_paths = sorted({_repo_relative(d["path"]) for d in docs if _repo_relative(d["path"])})
    try:
        rows = await pool.fetch(
            """
            SELECT h.project_key, h.document_key, r.revision, r.title, r.source_path,
                   r.created_at AS at, (r.id = h.approved_revision_id) AS approved
            FROM project_document_revisions r
            JOIN project_document_heads h ON h.id = r.head_id
            WHERE r.tenant_id = (SELECT s.tenant_id FROM chat_sessions s WHERE s.id = $1)
              AND (r.source_session_id = $1
                   OR r.author_id = 'chat:' || left($1::text, 8)
                   OR r.source_path = ANY($2::text[]))
            ORDER BY r.created_at DESC
            LIMIT 100
            """,
            session_id,
            doc_rel_paths,
        )
        if rows:
            used.append("canonical")
        from app.services.document_refs import canonical_view_ref

        for r in rows:
            ref = canonical_view_ref(
                r["project_key"], r["document_key"], title=r["title"], revision=r["revision"],
                status="approved" if r["approved"] else "draft", authoritative=bool(r["approved"]),
            )
            canonical_docs.append({
                "project": r["project_key"],
                "document_key": r["document_key"],
                "revision": r["revision"],
                "title": r["title"],
                "source_path": r["source_path"],
                "approved": bool(r["approved"]),
                "at": r["at"].isoformat() if hasattr(r["at"], "isoformat") else r["at"],
                "view": ref,
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_documents canonical source failed: %s", exc)

    from app.services.document_refs import evaluate_rdoc_compliance

    rdoc = evaluate_rdoc_compliance(
        [d["path"] for d in docs],
        [c["source_path"] for c in canonical_docs if c.get("source_path")],
    )
    return {
        "documents": docs,
        "other_files": col.others,
        "sources_used": used,
        "canonical_documents": canonical_docs,
        "rdoc": rdoc,
    }


def _repo_relative(abs_path: str) -> str:
    """절대 경로 → 저장소 기준 상대 경로(docs/…, reports/…). 아니면 빈 문자열."""
    from app.services.document_refs import _norm, is_new_document_path

    rel = _norm(abs_path)
    return rel if is_new_document_path(rel) else ""


def _iter_changed_files(value: Any) -> List[str]:
    """actual_changed_files 는 문자열 배열일 때도, dict 배열일 때도 있다."""
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
    out: List[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            p = item.get("path") or item.get("file") or item.get("file_path")
            if isinstance(p, str):
                out.append(p)
    return out
