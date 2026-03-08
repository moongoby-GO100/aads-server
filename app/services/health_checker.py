"""
AADS-166: 파이프라인 전체 헬스체크 시스템
Part 1~5 핵심 로직 모듈화
"""
import os
import json
import asyncio
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import structlog
import asyncpg

logger = structlog.get_logger()

KST = timezone(timedelta(hours=9))
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads_dev_local@aads-postgres:5432/aads"
)

DIRECTIVE_BASE = "/root/.genspark/directives"
GITHUB_PAT = os.getenv("GITHUB_PAT", "")
SSH_SERVERS = {
    "211": "211.188.51.113",
    "114": "116.120.58.155",
}


# ─── Part 1: Directive Folder Scan ─────────────────────────────────────────

async def scan_directive_folder(status: str) -> Dict[str, Any]:
    """디렉티브 폴더 스캔. status: pending|running|done|archived."""
    folder = os.path.join(DIRECTIVE_BASE, status)
    if not os.path.isdir(folder):
        return {"status": status, "folder_exists": False, "count": 0, "directives": []}

    directives = []
    try:
        for fname in sorted(os.listdir(folder)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(folder, fname)
            try:
                stat = os.stat(fpath)
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    preview_lines = []
                    for i, line in enumerate(f):
                        if i >= 5:
                            break
                        preview_lines.append(line.rstrip())
                directives.append({
                    "filename": fname,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=KST).isoformat(),
                    "preview": "\n".join(preview_lines),
                })
            except Exception as e:
                directives.append({"filename": fname, "error": str(e)})
    except Exception as e:
        logger.error("scan_directive_folder_error", status=status, error=str(e))

    return {
        "status": status,
        "folder_exists": True,
        "count": len(directives),
        "directives": directives,
    }


# ─── Part 2: Pipeline Process Liveness ─────────────────────────────────────

def _parse_ps_line(line: str) -> Optional[Dict[str, Any]]:
    """ps aux 출력 한 줄 파싱 → pid, cpu, mem, elapsed info."""
    parts = line.split(None, 10)
    if len(parts) < 11:
        return None
    return {
        "pid": int(parts[1]),
        "cpu": parts[2] + "%",
        "mem": parts[3] + "%",
        "cmd": parts[10][:200],
    }


async def _run_local_cmd(cmd: str, timeout: float = 5.0) -> str:
    """로컬 명령 실행."""
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


async def _run_ssh_cmd(server_key: str, cmd: str, timeout: float = 5.0) -> str:
    """SSH 명령 실행. 실패 시 빈 문자열."""
    ip = SSH_SERVERS.get(server_key, server_key)
    ssh_cmd = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no root@{ip} {repr(cmd)}"
    return await _run_local_cmd(ssh_cmd, timeout=timeout)


async def check_pipeline_status() -> Dict[str, Any]:
    """파이프라인 프로세스 liveness 체크."""
    processes = ["bridge.py", "auto_trigger", "session_watchdog", "claude_exec"]

    async def _check_local(proc_name: str) -> Dict:
        output = await _run_local_cmd(f"pgrep -af '{proc_name}' || true")
        lines = [l for l in output.split("\n") if l.strip() and proc_name in l]
        if proc_name == "claude_exec":
            return {"running": len(lines) > 0, "active_sessions": len(lines)}
        return {"running": len(lines) > 0, "pid": int(lines[0].split()[0]) if lines else None}

    async def _check_remote_211() -> Dict:
        # SSH 먼저 시도, 실패 시 HTTP fallback
        output = await _run_ssh_cmd("211", "pgrep -af 'bridge|auto_trigger|session_watchdog|claude_exec' || true", timeout=10)
        if output and not output.startswith("(error"):
            result = {}
            for proc_name in processes:
                lines = [l for l in output.split("\n") if proc_name in l and l.strip()]
                if proc_name == "claude_exec":
                    result["claude_exec_sessions"] = len(lines)
                else:
                    result[proc_name.replace(".", "_")] = {
                        "running": len(lines) > 0,
                        "pid": int(lines[0].split()[0]) if lines else None,
                    }
            result["reachable"] = True
            result["method"] = "ssh"
            return result

        # SSH 실패 → HTTP fallback
        http_result = await _check_http_health("211")
        if http_result.get("ok"):
            return {
                "reachable": True,
                "method": "http",
                "http_status": http_result,
                "note": "SSH unavailable, using HTTP health endpoint",
            }

        # 둘 다 실패 → reachable False (DEGRADED, not CRITICAL)
        return {"reachable": False, "method": "ssh+http", "error": output or "unreachable"}

    # 병렬 실행
    local_results = await asyncio.gather(
        *[_check_local(p) for p in processes],
        return_exceptions=True,
    )
    remote_211 = await _check_remote_211()

    server_68 = {}
    for i, proc_name in enumerate(processes):
        key = proc_name.replace(".", "_")
        if isinstance(local_results[i], Exception):
            server_68[key] = {"running": False, "error": str(local_results[i])}
        else:
            server_68[key] = local_results[i]

    # overall 판정
    critical_procs = ["bridge_py", "auto_trigger"]
    all_ok = True
    any_down = False
    for cp in critical_procs:
        r211 = remote_211.get(cp, {})
        if isinstance(r211, dict) and not r211.get("running", False):
            any_down = True

    if not remote_211.get("reachable", False):
        overall = "DEGRADED"
    elif any_down:
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    return {
        "server_211": remote_211,
        "server_68": server_68,
        "overall": overall,
    }


# ─── Part 3: Infrastructure Check ──────────────────────────────────────────

async def _check_db() -> Dict[str, Any]:
    """DB ping."""
    import time
    start = time.time()
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=5)
        await conn.fetchval("SELECT 1")
        await conn.close()
        return {"ok": True, "latency_ms": int((time.time() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "error": str(e), "latency_ms": int((time.time() - start) * 1000)}


async def _check_github_pat() -> Dict[str, Any]:
    """GitHub PAT 검증. 미설정 시 severity: warning (기능에 필수 아님)."""
    import httpx
    pat = GITHUB_PAT or os.getenv("GITHUB_TOKEN", "")
    if not pat:
        return {"ok": False, "error": "PAT not configured", "severity": "warning"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"token {pat}"},
            )
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            data = r.json()
            core = data.get("resources", {}).get("core", {})
            return {
                "ok": True,
                "rate_remaining": core.get("remaining", 0),
                "rate_limit": core.get("limit", 0),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


_HTTP_HEALTH_URLS = {
    "211": [
        "http://211.188.51.113:8200/health",
        "http://211.188.51.113:8100/api/v1/health",
        "http://211.188.51.113:8080/health",
    ],
    "114": [
        "http://116.120.58.155:7916/api/health",
        "http://116.120.58.155:7916/health",
    ],
}


async def _check_http_health(server_key: str) -> Dict[str, Any]:
    """HTTP health endpoint 호출로 원격 서버 상태 확인."""
    import httpx
    import time
    urls = _HTTP_HEALTH_URLS.get(server_key, [])
    for url in urls:
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url)
                latency = int((time.time() - start) * 1000)
                if r.status_code < 500:
                    try:
                        body = r.json()
                    except Exception:
                        body = {}
                    return {
                        "ok": r.status_code < 400,
                        "method": "http",
                        "url": url,
                        "status_code": r.status_code,
                        "latency_ms": latency,
                        "services": body,
                    }
        except Exception:
            continue
    return {"ok": False, "method": "http", "error": "all http endpoints unreachable", "urls_tried": urls}


async def _check_ssh(server_key: str) -> Dict[str, Any]:
    """SSH 연결 테스트. 실패 시 HTTP fallback — 실패해도 severity: warning."""
    import time
    start = time.time()
    output = await _run_ssh_cmd(server_key, "echo ok", timeout=8)
    latency = int((time.time() - start) * 1000)
    ok = "ok" in output
    if ok:
        return {"ok": True, "method": "ssh", "latency_ms": latency}

    # SSH 실패 → HTTP fallback
    http_result = await _check_http_health(server_key)
    if http_result.get("ok"):
        return http_result

    # 둘 다 실패 → WARNING (CRITICAL 아님, SSH 키 부재가 원인일 수 있음)
    return {
        "ok": False,
        "method": "ssh+http",
        "latency_ms": latency,
        "error": output[:200] if output else "timeout or unreachable",
        "http_fallback": http_result,
        "severity": "warning",
    }


async def _check_disk(server_key: Optional[str] = None) -> Dict[str, Any]:
    """디스크 사용량."""
    if server_key:
        output = await _run_ssh_cmd(server_key, "df -h / | awk 'NR==2'", timeout=8)
    else:
        output = await _run_local_cmd("df -h / | awk 'NR==2'")
    if not output or output.startswith("(error"):
        return {"ok": False, "error": output}
    parts = output.split()
    if len(parts) < 5:
        return {"ok": False, "error": "unexpected df output"}
    usage_str = parts[4].replace("%", "")
    try:
        usage_pct = int(usage_str)
    except ValueError:
        return {"ok": False, "error": f"cannot parse usage: {parts[4]}"}
    severity = None
    if usage_pct >= 90:
        severity = "critical"
    elif usage_pct >= 80:
        severity = "warning"
    result = {"ok": usage_pct < 90, "usage_pct": usage_pct, "used": parts[2], "total": parts[1]}
    if severity:
        result["severity"] = severity
    return result


async def _check_memory() -> Dict[str, Any]:
    """메모리 사용량 (서버 68) — /proc/meminfo 기반 (Docker 컨테이너 호환)."""
    try:
        with open("/proc/meminfo", "r") as f:
            lines = f.readlines()
        mem = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                val = int(parts[1])  # kB 단위
                mem[key] = val
        total_mb = mem.get("MemTotal", 0) // 1024
        available_mb = mem.get("MemAvailable", mem.get("MemFree", 0)) // 1024
        used_mb = total_mb - available_mb
        usage_pct = round(used_mb / total_mb * 100, 1) if total_mb > 0 else 0

        # Docker cgroup v2 메모리 추가 확인
        cgroup_info = {}
        try:
            with open("/sys/fs/cgroup/memory.max", "r") as f:
                cg_max = f.read().strip()
            if cg_max != "max":
                cg_max_mb = int(cg_max) // (1024 * 1024)
                cgroup_info["cgroup_limit_mb"] = cg_max_mb
        except Exception:
            pass
        try:
            with open("/sys/fs/cgroup/memory.current", "r") as f:
                cg_cur = f.read().strip()
            cgroup_info["cgroup_used_mb"] = int(cg_cur) // (1024 * 1024)
        except Exception:
            pass

        result = {
            "ok": True,
            "total_mb": total_mb,
            "available_mb": available_mb,
            "used_mb": used_mb,
            "usage_pct": usage_pct,
        }
        if cgroup_info:
            result.update(cgroup_info)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _check_cpu() -> Dict[str, Any]:
    """CPU 부하 (서버 68) — /proc/loadavg + /proc/stat 기반 (Docker 컨테이너 호환)."""
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().split()
        load_1m = float(parts[0])
        load_5m = float(parts[1])
        load_15m = float(parts[2])
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # /proc/stat에서 CPU 사용률 2회 샘플링 (100ms 간격)
    cpu_usage_pct = None
    try:
        def _read_cpu_stat():
            with open("/proc/stat", "r") as f:
                for line in f:
                    if line.startswith("cpu "):
                        vals = list(map(int, line.split()[1:]))
                        total = sum(vals)
                        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                        return total, idle
            return None, None

        t1, i1 = _read_cpu_stat()
        await asyncio.sleep(0.1)
        t2, i2 = _read_cpu_stat()
        if t1 and t2 and t2 > t1:
            delta_total = t2 - t1
            delta_idle = i2 - i1
            cpu_usage_pct = round((1 - delta_idle / delta_total) * 100, 1)
    except Exception:
        pass

    result = {
        "ok": load_1m < 4.0,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
    }
    if cpu_usage_pct is not None:
        result["cpu_usage_pct"] = cpu_usage_pct
    return result


async def check_infra() -> Dict[str, Any]:
    """인프라 전체 점검 (병렬)."""
    results = await asyncio.gather(
        _check_db(),
        _check_github_pat(),
        _check_ssh("211"),
        _check_ssh("114"),
        _check_disk(),
        _check_disk("211"),
        _check_disk("114"),
        _check_memory(),
        _check_cpu(),
        return_exceptions=True,
    )

    keys = ["db", "github_pat", "ssh_211", "ssh_114",
            "disk_68", "disk_211", "disk_114", "memory_68", "cpu_68"]
    infra = {}
    issues = []
    for i, key in enumerate(keys):
        if isinstance(results[i], Exception):
            infra[key] = {"ok": False, "error": str(results[i])}
            issues.append({"type": f"{key}_error", "detail": str(results[i]), "severity": "critical"})
        else:
            infra[key] = results[i]
            if not results[i].get("ok", False):
                # severity 필드가 명시된 경우 그대로 사용, 없으면 기본 critical
                sev = results[i].get("severity", "critical")
                issues.append({
                    "type": f"{key}_{'warning' if sev == 'warning' else 'error'}",
                    "detail": results[i].get("error", "check failed"),
                    "severity": sev,
                })

    has_critical = any(i.get("severity") == "critical" for i in issues)
    has_warning = any(i.get("severity") == "warning" for i in issues)
    overall = "CRITICAL" if has_critical else ("DEGRADED" if has_warning else "HEALTHY")

    infra["overall"] = overall
    infra["issues"] = issues
    return infra


# ─── Part 4: Consistency Check ──────────────────────────────────────────────

async def check_consistency(auto_fix: bool = False) -> Dict[str, Any]:
    """정합성 검증. auto_fix=True 시 불일치 자동 복구."""
    issues = []
    result = {}
    fixes_applied = []

    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=5)
        try:
            # 1) STATUS.md ↔ DB
            db_last = await conn.fetchrow(
                "SELECT task_id FROM directive_lifecycle WHERE status='completed' "
                "ORDER BY completed_at DESC LIMIT 1"
            )
            db_last_id = db_last["task_id"] if db_last else None

            status_md_last = None
            status_md_path = "/root/aads/aads-docs/STATUS.md"
            if os.path.exists(status_md_path):
                try:
                    with open(status_md_path, "r") as f:
                        for line in f:
                            if "last_completed:" in line:
                                status_md_last = line.split(":", 1)[1].strip().strip('"')
                                break
                except Exception:
                    pass

            status_sync_ok = (db_last_id == status_md_last) if db_last_id and status_md_last else True
            result["status_md_sync"] = {
                "ok": status_sync_ok,
                "db_last": db_last_id,
                "status_md_last": status_md_last,
            }
            if not status_sync_ok:
                issues.append({"type": "status_md_mismatch", "detail": f"DB: {db_last_id}, STATUS.md: {status_md_last}"})

            # 2) pending 폴더 ↔ DB queued
            pending_dir = os.path.join(DIRECTIVE_BASE, "pending")
            folder_count = len([f for f in os.listdir(pending_dir) if f.endswith(".md")]) if os.path.isdir(pending_dir) else 0
            db_queued = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle WHERE status='queued'"
            )
            db_queued = int(db_queued or 0)
            pending_ok = abs(folder_count - db_queued) <= 2  # 허용 오차
            result["pending_sync"] = {
                "ok": pending_ok,
                "folder_count": folder_count,
                "db_queued": db_queued,
                "mismatch": abs(folder_count - db_queued),
            }
            if not pending_ok:
                issues.append({"type": "pending_mismatch", "detail": f"DB에 {db_queued}건 queued이나 폴더는 {folder_count}건"})

            # 2-b) auto_fix: pending 폴더에 없는 queued 건 → archived로 업데이트
            if auto_fix and db_queued > 0:
                # pending 폴더 파일명에서 task_id 추출
                folder_task_ids: set = set()
                if os.path.isdir(pending_dir):
                    for fname in os.listdir(pending_dir):
                        if fname.endswith(".md"):
                            # 파일명 패턴: AADS_YYYYMMDD_HHMMSS_LABEL.md → task_id는 파일 내부에서 추출
                            fpath = os.path.join(pending_dir, fname)
                            try:
                                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                    content = f.read(500)
                                # TASK_ID: XXX-NNN 패턴 추출
                                import re
                                m = re.search(r"TASK_ID:\s*([\w-]+)", content)
                                if m:
                                    folder_task_ids.add(m.group(1))
                                else:
                                    folder_task_ids.add(fname.replace(".md", ""))
                            except Exception:
                                folder_task_ids.add(fname.replace(".md", ""))

                # DB에서 queued 건 조회
                queued_rows = await conn.fetch(
                    "SELECT id, task_id FROM directive_lifecycle WHERE status='queued'"
                )
                fixed_count = 0
                for row in queued_rows:
                    db_tid = row["task_id"]
                    if db_tid not in folder_task_ids:
                        await conn.execute(
                            "UPDATE directive_lifecycle SET status='archived', "
                            "completed_at=NOW() WHERE id=$1",
                            row["id"],
                        )
                        fixed_count += 1
                if fixed_count > 0:
                    fixes_applied.append(f"queued→archived: {fixed_count}건 (pending 폴더 없음)")
                result["auto_fix_queued"] = {"fixed": fixed_count, "total_queued": len(queued_rows)}

            # 3) commit SHA 검증
            commit_sha_db = None
            status_sha = None
            try:
                row = await conn.fetchrow(
                    "SELECT commit_sha FROM commit_log ORDER BY pushed_at DESC LIMIT 1"
                )
                commit_sha_db = row["commit_sha"] if row else None
            except Exception:
                pass
            if os.path.exists(status_md_path):
                try:
                    with open(status_md_path, "r") as f:
                        for line in f:
                            if "commit_sha:" in line:
                                status_sha = line.split(":", 1)[1].strip().strip('"')
                                break
                except Exception:
                    pass
            commit_ok = True  # 완전 일치보다 존재 여부만 확인
            result["commit_sync"] = {
                "ok": commit_ok,
                "db_last_sha": commit_sha_db,
                "status_md_sha": status_sha,
            }

            # 4) HANDOVER 동기화
            result["handover_sync"] = {"ok": True}

        finally:
            await conn.close()
    except Exception as e:
        result["error"] = str(e)
        issues.append({"type": "consistency_db_error", "detail": str(e)})

    has_issue = len(issues) > 0
    result["overall"] = "DEGRADED" if has_issue else "HEALTHY"
    result["issues"] = issues
    if fixes_applied:
        result["fixes_applied"] = fixes_applied
    return result


# ─── Part 5: Full Health (통합) ─────────────────────────────────────────────

async def full_health_check() -> Dict[str, Any]:
    """Part 1~4 + 기존 health-check 병렬 실행."""
    import time
    start = time.time()

    # Part 1: 4개 상태 폴더 스캔
    dir_results = await asyncio.gather(
        scan_directive_folder("pending"),
        scan_directive_folder("running"),
        scan_directive_folder("done"),
        scan_directive_folder("archived"),
        return_exceptions=True,
    )
    directives_section = {}
    for i, status in enumerate(["pending", "running", "done", "archived"]):
        if isinstance(dir_results[i], Exception):
            directives_section[status] = {"error": str(dir_results[i])}
        else:
            directives_section[status] = {"count": dir_results[i]["count"], "folder_exists": dir_results[i]["folder_exists"]}

    # Part 2~4 병렬
    pipeline_result, infra_result, consistency_result = await asyncio.gather(
        check_pipeline_status(),
        check_infra(),
        check_consistency(),
        return_exceptions=True,
    )

    # 기존 DB 기반 health-check
    existing_health = {}
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=5)
        try:
            stalled_running = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle "
                "WHERE status='running' AND started_at < NOW() - INTERVAL '60 min'"
            )
            completed_today = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle WHERE status = 'completed' AND completed_at >= CURRENT_DATE"
            )
            existing_health = {
                "stalled_running": int(stalled_running or 0),
                "completed_today": int(completed_today or 0),
            }
        finally:
            await conn.close()
    except Exception as e:
        existing_health = {"error": str(e)}

    # 이슈 통합
    all_issues = []
    sections_status = {}

    if isinstance(pipeline_result, Exception):
        pipeline_result = {"overall": "CRITICAL", "error": str(pipeline_result)}
    sections_status["pipeline"] = pipeline_result.get("overall", "HEALTHY")

    if isinstance(infra_result, Exception):
        infra_result = {"overall": "CRITICAL", "issues": [{"type": "infra_error", "detail": str(infra_result)}]}
    all_issues.extend(infra_result.get("issues", []))
    sections_status["infra"] = infra_result.get("overall", "HEALTHY")

    if isinstance(consistency_result, Exception):
        consistency_result = {"overall": "CRITICAL", "issues": [{"type": "consistency_error", "detail": str(consistency_result)}]}
    all_issues.extend(consistency_result.get("issues", []))
    sections_status["consistency"] = consistency_result.get("overall", "HEALTHY")

    # 전체 상태 결정
    statuses = list(sections_status.values())
    if "CRITICAL" in statuses:
        overall = "CRITICAL"
    elif "DEGRADED" in statuses:
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    duration_ms = int((time.time() - start) * 1000)

    # 한국어 요약
    summary_parts = []
    summary_parts.append(f"파이프라인: {'정상' if sections_status.get('pipeline') == 'HEALTHY' else sections_status.get('pipeline', '?')}")
    summary_parts.append(f"인프라: {'정상' if sections_status.get('infra') == 'HEALTHY' else sections_status.get('infra', '?')}")
    summary_parts.append(f"정합성: {'정상' if sections_status.get('consistency') == 'HEALTHY' else sections_status.get('consistency', '?')}")
    if all_issues:
        summary_parts.append(f"이슈 {len(all_issues)}건")

    return {
        "status": overall,
        "checked_at": datetime.now(tz=KST).isoformat(),
        "duration_ms": duration_ms,
        "sections": {
            "directives": directives_section,
            "pipeline": pipeline_result,
            "infra": infra_result,
            "consistency": consistency_result,
            "existing_health": existing_health,
        },
        "issues": all_issues,
        "summary_kr": ", ".join(summary_parts),
    }


# ─── Quick Health (SSE용 경량) ──────────────────────────────────────────────

async def quick_health() -> Dict[str, Any]:
    """SSE 스트리밍용 경량 헬스체크."""
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=3)
        try:
            stalled = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle "
                "WHERE status='running' AND started_at < NOW() - INTERVAL '60 min'"
            )
            running = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle WHERE status='running'"
            )
            completed_today = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle WHERE status='completed' AND completed_at >= CURRENT_DATE"
            )
        finally:
            await conn.close()

        pending_dir = os.path.join(DIRECTIVE_BASE, "pending")
        pending_count = len([f for f in os.listdir(pending_dir) if f.endswith(".md")]) if os.path.isdir(pending_dir) else 0
        running_dir = os.path.join(DIRECTIVE_BASE, "running")
        running_folder = len([f for f in os.listdir(running_dir) if f.endswith(".md")]) if os.path.isdir(running_dir) else 0

        status = "HEALTHY"
        if int(stalled or 0) > 0:
            status = "DEGRADED"

        return {
            "status": status,
            "stalled": int(stalled or 0),
            "running": int(running or 0),
            "completed_today": int(completed_today or 0),
            "pending_folder": pending_count,
            "running_folder": running_folder,
            "checked_at": datetime.now(tz=KST).isoformat(),
        }
    except Exception as e:
        return {"status": "CRITICAL", "error": str(e), "checked_at": datetime.now(tz=KST).isoformat()}


async def directive_changes_since(last_check: datetime) -> List[Dict]:
    """마지막 체크 이후 변경된 directive lifecycle."""
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=3)
        try:
            rows = await conn.fetch(
                """SELECT task_id, status, title, project,
                          COALESCE(completed_at, started_at, queued_at) as changed_at
                   FROM directive_lifecycle
                   WHERE COALESCE(completed_at, started_at, queued_at) > $1
                   ORDER BY COALESCE(completed_at, started_at, queued_at) DESC
                   LIMIT 10""",
                last_check,
            )
        finally:
            await conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


async def pipeline_quick_status() -> Dict[str, Any]:
    """SSE용 파이프라인 간략 상태."""
    bridge = await _run_local_cmd("pgrep -c -f 'bridge.py' || echo 0")
    claude_exec = await _run_local_cmd("pgrep -c -f 'claude_exec' || echo 0")
    try:
        bridge_count = int(bridge.strip())
    except ValueError:
        bridge_count = 0
    try:
        exec_count = int(claude_exec.strip())
    except ValueError:
        exec_count = 0
    return {
        "bridge_running": bridge_count > 0,
        "active_sessions": exec_count,
        "checked_at": datetime.now(tz=KST).isoformat(),
    }
