#!/usr/bin/env python3
"""
AADS Remote Agent Daemon — 원격 서버 매니저 대화 수집 + 68서버 메모리 연동
생성: 2026-03-05 T-061 (aads_remote_agent.py 기반)
T-062 적용: 116서버 newtalk_v2 대화수집 + Claude Code 연동

동작:
  - HTTP 데몬 실행 (기본 포트: 9900)
  - /health, /status 엔드포인트 제공
  - PROJECTS 설정에 따라 매니저 대화 로그 수집
  - 수집 데이터를 68서버 AADS Context API + Memory API에 전송
  - systemd 데몬으로 상시 실행

사용:
  python3 aads_remote_agent.py                # 기본 실행 (포트 9900)
  python3 aads_remote_agent.py --port 9900    # 포트 지정
  python3 aads_remote_agent.py --once         # 1회 수집 후 종료
  python3 aads_remote_agent.py --health       # 헬스체크만 수행

환경변수:
  AADS_REMOTE_PORT          HTTP 데몬 포트 (기본: 9900)
  AADS_API_URL              68서버 Context API URL
  AADS_MEMORY_URL           68서버 Memory API URL
  AADS_MONITOR_KEY          AADS 모니터 인증 키
  AADS_REMOTE_SERVER_ID     이 서버 식별자 (기본: REMOTE_116)
  COLLECT_INTERVAL          수집 주기(초) (기본: 300 = 5분)
  PROJECTS_CONFIG           JSON 형태 프로젝트 설정
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ─── 설정 ─────────────────────────────────────────────────────────────────────
PORT = int(os.getenv("AADS_REMOTE_PORT", "9900"))
AADS_API_URL = os.getenv("AADS_API_URL", "https://aads.newtalk.kr/api/v1/context/system")
AADS_MEMORY_URL = os.getenv("AADS_MEMORY_URL", "https://aads.newtalk.kr/api/v1/memory/log")
AADS_HEALTH_URL = os.getenv("AADS_HEALTH_URL", "https://aads.newtalk.kr/api/v1/health")
MONITOR_KEY = os.getenv("AADS_MONITOR_KEY", "")
SERVER_ID = os.getenv("AADS_REMOTE_SERVER_ID", "REMOTE_116")
COLLECT_INTERVAL = int(os.getenv("COLLECT_INTERVAL", "300"))

# 프로젝트 설정 — 환경변수 PROJECTS_CONFIG 또는 기본값
_DEFAULT_PROJECTS = {
    "newtalk_v2": {
        "path": "/root/newtalk-v2",
        "manager": "NT_MGR",
    }
}
try:
    PROJECTS = json.loads(os.getenv("PROJECTS_CONFIG", "{}")) or _DEFAULT_PROJECTS
except Exception:
    PROJECTS = _DEFAULT_PROJECTS

# ─── 로깅 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("aads-remote-agent")

# ─── 전역 상태 ──────────────────────────────────────────────────────────────────
_state = {
    "started_at": None,
    "last_collect": None,
    "collect_count": 0,
    "last_error": None,
    "conversations_total": 0,
    "status": "initializing",
}


# ─── KST 타임스탬프 ────────────────────────────────────────────────────────────
def _now_kst() -> str:
    try:
        kst = timezone(timedelta(hours=9))
        return datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def _now_iso() -> str:
    try:
        kst = timezone(timedelta(hours=9))
        return datetime.now(kst).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── HTTP 요청 헬퍼 ───────────────────────────────────────────────────────────
def _post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Monitor-Key": MONITOR_KEY,
            "User-Agent": "curl/7.64.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        return {"status": "error", "code": e.code, "detail": body_text}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        headers={"X-Monitor-Key": MONITOR_KEY, "User-Agent": "curl/7.64.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── 대화 수집 ────────────────────────────────────────────────────────────────
def _collect_newtalk_v2_conversations(project_name: str, config: dict) -> list:
    """
    newtalk_v2 디렉토리에서 매니저 대화 로그 수집.
    - /root/newtalk-v2/storage/logs/ 에서 최신 로그 파일 읽기
    - Laravel 로그 또는 커스텀 매니저 대화 파일 탐색
    """
    project_path = config.get("path", "/root/newtalk-v2")
    manager = config.get("manager", "NT_MGR")
    conversations = []

    # 탐색 경로 목록
    search_paths = [
        os.path.join(project_path, "storage", "logs"),
        os.path.join(project_path, "logs"),
        os.path.join(project_path, "storage", "app", "conversations"),
        "/var/log/newtalk-v2",
        "/var/log/newtalk_v2",
    ]

    for log_dir in search_paths:
        if not os.path.isdir(log_dir):
            continue

        try:
            files = sorted(
                [
                    f for f in os.listdir(log_dir)
                    if f.endswith(".log") or f.endswith(".json")
                ],
                reverse=True
            )[:5]  # 최신 5개 파일만

            for fname in files:
                fpath = os.path.join(log_dir, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    # 24시간 이내 파일만 처리
                    if time.time() - mtime > 86400:
                        continue

                    with open(fpath, "r", errors="replace") as f:
                        content = f.read(65536)  # 최대 64KB

                    # 대화 패턴 추출 (간단한 룰 기반)
                    conv_count = len(re.findall(
                        r'(?i)(message|chat|conversation|대화|메시지|사용자)',
                        content
                    ))

                    if conv_count > 0:
                        conversations.append({
                            "file": fname,
                            "mtime": _now_iso(),
                            "conv_count_estimate": conv_count,
                            "manager": manager,
                            "project": project_name,
                        })
                except Exception as e:
                    logger.debug("파일 읽기 실패 %s: %s", fpath, e)

        except Exception as e:
            logger.debug("디렉토리 탐색 실패 %s: %s", log_dir, e)

    # 대화 파일이 없으면 프로젝트 존재 여부만 보고
    if not conversations:
        conversations.append({
            "file": "status_check",
            "mtime": _now_iso(),
            "conv_count_estimate": 0,
            "manager": manager,
            "project": project_name,
            "note": "대화 로그 파일 없음 또는 접근 불가",
            "project_exists": os.path.isdir(project_path),
        })

    return conversations


def collect_all_projects() -> dict:
    """모든 PROJECTS 설정에 대해 대화 수집"""
    results = {}
    total_convs = 0

    for project_name, config in PROJECTS.items():
        try:
            convs = _collect_newtalk_v2_conversations(project_name, config)
            results[project_name] = {
                "manager": config.get("manager", "UNKNOWN"),
                "path": config.get("path", ""),
                "conversations": convs,
                "collected_at": _now_iso(),
            }
            total_convs += sum(c.get("conv_count_estimate", 0) for c in convs)
            logger.info("수집 완료: %s → %d 항목", project_name, len(convs))
        except Exception as e:
            logger.error("수집 실패: %s — %s", project_name, e)
            results[project_name] = {
                "manager": config.get("manager", "UNKNOWN"),
                "error": str(e),
                "collected_at": _now_iso(),
            }

    return results, total_convs


# ─── task_result 자동 보고 (T-091) ──────────────────────────────────────────────
# 프로젝트 디렉토리별 최근 작업 결과 파일 탐색 경로
_TASK_RESULT_PATTERNS = [
    "reports/*.md",
    "docs/reports/*.md",
    "aads-docs/reports/*.md",
    ".genspark/directives/done/*.md",
    "handover*.md",
    "HANDOVER*.md",
    "RESULT*.md",
]

_reported_tasks: dict = {}  # task_id → reported_at (중복 방지)

PROJECTS_WITH_NAMES = {
    # 211서버 기본값 (환경변수 PROJECTS_CONFIG로 재정의 가능)
    "KIS":       {"path": "/root/kis",       "project": "KIS"},
    "GO100":     {"path": "/root/go100",     "project": "GO100"},
    "ShortFlow": {"path": "/root/shortflow", "project": "ShortFlow"},
}


def _detect_task_results_from_dir(project: str, base_path: str) -> list:
    """
    프로젝트 디렉토리에서 최근 24시간 내 변경된 보고서/작업결과 파일 감지.
    Returns list of task_result dicts.
    """
    results = []
    cutoff = time.time() - 86400  # 24시간 이내
    search_paths = []

    for pattern in _TASK_RESULT_PATTERNS:
        import glob as _glob
        full_pattern = os.path.join(base_path, pattern)
        try:
            matched = _glob.glob(full_pattern)
            search_paths.extend(matched)
        except Exception:
            pass

    for fpath in search_paths:
        try:
            mtime = os.path.getmtime(fpath)
            if mtime < cutoff:
                continue
            fname = os.path.basename(fpath)
            task_id_candidate = fname.replace(".md", "").replace(".txt", "")

            # 중복 보고 방지
            if task_id_candidate in _reported_tasks:
                continue

            with open(fpath, "r", errors="replace") as f:
                content = f.read(4096)

            # 파일에서 task_id, title, summary 추출 시도
            task_id = task_id_candidate
            title = fname
            summary = content[:200].replace("\n", " ").strip()
            status = "completed"

            # YAML front matter 파싱 시도
            if content.startswith("---"):
                fm_end = content.find("---", 3)
                if fm_end > 0:
                    fm = content[3:fm_end]
                    for line in fm.split("\n"):
                        if ":" in line:
                            k, _, v = line.partition(":")
                            k, v = k.strip(), v.strip()
                            if k == "task_id":
                                task_id = v
                            elif k == "title":
                                title = v[:200]
                            elif k in ("status", "task_status"):
                                status = v

            # H1 제목 추출
            for line in content.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()[:200]
                    break

            results.append({
                "task_id": task_id,
                "project": project,
                "status": status,
                "title": title,
                "summary": summary,
                "completed_at": _now_iso(),
            })
            _reported_tasks[task_id_candidate] = _now_iso()

        except Exception as e:
            logger.debug("파일 처리 실패 %s: %s", fpath, e)

    return results


def auto_report_task_results() -> list:
    """
    모든 프로젝트 디렉토리에서 task_result 감지 → 68서버에 자동 보고.
    Returns list of reported task_result dicts.
    """
    reported = []

    # PROJECTS_CONFIG 우선, 없으면 PROJECTS_WITH_NAMES 사용
    projects_to_scan = {}
    for p_name, p_cfg in PROJECTS.items():
        project_label = p_cfg.get("project", p_name.upper())
        projects_to_scan[project_label] = p_cfg.get("path", f"/root/{p_name.lower()}")

    # PROJECTS_WITH_NAMES 보완
    for label, cfg in PROJECTS_WITH_NAMES.items():
        if label not in projects_to_scan:
            projects_to_scan[label] = cfg["path"]

    for project, base_path in projects_to_scan.items():
        if not os.path.isdir(base_path):
            continue

        task_results = _detect_task_results_from_dir(project, base_path)
        for tr in task_results:
            payload = {
                "category": f"cross_msg_{SERVER_ID}_AADS_MGR",
                "key": f"task_result_{tr['task_id']}_{int(time.time())}",
                "value": {
                    "message_type": "task_result",
                    "project": tr["project"],
                    "task_id": tr["task_id"],
                    "status": tr["status"],
                    "title": tr["title"],
                    "summary": tr["summary"],
                    "completed_at": tr["completed_at"],
                },
            }
            result = _post_json(AADS_API_URL, payload)
            logger.info(
                "task_result 보고 [%s/%s]: %s",
                tr["project"], tr["task_id"], result.get("status", "unknown")
            )
            reported.append({**tr, "report_status": result.get("status", "unknown")})

    return reported


# ─── AADS 보고 ─────────────────────────────────────────────────────────────────
def report_to_aads(collect_result: dict, total_convs: int) -> dict:
    """수집 결과를 68서버 AADS Context API에 보고"""
    ts = _now_iso()

    # 1. Context API에 원격 서버 상태 저장
    ctx_payload = {
        "category": "remote_agents",
        "key": SERVER_ID,
        "value": {
            "server_id": SERVER_ID,
            "status": "active",
            "last_collect": ts,
            "projects": list(collect_result.keys()),
            "total_conversations": total_convs,
            "collect_data": collect_result,
            "updated_at": ts,
        },
    }
    ctx_result = _post_json(AADS_API_URL, ctx_payload)
    logger.info("Context API 저장: %s", ctx_result.get("status", "unknown"))

    # 2. Memory API에 매니저 대화 로그 저장
    for project_name, data in collect_result.items():
        manager = data.get("manager", "UNKNOWN")
        convs = data.get("conversations", [])
        if not convs:
            continue

        mem_payload = {
            "user_id": 2,
            "memory_type": f"manager_conv_{manager.lower()}",
            "content": {
                "agent_id": manager,
                "event_type": "conversation_collect",
                "details": {
                    "server_id": SERVER_ID,
                    "project": project_name,
                    "conversations": convs[:10],  # 최대 10개만
                    "total": total_convs,
                    "source": "aads_remote_agent",
                },
                "logged_at": ts,
            },
            "importance": 6.5,
            "expires_at": None,
        }
        mem_result = _post_json(AADS_MEMORY_URL, mem_payload)
        logger.info("Memory API 저장 (%s): %s", manager, mem_result.get("status", "unknown"))

    # 3. task_result 자동 감지 보고 (T-091)
    task_results = auto_report_task_results()
    if task_results:
        logger.info("task_result 자동 보고 완료: %d건", len(task_results))

    return ctx_result


def run_collect_cycle():
    """1회 수집 → 보고 사이클 실행"""
    logger.info("=== 수집 사이클 시작 (%s) ===", _now_kst())
    _state["status"] = "collecting"

    try:
        collect_result, total_convs = collect_all_projects()
        _state["conversations_total"] += total_convs

        report_result = report_to_aads(collect_result, total_convs)

        _state["last_collect"] = _now_iso()
        _state["collect_count"] += 1
        _state["status"] = "active"
        _state["last_error"] = None
        logger.info("=== 수집 사이클 완료 — 총 대화수: %d ===", total_convs)
        return True

    except Exception as e:
        _state["last_error"] = str(e)
        _state["status"] = "error"
        logger.error("수집 사이클 실패: %s", e)
        return False


def run_daemon():
    """백그라운드 수집 스레드 — COLLECT_INTERVAL 주기로 실행"""
    logger.info("데몬 수집 스레드 시작 (주기: %d초)", COLLECT_INTERVAL)
    while True:
        run_collect_cycle()
        time.sleep(COLLECT_INTERVAL)


# ─── HTTP 핸들러 ──────────────────────────────────────────────────────────────
class AgentHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug("HTTP: " + fmt, *args)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path == "/health":
            uptime_secs = (
                int(time.time() - _state["started_at"])
                if _state["started_at"]
                else 0
            )
            self._send_json({
                "status": "ok",
                "server_id": SERVER_ID,
                "uptime_seconds": uptime_secs,
                "collect_count": _state["collect_count"],
                "last_collect": _state["last_collect"],
                "agent_status": _state["status"],
                "projects": list(PROJECTS.keys()),
                "timestamp": _now_iso(),
            })

        elif path == "/status":
            self._send_json({
                "server_id": SERVER_ID,
                "status": _state["status"],
                "started_at": _state["started_at"],
                "last_collect": _state["last_collect"],
                "collect_count": _state["collect_count"],
                "conversations_total": _state["conversations_total"],
                "last_error": _state["last_error"],
                "projects": PROJECTS,
                "config": {
                    "port": PORT,
                    "collect_interval": COLLECT_INTERVAL,
                    "aads_api": AADS_API_URL,
                },
                "timestamp": _now_iso(),
            })

        elif path == "/collect":
            # 수동 수집 트리거
            success = run_collect_cycle()
            self._send_json({
                "triggered": True,
                "success": success,
                "last_collect": _state["last_collect"],
                "error": _state["last_error"],
            })

        else:
            self._send_json({"error": "Not Found", "path": path}, status=404)


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    global PROJECTS, PORT

    parser = argparse.ArgumentParser(
        description="AADS Remote Agent — 원격 서버 매니저 대화 수집 데몬",
    )
    parser.add_argument("--port", type=int, default=PORT, help="HTTP 포트 (기본: 9900)")
    parser.add_argument("--once", action="store_true", help="1회 수집 후 종료")
    parser.add_argument("--health", action="store_true", help="헬스체크만 수행 (로컬)")
    parser.add_argument("--config", help="프로젝트 설정 JSON 파일 경로")
    args = parser.parse_args()

    PORT = args.port

    if args.config:
        try:
            with open(args.config) as f:
                PROJECTS = json.load(f)
            logger.info("프로젝트 설정 로드: %s", args.config)
        except Exception as e:
            logger.error("설정 파일 로드 실패: %s", e)
            sys.exit(1)

    if args.health:
        # 로컬 헬스체크
        print(json.dumps({
            "status": "ok",
            "server_id": SERVER_ID,
            "projects": list(PROJECTS.keys()),
            "timestamp": _now_iso(),
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    if args.once:
        logger.info("1회 수집 모드")
        success = run_collect_cycle()
        sys.exit(0 if success else 1)

    # 데몬 모드
    _state["started_at"] = time.time()
    _state["status"] = "starting"

    logger.info("AADS Remote Agent 시작")
    logger.info("  Server ID     : %s", SERVER_ID)
    logger.info("  Port          : %d", PORT)
    logger.info("  AADS API      : %s", AADS_API_URL)
    logger.info("  Collect interval: %d초", COLLECT_INTERVAL)
    logger.info("  Projects      : %s", list(PROJECTS.keys()))

    if not MONITOR_KEY:
        logger.warning("AADS_MONITOR_KEY 미설정 — API 인증 실패 가능")

    # 백그라운드 수집 스레드 시작
    t = threading.Thread(target=run_daemon, daemon=True)
    t.start()

    # HTTP 서버 시작
    _state["status"] = "active"
    server = HTTPServer(("0.0.0.0", PORT), AgentHandler)
    logger.info("HTTP 서버 시작: 0.0.0.0:%d", PORT)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("종료 신호 수신")
    finally:
        server.server_close()
        logger.info("AADS Remote Agent 종료")


if __name__ == "__main__":
    main()
