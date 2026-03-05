#!/usr/bin/env python3
"""
aads_remote_agent.py — T-061: 211서버 원격 에이전트 데몬
- HTTP 서버 (포트 9900)
- POST /tasks  : 작업 수신 → Claude Code 실행 → 결과 콜백
- GET  /health : 상태 확인
- GET  /status : go100/shortflow 프로젝트 상태 수집
- 5분 간격 자동 보고: AADS /api/v1/memory/cross-message 전송
- 5분 간격 대화 수집: go100/shortflow 최신 로그 파싱 후 전송
"""

import asyncio
import subprocess
import json
import logging
import os
import glob
import shutil
from datetime import datetime

try:
    from aiohttp import web, ClientSession, ClientTimeout
except ImportError:
    raise SystemExit("aiohttp가 필요합니다: pip install aiohttp")

# ── 설정 ─────────────────────────────────────────────────────────────────────
AADS_SERVER = os.getenv("AADS_SERVER", "https://aads.newtalk.kr/api/v1")
REMOTE_KEY  = os.getenv("AADS_REMOTE_KEY", "changeme")
PORT        = int(os.getenv("AADS_REMOTE_PORT", "9900"))
LOG_FILE    = os.getenv("AADS_LOG_FILE", "/var/log/aads_remote_agent.log")
AGENT_ID    = os.getenv("AADS_AGENT_ID", "REMOTE_211")
REPORT_INTERVAL = int(os.getenv("AADS_REPORT_INTERVAL", "300"))  # 5분

PROJECTS = {
    "go100": {
        "path": "/root/go100",
        "manager": "GO100_MGR",
        "log_dirs": ["/root/go100/logs", "/root/go100/log"],
    },
    "shortflow": {
        "path": "/root/shortflow",
        "manager": "SF_MGR",
        "log_dirs": ["/root/shortflow/logs", "/root/shortflow/log"],
    },
}

# ── 로깅 ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE) if os.access(os.path.dirname(LOG_FILE) or ".", os.W_OK) else logging.NullHandler(),
    ],
)
logger = logging.getLogger("aads_remote_agent")


# ── 인증 헬퍼 ────────────────────────────────────────────────────────────────
def _check_auth(request: web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        return token == REMOTE_KEY
    return False


def _auth_error() -> web.Response:
    return web.json_response({"error": "Unauthorized"}, status=401)


# ── RemoteAgent ───────────────────────────────────────────────────────────────
class RemoteAgent:
    def __init__(self):
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self.results: dict = {}
        self._http_session: ClientSession | None = None

    # ── HTTP 세션 ────────────────────────────────────────────────────────────
    async def _get_session(self) -> ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = ClientSession(
                timeout=ClientTimeout(total=30),
                headers={"Authorization": f"Bearer {REMOTE_KEY}"},
            )
        return self._http_session

    # ── POST /tasks ──────────────────────────────────────────────────────────
    async def handle_task(self, request: web.Request) -> web.Response:
        if not _check_auth(request):
            return _auth_error()

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        task_id = body.get("task_id", f"task_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}")
        prompt  = body.get("prompt", "")
        project = body.get("project", "")
        callback_url = body.get("callback_url", "")

        if not prompt:
            return web.json_response({"error": "prompt required"}, status=400)

        logger.info(f"[TASK] {task_id} — project={project} prompt_len={len(prompt)}")

        # 백그라운드에서 Claude Code 실행 후 콜백
        asyncio.create_task(self._run_claude(task_id, prompt, project, callback_url))

        return web.json_response({
            "task_id": task_id,
            "status": "accepted",
            "message": "Task queued for execution",
        })

    async def _run_claude(self, task_id: str, prompt: str, project: str, callback_url: str):
        """Claude Code subprocess 실행 후 결과를 AADS 또는 callback_url로 전송"""
        started_at = datetime.utcnow().isoformat() + "Z"
        result = {"task_id": task_id, "project": project, "started_at": started_at}

        # 작업 디렉토리 결정
        cwd = PROJECTS.get(project, {}).get("path", "/root")
        if not os.path.isdir(cwd):
            cwd = "/root"

        # Claude Code 실행
        claude_bin = shutil.which("claude") or "claude"
        cmd = [claude_bin, "-p", prompt, "--output-format", "json"]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            raw = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw_output": raw}

            result.update({
                "status": "success" if proc.returncode == 0 else "error",
                "exit_code": proc.returncode,
                "output": parsed,
                "stderr": err[:2000] if err else "",
                "finished_at": datetime.utcnow().isoformat() + "Z",
            })
        except asyncio.TimeoutError:
            result.update({"status": "timeout", "error": "Claude Code timed out after 300s"})
        except FileNotFoundError:
            result.update({"status": "error", "error": "claude binary not found"})
        except Exception as e:
            result.update({"status": "error", "error": str(e)})

        self.results[task_id] = result
        logger.info(f"[TASK_DONE] {task_id} status={result.get('status')}")

        # 콜백 전송
        target = callback_url or f"{AADS_SERVER}/memory/cross-message"
        await self._post_result(task_id, result, target)

    async def _post_result(self, task_id: str, result: dict, url: str):
        payload = {
            "from_agent": AGENT_ID,
            "to_agent": "AADS_MGR",
            "message_type": "task_result",
            "task_id": task_id,
            "content": result,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                logger.info(f"[CALLBACK] {url} → HTTP {resp.status}")
        except Exception as e:
            logger.error(f"[CALLBACK_ERR] {url} — {e}")

    # ── GET /health ──────────────────────────────────────────────────────────
    async def handle_health(self, request: web.Request) -> web.Response:
        # 인증 없이 공개 (모니터링 용)
        claude_version = "unknown"
        claude_found = False
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            claude_version = out.decode().strip()
            claude_found = proc.returncode == 0
        except Exception:
            claude_found = False

        # 디스크/메모리
        disk_info = {}
        mem_info = {}
        try:
            df = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()
            if len(df) > 1:
                parts = df[1].split()
                disk_info = {"total": parts[1], "used": parts[2], "free": parts[3], "pct": parts[4]}
        except Exception:
            pass
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            mem = {l.split(":")[0].strip(): l.split(":")[1].strip() for l in lines if ":" in l}
            mem_info = {
                "total": mem.get("MemTotal", ""),
                "free": mem.get("MemFree", ""),
                "available": mem.get("MemAvailable", ""),
            }
        except Exception:
            pass

        return web.json_response({
            "status": "ok",
            "agent_id": AGENT_ID,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "claude": {"found": claude_found, "version": claude_version},
            "disk": disk_info,
            "memory": mem_info,
            "pending_tasks": self.task_queue.qsize(),
            "cached_results": len(self.results),
        })

    # ── GET /status ──────────────────────────────────────────────────────────
    async def handle_status(self, request: web.Request) -> web.Response:
        if not _check_auth(request):
            return _auth_error()

        status = {}
        for name, cfg in PROJECTS.items():
            proj_path = cfg["path"]
            exists = os.path.isdir(proj_path)

            # 프로세스 확인
            proc_info = []
            try:
                out = subprocess.check_output(
                    ["ps", "aux"], text=True, stderr=subprocess.DEVNULL
                )
                for line in out.splitlines():
                    if name in line.lower() and "ps aux" not in line:
                        proc_info.append(line.strip()[:200])
            except Exception:
                pass

            # 최근 로그
            recent_logs = []
            for log_dir in cfg.get("log_dirs", []):
                if os.path.isdir(log_dir):
                    files = sorted(glob.glob(f"{log_dir}/*"), key=os.path.getmtime, reverse=True)
                    for fpath in files[:3]:
                        try:
                            with open(fpath) as f:
                                lines = f.readlines()
                            recent_logs.append({
                                "file": fpath,
                                "lines": [l.rstrip() for l in lines[-20:]],
                                "mtime": datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat(),
                            })
                        except Exception:
                            pass
                    break

            status[name] = {
                "path": proj_path,
                "exists": exists,
                "manager": cfg["manager"],
                "processes": proc_info[:5],
                "recent_logs": recent_logs,
            }

        return web.json_response({
            "agent_id": AGENT_ID,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "projects": status,
        })

    # ── GET /tasks/{task_id} ─────────────────────────────────────────────────
    async def handle_task_result(self, request: web.Request) -> web.Response:
        if not _check_auth(request):
            return _auth_error()
        task_id = request.match_info["task_id"]
        if task_id not in self.results:
            return web.json_response({"error": "task not found"}, status=404)
        return web.json_response(self.results[task_id])

    # ── 백그라운드: auto_report (5분 간격) ──────────────────────────────────
    async def auto_report(self):
        """5분 간격으로 프로젝트 상태를 AADS로 전송"""
        await asyncio.sleep(30)  # 초기 지연
        while True:
            try:
                status = {}
                for name, cfg in PROJECTS.items():
                    proj_path = cfg["path"]
                    status[name] = {
                        "path": proj_path,
                        "exists": os.path.isdir(proj_path),
                        "manager": cfg["manager"],
                    }

                payload = {
                    "from_agent": AGENT_ID,
                    "to_agent": "AADS_MGR",
                    "message_type": "status_report",
                    "content": {
                        "projects": status,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "agent_id": AGENT_ID,
                    },
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
                url = f"{AADS_SERVER}/memory/cross-message"
                session = await self._get_session()
                async with session.post(url, json=payload) as resp:
                    logger.info(f"[AUTO_REPORT] → HTTP {resp.status}")
            except Exception as e:
                logger.error(f"[AUTO_REPORT_ERR] {e}")

            await asyncio.sleep(REPORT_INTERVAL)

    # ── 백그라운드: collect_conversations (5분 간격) ─────────────────────────
    async def collect_conversations(self):
        """5분 간격으로 go100/shortflow 최신 대화/로그 수집 후 AADS 전송"""
        await asyncio.sleep(60)  # 초기 지연 (auto_report와 엇갈리게)
        while True:
            try:
                for name, cfg in PROJECTS.items():
                    conversations = []
                    for log_dir in cfg.get("log_dirs", []):
                        if not os.path.isdir(log_dir):
                            continue
                        files = sorted(
                            glob.glob(f"{log_dir}/*"),
                            key=os.path.getmtime,
                            reverse=True,
                        )
                        for fpath in files[:2]:
                            try:
                                with open(fpath) as f:
                                    lines = f.readlines()
                                conversations.append({
                                    "file": os.path.basename(fpath),
                                    "lines": [l.rstrip() for l in lines[-50:]],
                                    "mtime": datetime.fromtimestamp(
                                        os.path.getmtime(fpath)
                                    ).isoformat(),
                                })
                            except Exception:
                                pass
                        if conversations:
                            break

                    if not conversations:
                        continue

                    payload = {
                        "from_agent": AGENT_ID,
                        "to_agent": cfg["manager"],
                        "message_type": "conversation_collect",
                        "content": {
                            "project": name,
                            "conversations": conversations,
                            "collected_at": datetime.utcnow().isoformat() + "Z",
                        },
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }
                    url = f"{AADS_SERVER}/memory/cross-message"
                    session = await self._get_session()
                    async with session.post(url, json=payload) as resp:
                        logger.info(f"[COLLECT] {name} → HTTP {resp.status}")

            except Exception as e:
                logger.error(f"[COLLECT_ERR] {e}")

            await asyncio.sleep(REPORT_INTERVAL)


# ── 앱 설정 ──────────────────────────────────────────────────────────────────
agent = RemoteAgent()


async def on_startup(app: web.Application):
    asyncio.create_task(agent.auto_report())
    asyncio.create_task(agent.collect_conversations())
    logger.info(f"[START] AADS Remote Agent listening on :{PORT} — agent_id={AGENT_ID}")


async def on_cleanup(app: web.Application):
    if agent._http_session and not agent._http_session.closed:
        await agent._http_session.close()


app = web.Application()
app.router.add_post("/tasks",              agent.handle_task)
app.router.add_get("/tasks/{task_id}",     agent.handle_task_result)
app.router.add_get("/health",              agent.handle_health)
app.router.add_get("/status",              agent.handle_status)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
