"""
프로젝트별 문서 통합 조회 API.
3개 서버(68/211/114)의 docs, reports 디렉토리를 스캔하여
프로젝트별로 분류된 문서 목록과 내용을 제공한다.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = structlog.get_logger()

# ── 캐시 ──
_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 300  # 5분
PERSISTENT_CACHE_FILE = Path(os.getenv("PROJECT_DOCS_CACHE_FILE", "/tmp/aads_project_docs_cache.json"))

# ── 서버/프로젝트 경로 매핑 ──
SERVER_CONFIG = {
    "AADS": {
        "host": None,  # 로컬
        "paths": [
            {"base": "/app/docs", "label": "서버 문서"},
            {"base": "/app/reports", "label": "서버 리포트"},
            {"base": "/root/aads/aads-docs/docs", "label": "공용 문서"},
            {"base": "/root/aads/aads-docs/reports", "label": "공용 리포트", "exclude": ["ceo-documents/_index.json"]},
            {"base": "/root/aads/aads-dashboard/docs", "label": "대시보드 문서"},
            {"base": "/root/aads/aads-dashboard/reports", "label": "대시보드 리포트"},
            {"base": "/root/aads/aads-core/docs", "label": "코어 문서"},
            {"base": "/root/aads/aads-core/reports", "label": "코어 리포트"},
        ],
    },
    "KIS": {
        "host": "server-211",
        "paths": [
            {"base": "/root/kis-autotrade-v4/docs", "label": "문서",
             "exclude": ["kis-api-portal", "GO100", "go100"]},
        ],
    },
    "GO100": {
        "host": "server-211",
        "paths": [
            {"base": "/root/kis-autotrade-v4/report", "label": "리포트"},
            {"base": "/root/kis-autotrade-v4/reports", "label": "리포트"},
            {"base": "/root/kis-autotrade-v4/docs/go100", "label": "문서"},
            {"base": "/root/kis-autotrade-v4/docs/technical", "label": "기술문서"},
            {"base": "/root/kis-autotrade-v4/docs", "label": "문서",
             "include": ["GO100", "go100"], "exclude": ["go100/", "technical/"]},
        ],
    },
    "SF": {
        "host": "server-114",
        "paths": [
            {"base": "/data/shortflow/docs", "label": "서비스 문서"},
        ],
    },
    "NTV2": {
        "host": "server-114",
        "paths": [
            {"base": "/srv/newtalk-v2/docs", "label": "서비스 문서"},
        ],
    },
}

EXTENSIONS = {".md", ".txt", ".html", ".json", ".yaml", ".yml", ".py", ".sh", ".sql"}


def _load_persistent_cache() -> Optional[dict]:
    """프로세스 재시작 후에도 이전 문서 목록을 즉시 재사용한다."""
    if _cache["data"]:
        return _cache["data"]
    try:
        if not PERSISTENT_CACHE_FILE.exists():
            return None
        data = json.loads(PERSISTENT_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("status") != "ok":
            return None
        _cache["data"] = data
        _cache["ts"] = int(data.get("scanned_at") or 0)
        return data
    except Exception as e:
        logger.warning("project_docs_cache_load_failed", path=str(PERSISTENT_CACHE_FILE), error=str(e))
        return None


def _save_persistent_cache(data: dict) -> None:
    """스캔 결과를 파일 캐시에 저장한다. 실패해도 API 응답은 유지한다."""
    try:
        PERSISTENT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSISTENT_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("project_docs_cache_save_failed", path=str(PERSISTENT_CACHE_FILE), error=str(e))


def _file_key(doc: dict) -> tuple[str, str]:
    return (doc.get("base_path", ""), doc.get("path", ""))


def _file_signature(doc: dict) -> tuple[int, int]:
    return (int(doc.get("size") or 0), int(doc.get("modified") or 0))


def _previous_project(previous: Optional[dict], project: str) -> Optional[dict]:
    if not previous:
        return None
    for item in previous.get("projects") or []:
        if item.get("project") == project:
            return item
    return None


def _attach_delta(current: dict, previous: Optional[dict]) -> dict:
    """기존 목록과 비교해 이번 스캔에서 실제 변경된 파일 수를 표시한다."""
    prev_files = previous.get("files", []) if previous else []
    prev_map = {_file_key(doc): doc for doc in prev_files}
    curr_map = {_file_key(doc): doc for doc in current.get("files", [])}

    new_count = 0
    updated_count = 0
    unchanged_count = 0
    for key, doc in curr_map.items():
        prev_doc = prev_map.get(key)
        if not prev_doc:
            new_count += 1
        elif _file_signature(prev_doc) != _file_signature(doc):
            updated_count += 1
        else:
            unchanged_count += 1

    current["delta"] = {
        "new": new_count,
        "updated": updated_count,
        "removed": max(0, len(prev_map) - len(curr_map.keys() & prev_map.keys())),
        "unchanged": unchanged_count,
    }
    return current


async def _run_cmd(cmd: list[str], timeout: float = 10) -> str:
    """subprocess 실행 헬퍼."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace") if stdout else ""
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning("cmd_failed", cmd=cmd[:3], error=str(e))
        return ""


async def _scan_local(base: str, exclude: list[str] | None = None, include: list[str] | None = None) -> list[dict]:
    """로컬 파일시스템 스캔."""
    results = []
    base_path = Path(base)
    if not base_path.exists():
        return results
    for p in sorted(base_path.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in EXTENSIONS:
            continue
        rel = str(p.relative_to(base_path))
        if exclude and any(ex in rel for ex in exclude):
            continue
        if include and not any(inc in p.name for inc in include):
            continue
        stat = p.stat()
        results.append({
            "name": p.name,
            "path": rel,
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
            "type": _classify(p.name, rel),
        })
    return results


async def _scan_remote(host: str, base: str, exclude: list[str] | None = None, include: list[str] | None = None) -> list[dict]:
    """SSH로 원격 서버 스캔."""
    ext_pattern = " -o ".join(f'-name "*.{ext.lstrip(".")}"' for ext in EXTENSIONS)
    find_cmd = f'find {base} -type f \\( {ext_pattern} \\) -printf "%P\\t%s\\t%T@\\n" 2>/dev/null'
    output = await _run_cmd(["ssh", "-o", "ConnectTimeout=5", host, find_cmd], timeout=15)
    if not output.strip():
        return []
    results = []
    for line in output.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel_path, size_str, mtime_str = parts[0], parts[1], parts[2]
        if exclude and any(ex in rel_path for ex in exclude):
            continue
        name = rel_path.rsplit("/", 1)[-1] if "/" in rel_path else rel_path
        if include and not any(inc in name for inc in include):
            continue
        results.append({
            "name": name,
            "path": rel_path,
            "size": int(size_str) if size_str.isdigit() else 0,
            "modified": int(float(mtime_str)) if mtime_str else 0,
            "type": _classify(name, rel_path),
        })
    return sorted(results, key=lambda x: x["path"])


def _classify(name: str, path: str) -> str:
    """문서 유형 분류."""
    nl = name.lower()
    pl = path.lower()

    if any(key in pl for key in ("ceo-documents", "directive", "directives", "policy", "rule")) or \
            any(key in nl for key in ("directive", "directives", "policy", "rules")):
        return "directive"
    if "handover" in nl or "handover" in pl:
        return "handover"
    if any(key in pl for key in ("changelog", "release-note", "release_note", "history")) or \
            any(key in nl for key in ("changelog", "release-note", "release_note", "history")):
        return "changelog"
    if any(key in pl for key in ("report", "result", "retrospective", "postmortem")) or \
            any(key in nl for key in ("report", "result", "retrospective", "postmortem")):
        return "report"
    if any(key in pl for key in ("qa", "test", "verification", "benchmark")) or \
            any(key in nl for key in ("qa", "test", "verification", "benchmark")):
        return "qa"
    if any(key in pl for key in ("api", "openapi", "swagger")) or \
            any(key in nl for key in ("api", "openapi", "swagger")):
        return "api"
    if any(key in pl for key in ("architecture", "system-design", "design", "technical", "tech")) or \
            any(key in nl for key in ("architecture", "design", "technical", "tech")):
        return "architecture"
    if any(key in pl for key in ("runbook", "deploy", "deployment", "operation", "ops", "playbook", "troubleshoot")) or \
            any(key in nl for key in ("runbook", "deploy", "deployment", "operation", "ops", "playbook", "troubleshoot")):
        return "runbook"
    if any(key in pl for key in ("plan", "roadmap", "proposal", "spec", "prd", "layout")) or \
            any(key in nl for key in ("plan", "roadmap", "proposal", "spec", "prd")):
        return "plan"
    if any(key in pl for key in ("status", "incident", "issue", "summary")) or \
            any(key in nl for key in ("status", "incident", "issue", "summary")):
        return "status"
    if any(key in pl for key in ("lesson", "knowledge", "guide", "manual", "faq", "tutorial")) or \
            any(key in nl for key in ("lesson", "knowledge", "guide", "manual", "faq", "tutorial")):
        return "knowledge"
    if nl.endswith(".sql") or any(key in pl for key in ("schema", "migration", "erd", "ddl")) or \
            any(key in nl for key in ("schema", "migration", "erd", "ddl")):
        return "schema"
    if nl.endswith((".py", ".sh")):
        return "script"
    if nl.endswith((".json", ".yaml", ".yml")) or \
            any(key in pl for key in ("config", "settings", "compose", "env")) or \
            any(key in nl for key in ("config", "settings", "compose", "env")):
        return "config"
    return "doc"


async def _scan_project(project: str, config: dict, previous: Optional[dict] = None) -> dict:
    """프로젝트 1개 스캔."""
    host = config["host"]
    all_docs = []
    for path_cfg in config["paths"]:
        base = path_cfg["base"]
        exclude = path_cfg.get("exclude")
        include = path_cfg.get("include")
        label = path_cfg["label"]
        if host is None:
            docs = await _scan_local(base, exclude, include)
        else:
            docs = await _scan_remote(host, base, exclude, include)
        for d in docs:
            d["base_path"] = base
            d["label"] = label
            d["full_path"] = f"{base.rstrip('/')}/{d['path']}"
        all_docs.extend(docs)

    # 같은 실파일이 여러 base_path에서 중복 노출되지 않도록 정규화 dedupe
    deduped = []
    seen = set()
    for d in all_docs:
        key = (d.get("base_path", ""), d.get("path", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)
    return _attach_delta({
        "project": project,
        "host": host or "localhost",
        "total": len(deduped),
        "files": deduped,
    }, previous)


@router.get("/project-docs/scan")
async def scan_all_docs(force: bool = Query(False, description="캐시 무시하고 재스캔")):
    """전 서버 문서 스캔.

    - 일반 호출: 5분 메모리 캐시 우선, 프로세스 재시작 후에는 파일 캐시 우선.
    - 강제 호출: 기존 캐시와 비교해 new/updated/removed만 delta로 표시한다.
    """
    now = time.time()
    previous = _load_persistent_cache()
    if not force and previous and (now - _cache["ts"]) < CACHE_TTL:
        resp = dict(previous)
        resp["cache_hit"] = True
        resp["cache_age_sec"] = int(now - _cache["ts"])
        resp["cache_mode"] = "memory" if _cache["data"] is previous else "file"
        return resp

    tasks = [_scan_project(proj, cfg, _previous_project(previous, proj)) for proj, cfg in SERVER_CONFIG.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    projects = []
    total = 0
    for r in results:
        if isinstance(r, Exception):
            logger.error("scan_error", error=str(r))
            continue
        projects.append(r)
        total += r["total"]

    resp = {
        "status": "ok",
        "total": total,
        "projects": projects,
        "scanned_at": int(now),
        "cache_hit": False,
        "cache_mode": "incremental" if previous else "full",
        "delta": {
            "new": sum((p.get("delta") or {}).get("new", 0) for p in projects),
            "updated": sum((p.get("delta") or {}).get("updated", 0) for p in projects),
            "removed": sum((p.get("delta") or {}).get("removed", 0) for p in projects),
            "unchanged": sum((p.get("delta") or {}).get("unchanged", 0) for p in projects),
        },
    }
    _cache["data"] = resp
    _cache["ts"] = now
    _save_persistent_cache(resp)
    return resp


@router.get("/project-docs/content")
async def get_doc_content(
    project: str = Query(..., description="프로젝트명 (AADS/KIS/GO100/SF/NTV2)"),
    base_path: str = Query(..., description="base_path (스캔 결과에서 제공)"),
    file_path: str = Query(..., description="파일 상대 경로"),
):
    """문서 내용 조회."""
    config = SERVER_CONFIG.get(project)
    if not config:
        raise HTTPException(400, f"Unknown project: {project}")

    # 경로 검증 (traversal 방지)
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(400, "Invalid file path")

    full_path = f"{base_path}/{file_path}"
    host = config["host"]

    if host is None:
        # 로컬 파일
        p = Path(full_path)
        if not p.exists() or not p.is_file():
            raise HTTPException(404, "File not found")
        if p.stat().st_size > 1_000_000:
            raise HTTPException(413, "File too large (>1MB)")
        content = p.read_text(encoding="utf-8", errors="replace")
    else:
        # 원격 파일
        content = await _run_cmd(
            ["ssh", "-o", "ConnectTimeout=5", host, f"cat '{full_path}'"],
            timeout=10,
        )
        if not content:
            raise HTTPException(404, "File not found or empty")

    return {
        "project": project,
        "file_path": file_path,
        "full_path": full_path,
        "content": content,
        "size": len(content),
    }
