"""
AADS-195: PC 제어 에이전트 — Windows 클라이언트.
WebSocket으로 AADS 서버에 연결, 명령 수신/실행/결과 반환.
v1.0.12: 4010 수신 시 프로세스 종료 — 중복 인스턴스 ping-pong 해소.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict

try:
    import websockets
except ImportError as _ws_err:
    websockets = None  # type: ignore[assignment]
    logging.getLogger("pc-agent").error("websockets 임포트 실패: %s", _ws_err)

# 명령 모듈 임포트 — COMMAND_HANDLERS만 사용 (개별 임포트 금지: _safe_import 방어 무력화)
try:
    from commands import COMMAND_HANDLERS
except Exception as _cmd_err:
    COMMAND_HANDLERS = {}  # type: ignore[assignment,misc]
    logging.getLogger("pc-agent").error("commands 임포트 실패: %s", _cmd_err)

# updater는 자동업데이트 루프에서 직접 참조 필요 (방어적)
try:
    from commands import updater
except ImportError:
    updater = None  # type: ignore[assignment]

# screen_stream은 WebSocket 참조가 필요하므로 별도 임포트 (방어적)
try:
    from commands.screen_stream import get_streamer
except ImportError:
    get_streamer = None  # type: ignore[assignment]

# ── 경로/로깅 ──────────────────────────────────────────────────────────
INSTALL_DIR = Path(os.environ.get(
    "KAKAOBOT_INSTALL_DIR",
    os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local")),
        "KakaoBot",
    ),
))
CONFIG_PATH = INSTALL_DIR / "config.json"
AGENT_START_COUNT_FILE = INSTALL_DIR / ".agent_start_count"

# PyInstaller --windowed 환경: sys.stderr=None → StreamHandler 사용 불가
# FileHandler만 사용하여 깜박임 방지
_log_dir = str(INSTALL_DIR / "logs")
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(os.path.join(_log_dir, "agent.log"), encoding="utf-8")],
)
logger = logging.getLogger("pc-agent")

# ── 설정 ─────────────────────────────────────────────────────────────────

SERVER_URL = os.getenv("AADS_SERVER_URL", "wss://aads.newtalk.kr/api/v1/pc-agent/ws")
AGENT_SECRET = os.getenv("AADS_AGENT_TOKEN", os.getenv("PC_AGENT_SECRET", ""))
HEARTBEAT_INTERVAL = 25  # 초
RECONNECT_DELAY = 5  # 초
MAX_RECONNECT_DELAY = 30  # 초 — 지수 백오프 상한 (60→30으로 단축)
MAX_RECONNECT_DURATION = 300  # 초 — 5분 연속 재연결 실패 시 프로세스 종료 → launcher가 재시작
AUTO_UPDATE_INTERVAL = 600  # 초 — 10분마다 서버 버전 확인 (v1.0.38: 300→600 빈도 절감)


def _hidden_subprocess_kwargs() -> dict[str, int]:
    """Windows 상태 점검 명령이 콘솔 창을 만들지 않게 한다."""
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

# ── 단일 인스턴스 (Windows 뮤텍스) ────────────────────────────────────────

_win_mutex = None  # 전역 참조 유지 (GC 방지)


def _acquire_single_instance() -> bool:
    """Windows named mutex로 단일 인스턴스 보장.

    이미 이 프로세스에서 뮤텍스를 획득했으면 True 반환 (launcher 재시작 루프 대응).
    파일잠금보다 안정적: race condition 없음, 프로세스 종료 시 자동 해제.
    """
    global _win_mutex
    if sys.platform != "win32":
        return True
    if _win_mutex is not None:
        # 이 프로세스에서 이미 획득됨 — launcher가 에이전트 스레드를 재시작하는 경우
        logger.debug("뮤텍스 이미 획득됨 (재시작) — 통과")
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _win_mutex = kernel32.CreateMutexW(None, True, "KakaoBotAgent_SingleInstance_v2")
        last_err = kernel32.GetLastError()
        if last_err == 183:  # ERROR_ALREADY_EXISTS = 다른 프로세스가 이미 실행 중
            logger.warning("이미 실행 중인 에이전트 (다른 프로세스) — 이 인스턴스 종료")
            kernel32.CloseHandle(_win_mutex)
            _win_mutex = None
            return False
        return True
    except Exception as e:
        logger.debug("뮤텍스 생성 실패 (무시): %s", e)
        return True  # 뮤텍스 실패 시 실행 허용


def release_single_instance() -> None:
    """현재 프로세스가 보유한 Windows agent mutex를 명시적으로 해제."""
    global _win_mutex
    if sys.platform != "win32" or _win_mutex is None:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.ReleaseMutex(_win_mutex)
        kernel32.CloseHandle(_win_mutex)
        logger.info("에이전트 단일 인스턴스 뮤텍스 해제 완료")
    except Exception as e:
        logger.debug("에이전트 뮤텍스 해제 실패 (무시): %s", e)
    finally:
        _win_mutex = None


# ── 유틸리티 ──────────────────────────────────────────────────────────────

def _get_persistent_agent_id() -> str:
    """config.json에서 영속 agent_id를 읽거나, 없으면 생성하여 저장."""
    # 1) config.json에서 읽기 (최우선)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if cfg.get("agent_id"):
            agent_id = cfg["agent_id"]
            os.environ["AADS_AGENT_ID"] = agent_id
            return agent_id
    except Exception:
        pass

    # 2) 새 ID 생성 + config.json에 저장
    new_id = str(uuid.uuid4())[:12]
    try:
        cfg = {}
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg["agent_id"] = new_id
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("새 agent_id 생성+저장: %s", new_id)
    except Exception as e:
        logger.warning("agent_id 저장 실패: %s", e)
    os.environ["AADS_AGENT_ID"] = new_id
    return new_id


def _is_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_agent_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _node_role() -> str:
    return str(
        os.getenv("AADS_PC_AGENT_NODE_ROLE", "")
        or _read_agent_config().get("node_role")
        or "interactive"
    ).strip().lower()


def _increment_agent_start_count() -> int:
    try:
        count = int(AGENT_START_COUNT_FILE.read_text(encoding="utf-8").strip() or "0") + 1
    except Exception:
        count = 1
    try:
        AGENT_START_COUNT_FILE.write_text(str(count), encoding="utf-8")
    except Exception:
        pass
    return count


def _collect_capabilities() -> list[str]:
    caps = {"pc_control"}
    if "browser_launch" in COMMAND_HANDLERS:
        caps.update({"chrome_cdp", "interactive_browser"})
    if "ollama_chat" in COMMAND_HANDLERS:
        caps.add("pc_ollama")
    if "local_model_queue_status" in COMMAND_HANDLERS:
        caps.add("local_model_manager")

    extra_caps = os.getenv("AADS_PC_AGENT_CAPABILITIES", "")
    if extra_caps:
        for item in extra_caps.split(","):
            norm = item.strip().lower().replace("-", "_")
            if norm:
                caps.add(norm)

    if _is_truthy(os.getenv("AADS_PC_AGENT_ENABLE_VVIC", "")):
        caps.add("vvic")

    if _node_role() == "windows_e2e":
        caps.add("windows_e2e")

    return sorted(caps)


def _collect_command_types() -> list[str]:
    command_types = {str(key).strip().lower() for key in COMMAND_HANDLERS.keys() if str(key).strip()}
    if "shell" in command_types:
        command_types.update({"cmd", "powershell"})
    return sorted(command_types)


class PCAgent:
    """PC 제어 에이전트 클라이언트."""

    def __init__(self) -> None:
        # 단일 인스턴스 — main() 우회 시(launcher가 직접 PCAgent().run() 호출)에도 동작
        if not _acquire_single_instance():
            import time as _time
            for _retry in range(3):
                logger.info("뮤텍스 대기 (%d/3)...", _retry + 1)
                _time.sleep(2)
                if _acquire_single_instance():
                    break
            else:
                logger.warning("뮤텍스 미획득 — 다른 인스턴스 실행 중, run()에서 즉시 종료")
                self._init_ok = False
                self._running = False
                self._exit_for_update = False
                self.is_connected = False
                self.agent_id = ""
                self.hostname = ""
                self.os_info = ""
                self._loop = None
                self._ws = None
                self._last_server_message_monotonic = None
                return
        self._init_ok = True
        self.agent_id = _get_persistent_agent_id()
        self.hostname = platform.node()
        self.os_info = f"{platform.system()} {platform.release()} {platform.version()}"
        self._running = True
        self._exit_for_update = False
        self.is_connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any | None = None
        self._last_server_message_monotonic: float | None = None
        self._started_at = time.time()
        self._agent_start_count = _increment_agent_start_count()
        self._telemetry_cache: dict[str, Any] = {}
        self._telemetry_cached_at = 0.0

    def _get_version(self) -> str:
        """VERSION 파일에서 에이전트 버전 읽기."""
        try:
            vf = Path(os.path.dirname(os.path.abspath(__file__))) / "VERSION"
            if vf.exists():
                return vf.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return "unknown"

    def _runtime_telemetry(self) -> dict[str, Any]:
        """Collect auto-recovery state; cached to keep heartbeat inexpensive."""
        now = time.time()
        if self._telemetry_cache and now - self._telemetry_cached_at < 60:
            return dict(self._telemetry_cache)

        watchdog: dict[str, Any] = {"registered": False, "platform": sys.platform}
        startup: dict[str, Any] = {"registered": False, "platform": sys.platform}
        if sys.platform == "win32":
            try:
                result = subprocess.run(
                    ["schtasks", "/Query", "/TN", "KakaoBotWatchdog", "/FO", "LIST"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    **_hidden_subprocess_kwargs(),
                )
                watchdog = {
                    "registered": result.returncode == 0,
                    "return_code": result.returncode,
                    "summary": (result.stdout or result.stderr or "").strip()[:1000],
                }
            except Exception as exc:
                watchdog = {"registered": False, "error": type(exc).__name__}
            legacy_registry_present = False
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                )
                try:
                    value, _ = winreg.QueryValueEx(key, "KakaoBot")
                    legacy_registry_present = bool(str(value).strip())
                finally:
                    winreg.CloseKey(key)
            except FileNotFoundError:
                pass
            except Exception as exc:
                startup = {"registered": False, "error": type(exc).__name__}
            if "error" not in startup:
                startup_cmd = (
                    Path(os.environ.get("APPDATA", ""))
                    / "Microsoft/Windows/Start Menu/Programs/Startup/AADS-PC-Agent-Watchdog.cmd"
                )
                legacy_startup_cmd_present = startup_cmd.exists()
                startup = {
                    "registered": not legacy_registry_present and not legacy_startup_cmd_present,
                    "mode": "scheduled_task_hidden",
                    "legacy_registry_present": legacy_registry_present,
                    "legacy_startup_cmd_present": legacy_startup_cmd_present,
                }

        self._telemetry_cache = {
            "node_role": _node_role(),
            "agent_pid": os.getpid(),
            "launcher_or_parent_pid": os.getppid(),
            "agent_uptime_seconds": int(max(0, now - self._started_at)),
            "agent_start_count": self._agent_start_count,
            "watchdog_task": watchdog,
            "startup_registration": startup,
        }
        self._telemetry_cached_at = now
        return dict(self._telemetry_cache)

    async def run(self) -> None:
        """메인 루프 — 서버 연결 + 재연결 (지수 백오프).

        v1.0.36 (2026-05-28): 서버측 종료 코드(1012/1001/1006/1011)는
        즉시 재연결(1초) — 지수 백오프 안 함. AADS hot-reload 시
        ~6초 다운타임을 ~1초로 단축.
        """
        if not getattr(self, '_init_ok', True):
            logger.warning("초기화 실패 상태 — 60초 대기 후 종료 (빠른 재시작 루프 방지)")
            await asyncio.sleep(60)
            return
        if websockets is None:
            logger.error("websockets 모듈 없음 — 120초 대기 후 종료 (재다운로드 루프 방지)")
            await asyncio.sleep(120)
            return
        logger.info("PC Agent 시작 agent_id=%s hostname=%s", self.agent_id, self.hostname)
        self._loop = asyncio.get_running_loop()
        delay = RECONNECT_DELAY

        reconnect_count = 0
        first_fail_time: float | None = None
        # 서버측 의도/일시 종료 → 즉시 재연결 (지수 백오프 스킵)
        FAST_RECONNECT_CODES = {1000, 1001, 1005, 1006, 1011, 1012}
        FAST_RECONNECT_DELAY = 1

        while self._running:
            fast_reconnect = False
            try:
                reconnect_count += 1
                if reconnect_count > 1:
                    logger.info("재연결 시도 #%d (delay=%ds)", reconnect_count - 1, delay)
                await self._connect()
                delay = RECONNECT_DELAY
                reconnect_count = 0
                first_fail_time = None
            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed as e:
                code = getattr(e, "code", None) or 0
                if code in FAST_RECONNECT_CODES:
                    logger.info(
                        "서버측 종료 (code=%s reason=%s) — 즉시 재연결",
                        code, getattr(e, "reason", ""),
                    )
                    fast_reconnect = True
                    delay = RECONNECT_DELAY
                    reconnect_count = 0
                    first_fail_time = None
                else:
                    logger.error(
                        "연결 종료 (code=%s reason=%s) — %d초 후 재연결",
                        code, getattr(e, "reason", ""), delay,
                    )
                    if first_fail_time is None:
                        first_fail_time = asyncio.get_event_loop().time()
            except Exception as e:
                logger.error("연결 오류: %s — %d초 후 재연결 (시도 #%d)", e, delay, reconnect_count)
                if first_fail_time is None:
                    first_fail_time = asyncio.get_event_loop().time()
            finally:
                self.is_connected = False
            # 5분 연속 재연결 실패 시 프로세스 종료 → launcher watchdog가 클린 재시작
            if first_fail_time is not None:
                elapsed = asyncio.get_event_loop().time() - first_fail_time
                if elapsed > MAX_RECONNECT_DURATION:
                    logger.error("재연결 %d초 연속 실패 — 프로세스 종료 (launcher가 재시작)", int(elapsed))
                    break
            if self._running:
                if fast_reconnect:
                    logger.info("fast_reconnect — %d초 대기", FAST_RECONNECT_DELAY)
                    await asyncio.sleep(FAST_RECONNECT_DELAY)
                else:
                    logger.info("재연결 대기 %d초...", delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, MAX_RECONNECT_DELAY)

        if self._exit_for_update:
            logger.info("자동 업데이트 종료 - exit(42)")
            release_single_instance()
            raise SystemExit(42)

    async def _connect(self) -> None:
        """WebSocket 서버 연결."""
        url = f"{SERVER_URL}/{self.agent_id}"
        if AGENT_SECRET:
            url = f"{url}?token={AGENT_SECRET}"

        logger.info("서버 연결 중: %s", url)

        try:
            async with websockets.connect(
                url,
                ping_interval=15,
                ping_timeout=10,
                close_timeout=10,
                open_timeout=15,
            ) as ws:
                logger.info("서버 연결 성공")
                self._ws = ws

                # 등록 메시지 전송
                await ws.send(json.dumps({
                    "type": "register",
                    "id": str(uuid.uuid4()),
                    "payload": {
                        "hostname": self.hostname,
                        "os_info": self.os_info,
                        "version": self._get_version(),
                        "capabilities": _collect_capabilities(),
                        "command_types": _collect_command_types(),
                    },
                }))

                # WoL용 네트워크 정보 자동 전송
                try:
                    from commands.network import get_primary_mac
                    _net = get_primary_mac()
                    if _net:
                        await ws.send(json.dumps({
                            "type": "network_info",
                            "id": str(uuid.uuid4()),
                            "payload": {
                                "mac_address": _net["mac"],
                                "ip_address": _net["ip"],
                                "interface_name": _net["name"],
                                "hostname": self.hostname,
                            }
                        }))
                        logger.info("WoL 네트워크 정보 전송: MAC=%s IP=%s", _net["mac"], _net["ip"])
                except Exception as e:
                    logger.warning("네트워크 정보 전송 실패 (무시): %s", e)

                self.is_connected = True
                self._mark_server_message()
                logger.info("서버 등록 완료 — WebSocket 연결 활성")

                # 하트비트 + 자동 업데이트 태스크 시작
                heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                update_task = asyncio.create_task(self._auto_update_loop(ws))

                try:
                    async for raw in ws:
                        self._mark_server_message()
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("잘못된 JSON 수신: %s", raw[:100])
                            continue

                        msg_type = msg.get("type", "")

                        if msg_type == "command":
                            asyncio.create_task(self._handle_command(ws, msg))
                        elif msg_type == "heartbeat":
                            pass  # 서버 ACK
                        else:
                            logger.debug("알 수 없는 메시지: %s", msg_type)
                finally:
                    heartbeat_task.cancel()
                    update_task.cancel()
                    self._ws = None
                    self.is_connected = False
                    self._last_server_message_monotonic = None
                    logger.info("WebSocket 연결 해제")

        except websockets.ConnectionClosedError as e:
            if e.code == 4010:
                # 서버가 "다른 인스턴스가 이미 활성" 통보 → 이 프로세스 종료
                logger.warning(
                    "서버가 중복 연결 거부 (4010: %s) — 이 인스턴스 종료",
                    e.reason,
                )
                self._running = False
                return
            raise

    async def _heartbeat(self, ws: Any) -> None:
        """주기적 하트비트 전송. PC 절전 복귀 시 즉시 재연결 트리거."""
        last_beat = asyncio.get_event_loop().time()
        while True:
            try:
                now = asyncio.get_event_loop().time()
                gap = now - last_beat
                if gap > HEARTBEAT_INTERVAL * 3:
                    logger.info("PC 절전 복귀 감지 (gap=%.1fs) — 즉시 재연결", gap)
                    await ws.close(code=1000, reason="sleep_wake_reconnect")
                    break
                last_beat = now
                await ws.send(json.dumps({
                    "type": "heartbeat",
                    "id": str(uuid.uuid4()),
                    "payload": {},
                }))
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except Exception:
                break

    async def _auto_update_loop(self, ws: Any) -> None:
        """주기적 서버 업데이트 확인.

        업데이트가 감지되면 worker를 종료 코드 42로 내리고, launcher가 즉시
        최신 ZIP을 다운로드한 뒤 재기동한다. launcher의 1시간 주기 체크만
        기다리면 은행/브라우저 핫픽스 반영이 지연될 수 있다.

        v1.0.50: ws.state 체크 — WS 끊김 시 좀비 태스크 종료 (좀비 update loop 버그 수정).
        """
        if updater is None:
            logger.warning("updater 모듈 미로드 — 자동 업데이트 비활성화")
            return

        await asyncio.sleep(60)
        while True:
            # v1.0.50: ws 살아있는지 매 사이클 확인 — 좀비 태스크 방지
            try:
                ws_state = getattr(ws, "state", None)
                ws_closed = getattr(ws, "closed", False)
                if ws_closed or (ws_state is not None and str(ws_state).endswith("CLOSED")):
                    logger.info("_auto_update_loop: WS 끊김 감지 — 좀비 방지 종료")
                    return
                if not self.is_connected:
                    logger.info("_auto_update_loop: is_connected=False — 좀비 방지 종료")
                    return
            except Exception:
                # 상태 확인 자체 실패 = ws 비정상 → 종료
                logger.info("_auto_update_loop: ws 상태 확인 실패 — 좀비 방지 종료")
                return
            try:
                has_update = await updater.check_for_updates()
                if has_update:
                    logger.info("업데이트 감지 — launcher 즉시 다운로드를 위해 worker 종료 요청")
                    self._exit_for_update = True
                    self._running = False
                    await ws.close(code=1000, reason="auto_update")
                    return
            except Exception as e:
                logger.debug("자동 업데이트 확인 실패: %s", e)
            await asyncio.sleep(AUTO_UPDATE_INTERVAL)

    async def _handle_command(self, ws: Any, msg: Dict[str, Any]) -> None:
        """명령 실행 및 결과 반환."""
        command_id = msg.get("id", "")
        payload = msg.get("payload", {})
        command_type = payload.get("command_type", "")
        params = payload.get("params", {})

        logger.info("명령 수신 command_id=%s type=%s", command_id, command_type)

        # 스트리밍 명령은 WebSocket 참조가 필요하므로 직접 처리
        if command_type in ("stream_start", "stream_stop") and get_streamer is None:
            result = {"status": "error", "data": {"error": "screen_stream 모듈 미설치"}}
        elif command_type == "stream_start":
            try:
                streamer = get_streamer()
                await streamer.start(ws, params)
                result = {"status": "success", "data": {"message": "스트리밍 시작됨"}}
            except Exception as e:
                logger.error("스트리밍 시작 오류: %s", e)
                result = {"status": "error", "data": {"error": str(e)}}
        elif command_type == "stream_stop":
            try:
                streamer = get_streamer()
                await streamer.stop()
                result = {"status": "success", "data": {"message": "스트리밍 중지됨"}}
            except Exception as e:
                logger.error("스트리밍 중지 오류: %s", e)
                result = {"status": "error", "data": {"error": str(e)}}
        else:
            try:
                result = await self._execute_command(command_type, params)
            except Exception as e:
                logger.error("명령 실행 오류 command_id=%s: %s", command_id, e)
                result = {"status": "error", "data": {"error": str(e)}}

        # 결과 전송
        try:
            await ws.send(json.dumps({
                "type": "result",
                "id": command_id,
                "payload": result,
            }))
            logger.info("결과 전송 command_id=%s status=%s", command_id, result.get("status"))
        except Exception as e:
            logger.error("결과 전송 실패 command_id=%s: %s", command_id, e)

        if (
            command_type == "self_update"
            and result.get("status") == "ok"
            and bool((result.get("data") or {}).get("restart_requested"))
        ):
            logger.info("self_update 결과 전송 완료 — launcher 재기동 코드 42 요청")
            self._exit_for_update = True
            self._running = False
            await ws.close(code=1000, reason="self_update")

    async def _execute_command(self, command_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """명령 타입에 따른 실행 디스패치."""
        handler = COMMAND_HANDLERS.get(command_type)
        if handler is None:
            return {"status": "error", "data": {"error": f"지원하지 않는 명령: {command_type}"}}

        return await handler(params)

    def _mark_server_message(self) -> None:
        self._last_server_message_monotonic = time.monotonic()

    @property
    def seconds_since_server_message(self) -> float | None:
        if self._last_server_message_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self._last_server_message_monotonic)

    def stop(self) -> None:
        """에이전트 종료."""
        self._running = False
        release_single_instance()
        ws = self._ws
        loop = self._loop
        if ws is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(code=1001, reason="client_stop"), loop)
            except Exception as exc:
                logger.debug("WebSocket 종료 예약 실패: %s", exc)
        logger.info("PC Agent 종료 요청")


def main() -> None:
    """엔트리포인트."""
    agent = PCAgent()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        agent.stop()
        logger.info("PC Agent 종료")
    except Exception as e:
        logger.error("PC Agent 치명적 오류: %s", e, exc_info=True)


if __name__ == "__main__":
    main()
