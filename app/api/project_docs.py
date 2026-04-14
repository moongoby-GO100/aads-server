"""
프로젝트별 문서 통합 조회 API.
3개 서버(68/211/114)의 docs, reports 디렉토리를 스캔하여
프로젝트별로 분류된 문서 목록과 내용을 제공한다.
"""
from __future__ import annotations

import asyncio
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

# ── 서버/프로젝트 경로 매핑 ──
SERVER_CONFIG = {
    "AADS": {
        "host": None,  # 로컬
        "paths": [
            {"base": "/app/docs", "label": "문서"},
            {"base": "/app/reports", "label": "리포트"},
        ],
    },
    "KIS": {
        "host": "server-211",
        "paths": [
            {"base": "/root/kis-autotrade-v4/docs", "label": "문서",
             "exclude": ["kis-api-portal"]},
        ],
    },
    "GO100": {
        "host": "server-211",
        "paths": [
            {"base": "/root/kis-autotrade-v4/report", "label": "리포트"},
            {"base": "/root/kis-autotrade-v4/docs/go100", "label": "문서"},
            {"base": "/root/kis-autotrade-v4/docs/technical", "label": "기술문서"},
        ],
    },
    "SF": {
        "host": "server-114",
        "paths": [
            {"base": "/root/shortflow/docs", "label": "문서"},
            {"base": "/root/aads-hub/project-docs/shortflow", "label": "프로젝트 문서"},
        ],
    },
    "NTV2": {
        "host": "server-114",
        "paths": [
            {"base": "/root/newtalk-v2/docs", "label": "문서"},
            {"base": "/root/aads-hub/project-docs/newtalk-v2-api", "label": "프로젝트 문서"},
        ],
    },
}

EXTENSIONS = {".md", ".txt", ".html", ".json", ".yaml", ".yml", ".py", ".sh", ".sql"}


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


async def _scan_local(base: str, exclude: list[str] | None = None) -> list[dict]:
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
        stat = p.stat()
        results.append({
            "name": p.name,
            "path": rel,
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
            "type": _classify(p.name, rel),
        })
    return results


async def _scan_remote(host: str, base: str, exclude: list[str] | None = None) -> list[dict]:
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
    if "report" in pl or "report" in nl:
        return "report"
    if "spec" in nl or "architecture" in nl or "tech" in pl:
        return "tech"
    if "plan" in nl or "roadmap" in nl or "layout" in pl:
        return "plan"
    if "handover" in nl or "changelog" in nl:
        return "status"
    if "lesson" in pl or "knowledge" in pl:
        return "knowledge"
    if nl.endswith((".py", ".sh", ".sql")):
        return "code"
    return "doc"


async def _scan_project(project: str, config: dict) -> dict:
    """프로젝트 1개 스캔."""
    host = config["host"]
    all_docs = []
    for path_cfg in config["paths"]:
        base = path_cfg["base"]
        exclude = path_cfg.get("exclude")
        label = path_cfg["label"]
        if host is None:
            docs = await _scan_local(base, exclude)
        else:
            docs = await _scan_remote(host, base, exclude)
        for d in docs:
            d["base_path"] = base
            d["label"] = label
        all_docs.extend(docs)
    return {
        "project": project,
        "host": host or "localhost",
        "total": len(all_docs),
        "files": all_docs,
    }


@router.get("/project-docs/scan")
async def scan_all_docs(force: bool = Query(False, description="캐시 무시하고 재스캔")):
    """전 서버 문서 스캔 (캐시 5분)."""
    now = time.time()
    if not force and _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    tasks = [_scan_project(proj, cfg) for proj, cfg in SERVER_CONFIG.items()]
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
    }
    _cache["data"] = resp
    _cache["ts"] = now
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
        "content": content,
        "size": len(content),
    }
