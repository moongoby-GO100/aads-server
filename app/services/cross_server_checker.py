"""
AADS-181: 크로스 서버 디렉티브 체커
3대 서버에서 directives 폴더를 스캔하여 통합 현황을 반환한다.

- 서버 68: 로컬 파일 스캔
- 서버 211/114: SSH로 스캔, 실패 시 HTTP fallback
- 결과 캐싱: 30초 TTL (반복 SSH 호출 방지)
- 파싱: TASK_ID/TITLE/PRIORITY/MODEL/SIZE/project 필드 추출
"""
import asyncio
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

import structlog

from app.services.server_registry import SERVER_REGISTRY, ALL_STATUSES

logger = structlog.get_logger()

KST = timezone(timedelta(hours=9))

# ─── 캐시 (30초 TTL) ─────────────────────────────────────────────────────────
_cache: Dict[str, Any] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 30.0


def _cache_valid() -> bool:
    return time.monotonic() - _cache_ts < _CACHE_TTL


def _set_cache(data: Dict[str, Any]) -> None:
    global _cache, _cache_ts
    _cache = data
    _cache_ts = time.monotonic()


# ─── SSH 유틸 ─────────────────────────────────────────────────────────────────

async def _run_local_cmd(cmd: str, timeout: float = 5.0) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        return ""
    except Exception as e:
        return f"(error: {e})"


async def _run_ssh_cmd(host: str, cmd: str, timeout: float = 8.0) -> str:
    ssh_cmd = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes root@{host} {repr(cmd)}"
    return await _run_local_cmd(ssh_cmd, timeout=timeout)


# ─── 파싱 유틸 ───────────────────────────────────────────────────────────────

_FIELD_RE = re.compile(r"^([A-Z_]+):\s*(.+)$", re.MULTILINE)

# task_id prefix → project 추측
_PREFIX_TO_PROJECT = {
    "AADS": "AADS", "KIS": "KIS", "GO100": "GO100",
    "SF": "SF", "NTV2": "NTV2", "NAS": "NAS",
    "NT": "NTV2",
}


def _guess_project_from_task_id(task_id: str) -> str:
    """AADS-123 → AADS, KIS-41 → KIS 형식에서 프로젝트 추측."""
    m = re.match(r"^([A-Z][A-Z0-9]+)-\d+$", task_id or "")
    if m:
        prefix = m.group(1)
        return _PREFIX_TO_PROJECT.get(prefix, prefix)
    return "UNKNOWN"


def _parse_directive_content(content: str, filename: str, status: str, server_id: str) -> Dict[str, Any]:
    """디렉티브 파일 내용에서 메타데이터 파싱."""
    fields: Dict[str, str] = {}
    for m in _FIELD_RE.finditer(content):
        key = m.group(1).strip()
        val = m.group(2).strip().rstrip("|").strip()
        fields[key] = val

    task_id = fields.get("TASK_ID", "").strip()
    title = fields.get("TITLE", fields.get("title", "")).strip()
    priority = fields.get("PRIORITY", fields.get("priority", "")).strip()
    size = fields.get("SIZE", "").strip()
    model = fields.get("MODEL", "").strip()
    project = fields.get("project", "").strip() or fields.get("PROJECT", "").strip()

    # project 미지정 시 task_id prefix 기반 추측 (서버별 기본값 fallback)
    if not project and task_id:
        project = _guess_project_from_task_id(task_id)
    if not project:
        # 서버별 기본 프로젝트
        srv_cfg = SERVER_REGISTRY.get(server_id, {})
        projs = srv_cfg.get("projects", [])
        project = projs[0] if projs else "UNKNOWN"

    # 파일명에서 타임스탬프 추출 (AADS_20260308_124306_BRIDGE.md 형식)
    started_at = None
    ts_match = re.search(r"(\d{8}_\d{6})", filename)
    if ts_match:
        try:
            dt = datetime.strptime(ts_match.group(1), "%Y%m%d_%H%M%S")
            dt = dt.replace(tzinfo=KST)
            started_at = dt.isoformat()
        except Exception:
            pass

    return {
        "task_id": task_id or filename.replace(".md", ""),
        "title": title or filename.replace(".md", ""),
        "priority": priority,
        "size": size,
        "model": model,
        "project": project,
        "status": status,
        "server": server_id,
        "filename": filename,
        "started_at": started_at,
        "completed_at": None,  # done 폴더 파일에서 나중에 채울 수 있음
    }


# ─── 로컬 (서버 68) 스캔 ─────────────────────────────────────────────────────

async def _scan_local_server(statuses: List[str]) -> Dict[str, Any]:
    """서버 68 로컬 directives 폴더 스캔."""
    base = SERVER_REGISTRY["68"]["directive_base"]
    result: Dict[str, Any] = {"server": "68", "reachable": True, "method": "local", "directives": []}
    counts: Dict[str, int] = {}

    for status in statuses:
        folder = os.path.join(base, status)
        if not os.path.isdir(folder):
            counts[status] = 0
            continue
        try:
            files = [f for f in os.listdir(folder) if f.endswith(".md")]
            counts[status] = len(files)
            for fname in sorted(files)[:100]:  # 최대 100건
                fpath = os.path.join(folder, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(2000)
                    parsed = _parse_directive_content(content, fname, status, "68")
                    # 완료 시각: 파일 mtime 활용
                    if status == "done":
                        try:
                            mtime = os.path.getmtime(fpath)
                            parsed["completed_at"] = datetime.fromtimestamp(mtime, tz=KST).isoformat()
                        except Exception:
                            pass
                    result["directives"].append(parsed)
                except Exception as e:
                    result["directives"].append({
                        "task_id": fname.replace(".md", ""),
                        "filename": fname,
                        "status": status,
                        "server": "68",
                        "error": str(e),
                    })
        except Exception as e:
            counts[status] = 0
            logger.warning("local_scan_error", status=status, error=str(e))

    result["counts"] = counts
    result["total"] = sum(counts.values())
    return result


# ─── SSH (서버 211/114) 스캔 ─────────────────────────────────────────────────

async def _scan_remote_server(server_id: str, statuses: List[str]) -> Dict[str, Any]:
    """SSH로 원격 서버 directives 폴더 스캔."""
    cfg = SERVER_REGISTRY[server_id]
    host = cfg["host"]
    base = cfg["directive_base"]
    result: Dict[str, Any] = {"server": server_id, "method": "ssh", "directives": []}
    counts: Dict[str, int] = {}

    # 한 번의 SSH 호출로 모든 상태 폴더를 스캔
    # 각 폴더의 파일 목록 + 각 파일의 첫 20라인 읽기
    status_dirs = " ".join([f"{base}/{s}" for s in statuses])
    scan_cmd = (
        "for STATUS_DIR in " + " ".join([f"{base}/{s}" for s in statuses]) + "; do "
        "STATUS=$(basename $STATUS_DIR); "
        "[ -d $STATUS_DIR ] || continue; "
        "for F in $(ls $STATUS_DIR/*.md 2>/dev/null | head -100); do "
        "echo \"===FILE:$STATUS:$(basename $F)===\"; "
        "head -25 $F 2>/dev/null; "
        "echo \"===ENDFILE===\"; "
        "done; done"
    )

    output = await _run_ssh_cmd(host, scan_cmd, timeout=10)

    if not output or output.startswith("(error"):
        # SSH 실패 → HTTP fallback
        result["reachable"] = False
        result["method"] = "ssh_failed"
        result["error"] = output[:300] if output else "timeout"
        # counts는 모두 0으로
        for s in statuses:
            counts[s] = 0
        result["counts"] = counts
        result["total"] = 0
        return result

    result["reachable"] = True

    # 파싱: ===FILE:{status}:{filename}=== ... ===ENDFILE===
    blocks = re.split(r"===FILE:([^:]+):([^=]+)===", output)
    # blocks: ['', status1, fname1, content1+ENDFILE, status2, ...]
    for i in range(1, len(blocks) - 2, 3):
        status = blocks[i].strip()
        fname = blocks[i + 1].strip()
        raw = blocks[i + 2]
        content = raw.split("===ENDFILE===")[0].strip()
        if status not in statuses:
            continue
        parsed = _parse_directive_content(content, fname, status, server_id)
        if status == "done":
            # SSH mtime 가져오기는 비용이 크므로 파일명에서 추출
            ts_m = re.search(r"(\d{8}_\d{6})", fname)
            if ts_m:
                try:
                    dt = datetime.strptime(ts_m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=KST)
                    parsed["completed_at"] = dt.isoformat()
                except Exception:
                    pass
        result["directives"].append(parsed)

    # 상태별 카운트
    for s in statuses:
        counts[s] = sum(1 for d in result["directives"] if d.get("status") == s)
    result["counts"] = counts
    result["total"] = sum(counts.values())
    return result


# ─── 통합 스캔 ──────────────────────────────────────────────────────────────

async def scan_all_servers(
    statuses: Optional[List[str]] = None,
    project_filter: Optional[str] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    3대 서버에서 병렬로 directives를 스캔하여 통합 반환.

    캐시: 30초 TTL. force_refresh=True 시 캐시 무시.

    Returns:
        {
            "total_count": int,
            "by_server": {"68": [...], "211": [...], "114": [...]},
            "directives": [...],  # 통합 목록
            "counts": {"pending": N, "running": N, "done": N, "archived": N},
            "cached": bool,
            "scanned_at": str,
        }
    """
    if not force_refresh and _cache_valid() and not project_filter:
        cached = dict(_cache)
        cached["cached"] = True
        # project_filter가 없을 때만 캐시 반환
        return cached

    if statuses is None:
        statuses = ALL_STATUSES

    # 3서버 병렬 스캔
    local_task = _scan_local_server(statuses)
    remote_211_task = _scan_remote_server("211", statuses)
    remote_114_task = _scan_remote_server("114", statuses)

    results_list = await asyncio.gather(
        local_task, remote_211_task, remote_114_task,
        return_exceptions=True,
    )

    by_server: Dict[str, Any] = {}
    all_directives: List[Dict[str, Any]] = []
    total_counts: Dict[str, int] = {s: 0 for s in statuses}

    server_ids = ["68", "211", "114"]
    for sid, res in zip(server_ids, results_list):
        if isinstance(res, Exception):
            by_server[sid] = {"server": sid, "error": str(res), "reachable": False, "directives": [], "counts": {}, "total": 0}
        else:
            by_server[sid] = res
            all_directives.extend(res.get("directives", []))
            for s, cnt in res.get("counts", {}).items():
                total_counts[s] = total_counts.get(s, 0) + cnt

    # project_filter 적용
    if project_filter and project_filter.upper() != "ALL":
        pf = project_filter.upper()
        all_directives = [d for d in all_directives if d.get("project", "").upper() == pf]

    scanned_at = datetime.now(tz=KST).isoformat()

    data = {
        "total_count": len(all_directives),
        "by_server": by_server,
        "directives": all_directives,
        "counts": total_counts,
        "cached": False,
        "scanned_at": scanned_at,
    }

    # 캐시 갱신 (project_filter 없을 때만)
    if not project_filter:
        _set_cache(data)

    return data


async def get_server_summary() -> Dict[str, Any]:
    """
    GET /api/v1/ops/server-summary 응답용 3서버 요약.
    각 서버: pending/running/done 건수 + 프로세스 상태 + 디스크/메모리.
    """
    # 디렉티브 카운트 (캐시 사용)
    scan = await scan_all_servers(statuses=["pending", "running", "done"])

    servers_summary: Dict[str, Any] = {}

    for sid in ["68", "211", "114"]:
        cfg = SERVER_REGISTRY[sid]
        srv_data = scan["by_server"].get(sid, {})
        counts = srv_data.get("counts", {})
        reachable = srv_data.get("reachable", False)

        srv_info: Dict[str, Any] = {
            "server_id": sid,
            "display_name": cfg["display_name"],
            "projects": cfg["projects"],
            "reachable": reachable,
            "method": srv_data.get("method", "unknown"),
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "done": counts.get("done", 0),
        }

        if sid == "68":
            # 로컬 프로세스 확인
            from app.services.health_checker import _run_local_cmd
            bridge_out = await _run_local_cmd("pgrep -c -f 'claude_exec' || echo 0")
            try:
                srv_info["active_claude_sessions"] = int(bridge_out.strip())
            except ValueError:
                srv_info["active_claude_sessions"] = 0
        else:
            # 원격 SSH 프로세스 확인
            host = cfg["host"]
            proc_out = await _run_ssh_cmd(
                host,
                "pgrep -c -f 'claude_exec' 2>/dev/null || echo 0",
                timeout=5,
            )
            try:
                srv_info["active_claude_sessions"] = int(proc_out.strip())
            except ValueError:
                srv_info["active_claude_sessions"] = 0 if reachable else None

        servers_summary[sid] = srv_info

    return {
        "servers": servers_summary,
        "total_pending": sum(s.get("pending", 0) for s in servers_summary.values()),
        "total_running": sum(s.get("running", 0) for s in servers_summary.values()),
        "total_done": sum(s.get("done", 0) for s in servers_summary.values()),
        "scanned_at": scan.get("scanned_at", ""),
        "cached": scan.get("cached", False),
    }
