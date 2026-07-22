"""KakaoBot SaaS — PC Agent 런처.

EXE로 빌드되어 사용자 PC에 설치되는 불변 런처.
첫 실행: 토큰 입력 → config 저장 → 에이전트 다운로드 → 실행.
이후 실행: 버전 확인 → 업데이트 → 에이전트 실행.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 경로 상수
# ---------------------------------------------------------------------------
INSTALL_DIR = Path(os.environ.get(
    "KAKAOBOT_INSTALL_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"), "KakaoBot"),
))
CONFIG_PATH = INSTALL_DIR / "config.json"
AGENT_DIR = INSTALL_DIR / "agent"
LOG_DIR = INSTALL_DIR / "logs"
VERSION_FILE = AGENT_DIR / "VERSION"

DEFAULT_SERVER_URL = "wss://aads.newtalk.kr/api/v1/pc-agent/ws"
HTTP_BASE = "https://aads.newtalk.kr"
CRASH_COUNT_FILE = INSTALL_DIR / ".crash_count"
LAUNCH_COUNT_FILE = INSTALL_DIR / ".launcher_start_count"
MAX_CRASHES_BEFORE_REDOWNLOAD = 3
MAX_REDOWNLOADS_PER_HOUR = 3
SELF_UPDATE_EXIT_CODE = 42

# ---------------------------------------------------------------------------
# 로깅 설정
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "launcher.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("launcher")


def _hidden_subprocess_kwargs() -> dict[str, int]:
    """Windows 보조 명령과 worker를 콘솔 창 없이 실행한다."""
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# ---------------------------------------------------------------------------
# 원격 로그 업로드 핸들러 (v1.0.46) — ERROR/WARNING을 서버로 전송
# ---------------------------------------------------------------------------
class _RemoteLogHandler(logging.Handler):
    """ERROR/WARNING 레벨 로그를 서버 /api/v1/pc-agent/client-log로 비동기 전송."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._stop = False
        self._worker = threading.Thread(target=self._run, daemon=True, name="LauncherRemoteLog")
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            cfg = load_config() or {}
            if not cfg.get("agent_token"):
                return
            ver = "unknown"
            try:
                ver = VERSION_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            entry = {
                "agent_token": cfg["agent_token"],
                "agent_id": cfg.get("agent_id", "unknown"),
                "source": "launcher",
                "level": record.levelname,
                "version": ver,
                "hostname": os.environ.get("COMPUTERNAME", ""),
                "message": self.format(record),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            with self._lock:
                if len(self._queue) < 200:  # 큐 폭주 방지
                    self._queue.append(entry)
        except Exception:
            pass  # 절대 로깅에서 예외 던지지 않음

    def _run(self) -> None:
        import json as _json
        from urllib import request as _req
        while not self._stop:
            time.sleep(10)  # 10초마다 배치 전송
            with self._lock:
                batch = self._queue[:]
                self._queue.clear()
            for entry in batch:
                try:
                    data = _json.dumps(entry).encode()
                    rq = _req.Request(
                        f"{HTTP_BASE}/api/v1/pc-agent/client-log",
                        data=data,
                        headers={"Content-Type": "application/json"},
                    )
                    _req.urlopen(rq, timeout=10).read()
                except Exception:
                    pass  # 네트워크 실패 → 다음 배치에서 재시도하지 않음 (무한 누적 방지)


def _install_remote_log_handler() -> None:
    """로깅 시스템에 원격 핸들러 1회 설치 (중복 방지)."""
    root = logging.getLogger()
    if any(isinstance(h, _RemoteLogHandler) for h in root.handlers):
        return
    try:
        root.addHandler(_RemoteLogHandler())
    except Exception as e:
        logger.debug("remote_log_handler_install_failed: %s", e)


# ---------------------------------------------------------------------------
# 설정 관리
# ---------------------------------------------------------------------------
def load_config() -> dict | None:
    """config.json 로드. 없으면 None."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("config.json 파싱 실패, 재설정 필요")
    return None


def save_config(cfg: dict) -> None:
    """config.json 저장."""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("설정 저장 완료: %s", CONFIG_PATH)


# ---------------------------------------------------------------------------
# 토큰 입력 UI (tkinter)
# ---------------------------------------------------------------------------
def ask_token_gui() -> str | None:
    """tkinter 다이얼로그로 토큰 입력받기. 취소 시 None."""
    import tkinter as tk
    from tkinter import messagebox

    token_result: list[str | None] = [None]

    root = tk.Tk()
    root.title("KakaoBot 설정")
    root.geometry("420x200")
    root.resizable(False, False)
    # 화면 중앙
    root.update_idletasks()
    x = (root.winfo_screenwidth() - 420) // 2
    y = (root.winfo_screenheight() - 200) // 2
    root.geometry(f"+{x}+{y}")

    tk.Label(root, text="대시보드에서 복사한 토큰을 붙여넣기", font=("맑은 고딕", 11)).pack(pady=(20, 5))
    tk.Label(root, text="(AADS 대시보드 → 설정 → PC Agent 토큰)", font=("맑은 고딕", 9), fg="gray").pack()

    entry = tk.Entry(root, width=48, show="•")
    entry.pack(pady=10)
    entry.focus_set()

    def on_ok(_event=None):
        val = entry.get().strip()
        if not val:
            messagebox.showwarning("입력 필요", "토큰을 입력해주세요.")
            return
        token_result[0] = val
        root.destroy()

    def on_cancel():
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=10)
    tk.Button(btn_frame, text="확인", width=10, command=on_ok).pack(side=tk.LEFT, padx=5)
    tk.Button(btn_frame, text="취소", width=10, command=on_cancel).pack(side=tk.LEFT, padx=5)
    root.bind("<Return>", on_ok)

    root.mainloop()
    return token_result[0]


# ---------------------------------------------------------------------------
# 시작프로그램 등록 (Windows 레지스트리)
# ---------------------------------------------------------------------------
def _get_crash_count() -> int:
    """크래시 카운터 읽기."""
    try:
        return int(CRASH_COUNT_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _set_crash_count(n: int) -> None:
    """크래시 카운터 저장."""
    try:
        CRASH_COUNT_FILE.write_text(str(n), encoding="utf-8")
    except Exception:
        pass


def _can_redownload() -> tuple[bool, int]:
    """시간당 MAX_REDOWNLOADS_PER_HOUR 이내인지 확인.

    반환: (허용 여부, 최근 1시간 시도 횟수)
    .redownload_log 에 timestamp(float) 공백 구분 저장. 1시간 초과 항목 제거.
    crash 분기 + exit=0 분기 + 주기 업데이트 분기가 공유하여 무한 다운로드 루프 차단.
    """
    rdl_file = INSTALL_DIR / ".redownload_log"
    now = time.time()
    try:
        entries = [float(x) for x in rdl_file.read_text(encoding="utf-8").split() if x]
    except Exception:
        entries = []
    entries = [t for t in entries if now - t < 3600]
    return (len(entries) < MAX_REDOWNLOADS_PER_HOUR, len(entries))


def _record_redownload() -> None:
    """다운로드 시도 기록 — circuit breaker 카운팅."""
    rdl_file = INSTALL_DIR / ".redownload_log"
    now = time.time()
    try:
        entries = [float(x) for x in rdl_file.read_text(encoding="utf-8").split() if x]
    except Exception:
        entries = []
    entries = [t for t in entries if now - t < 3600]
    entries.append(now)
    try:
        rdl_file.write_text(" ".join(str(t) for t in entries), encoding="utf-8")
    except Exception:
        pass


def _redownload_agent(cfg: dict) -> bool:
    """Repair agent files without mutating VERSION or allowing a downgrade."""
    allowed, used = _can_redownload()
    if not allowed:
        logger.error(
            "강제 재다운로드 횟수 초과 (%d/%d) — 기존 코드로 재시도",
            used,
            MAX_REDOWNLOADS_PER_HOUR,
        )
        return False
    try:
        from updater import (
            _get_local_version,
            _is_remote_newer,
            check_update,
            download_update,
        )

        _need, remote_version = check_update(cfg)
        local_version = _get_local_version()
        if remote_version != local_version and not _is_remote_newer(
            remote_version, local_version
        ):
            logger.error(
                "복구 다운로드 역다운그레이드 차단: local=%s remote=%s",
                local_version,
                remote_version,
            )
            return False
        _record_redownload()
        download_update(cfg, remote_version)
        return True
    except Exception as exc:
        logger.error("복구 다운로드 실패: %s", exc)
        return False


def _increment_launcher_start_count() -> int:
    """Persist how often the launcher itself has started on this Windows node."""
    try:
        count = int(LAUNCH_COUNT_FILE.read_text(encoding="utf-8").strip() or "0") + 1
    except Exception:
        count = 1
    try:
        LAUNCH_COUNT_FILE.write_text(str(count), encoding="utf-8")
    except Exception:
        pass
    return count


def _startup_registration_status() -> dict:
    if sys.platform != "win32":
        return {"registered": False, "platform": sys.platform}
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
        return {"registered": False, "error": type(exc).__name__}

    startup_cmd = (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft/Windows/Start Menu/Programs/Startup/AADS-PC-Agent-Watchdog.cmd"
    )
    legacy_startup_cmd_present = startup_cmd.exists()
    return {
        "registered": not legacy_registry_present and not legacy_startup_cmd_present,
        "mode": "scheduled_task_hidden",
        "legacy_registry_present": legacy_registry_present,
        "legacy_startup_cmd_present": legacy_startup_cmd_present,
    }


def _watchdog_task_status() -> dict:
    if sys.platform != "win32":
        return {"registered": False, "platform": sys.platform}
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "KakaoBotWatchdog", "/FO", "LIST"],
            capture_output=True,
            text=True,
            timeout=10,
            **_hidden_subprocess_kwargs(),
        )
        output = (result.stdout or result.stderr or "").strip()
        return {
            "registered": result.returncode == 0,
            "return_code": result.returncode,
            "summary": output[:1000],
        }
    except Exception as exc:
        return {"registered": False, "error": type(exc).__name__}


def _send_launcher_status(
    cfg: dict,
    *,
    launcher_started_at: float,
    launcher_start_count: int,
    worker_restart_count: int,
    proc,
    disconnected_since: float | None,
) -> None:
    """Send a best-effort heartbeat without blocking the launcher watchdog loop."""
    token = str(cfg.get("agent_token") or "")
    if not token:
        return
    worker_connected = bool(proc and getattr(proc, "is_connected", False))
    payload = {
        "agent_token": token,
        "agent_id": str(cfg.get("agent_id") or "unknown"),
        "hostname": os.environ.get("COMPUTERNAME", ""),
        "version": VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "unknown",
        "node_role": str(cfg.get("node_role") or "interactive"),
        "launcher_pid": os.getpid(),
        "launcher_uptime_seconds": int(max(0, time.time() - launcher_started_at)),
        "launcher_start_count": launcher_start_count,
        "worker_restart_count": worker_restart_count,
        "worker_connected": worker_connected,
        "worker_disconnected_seconds": (
            int(max(0, time.time() - disconnected_since)) if disconnected_since else 0
        ),
        "watchdog_task": _watchdog_task_status(),
        "startup_registration": _startup_registration_status(),
        "reported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    def _post() -> None:
        try:
            from urllib import request as _req

            req = _req.Request(
                f"{HTTP_BASE}/api/v1/pc-agent/launcher-status",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            _req.urlopen(req, timeout=10).read()
        except Exception as exc:
            logger.debug("launcher_status_upload_failed: %s", exc)

    threading.Thread(target=_post, daemon=True, name="LauncherStatusUpload").start()


def register_startup() -> None:
    """중복 실행과 콘솔 깜빡임을 만드는 구형 자동실행 등록을 제거한다."""
    if sys.platform != "win32":
        return
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, "KakaoBot")
            logger.info("구형 HKCU Run 자동실행 제거 완료")
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(key)

        startup_cmd = (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft/Windows/Start Menu/Programs/Startup/AADS-PC-Agent-Watchdog.cmd"
        )
        if startup_cmd.exists():
            startup_cmd.unlink()
            logger.info("구형 시작프로그램 CMD 제거 완료")
    except Exception as e:
        logger.warning("구형 자동실행 정리 실패: %s", e)


def _build_hidden_watchdog_vbs(exe_path: str) -> str:
    """Create a console-free watchdog that starts one launcher instance only."""
    escaped_path = exe_path.replace('"', '""')
    process_name = Path(exe_path).name.replace("'", "''")
    watchdog_path = str(INSTALL_DIR / "aads_pc_agent_watchdog.vbs").replace('"', '""')
    return (
        "Option Explicit\n"
        "Dim shell, service, processes, agentExe, watchdogPath, startupCmd, taskCommand, fso\n"
        f'agentExe = "{escaped_path}"\n'
        f'watchdogPath = "{watchdog_path}"\n'
        'Set shell = CreateObject("WScript.Shell")\n'
        'Set service = GetObject("winmgmts:\\\\.\\root\\cimv2")\n\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\n'
        'startupCmd = shell.ExpandEnvironmentStrings("%APPDATA%") & "\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\AADS-PC-Agent-Watchdog.cmd"\n'
        'taskCommand = "schtasks.exe /Create /TN KakaoBotWatchdog /TR " & Chr(34) & "wscript.exe " & watchdogPath & Chr(34) & " /SC ONLOGON /DELAY 0000:30 /RL LIMITED /F"\n\n'
        "Sub EnforceSingleStartup\n"
        "  On Error Resume Next\n"
        '  shell.RegDelete "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\KakaoBot"\n'
        "  If fso.FileExists(startupCmd) Then fso.DeleteFile startupCmd, True\n"
        "  shell.Run taskCommand, 0, True\n"
        "  Err.Clear\n"
        "  On Error GoTo 0\n"
        "End Sub\n\n"
        "EnforceSingleStartup\n\n"
        "Do\n"
        "  On Error Resume Next\n"
        f'  Set processes = service.ExecQuery("SELECT * FROM Win32_Process WHERE Name=\'{process_name}\'")\n'
        "  If Err.Number = 0 Then\n"
        "    If processes.Count = 0 Then\n"
        "      shell.Run Chr(34) & agentExe & Chr(34), 0, False\n"
        "      WScript.Sleep 5000\n"
        "      EnforceSingleStartup\n"
        "    End If\n"
        "  End If\n"
        "  Err.Clear\n"
        "  On Error GoTo 0\n"
        "  WScript.Sleep 30000\n"
        "Loop\n"
    )


def register_watchdog_task() -> None:
    """로그온 시 콘솔 없는 단일 watchdog을 실행한다."""
    if sys.platform != "win32":
        return
    try:
        if not getattr(sys, "frozen", False):
            logger.info("개발 실행에서는 Windows watchdog 작업 등록을 생략")
            return

        watchdog_path = INSTALL_DIR / "aads_pc_agent_watchdog.vbs"
        watchdog_path.write_text(
            _build_hidden_watchdog_vbs(sys.executable),
            encoding="utf-8-sig",
        )
        task_command = f'wscript.exe "{watchdog_path}"'
        result = subprocess.run(
            ["schtasks", "/Create",
             "/TN", "KakaoBotWatchdog",
             "/TR", task_command,
             "/SC", "ONLOGON", "/DELAY", "0000:30",
             "/RL", "LIMITED",
             "/F"],
            capture_output=True, text=True, timeout=10,
            **_hidden_subprocess_kwargs(),
        )
        if result.returncode == 0:
            logger.info("Task Scheduler 숨김 watchdog 등록 완료 (로그온 후 30초)")
        else:
            logger.warning("Task Scheduler watchdog 등록 실패: %s", result.stderr.strip())
    except Exception as e:
        logger.warning("Task Scheduler watchdog 등록 실패: %s", e)


def disable_watchdog_for_user_exit() -> None:
    """Stop automatic revival only after an explicit full-exit confirmation."""
    if sys.platform != "win32":
        return
    for args in (
        ["schtasks", "/End", "/TN", "KakaoBotWatchdog"],
        ["schtasks", "/Delete", "/TN", "KakaoBotWatchdog", "/F"],
    ):
        try:
            subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=10,
                **_hidden_subprocess_kwargs(),
            )
        except Exception as exc:
            logger.warning("완전 종료 watchdog 해제 실패 (%s): %s", args[1], exc)



# ---------------------------------------------------------------------------
# 에이전트 실행
# ---------------------------------------------------------------------------
def run_agent(cfg: dict):
    """에이전트 실행.

    PyInstaller EXE 환경: sys.executable이 EXE 자신이라 subprocess로 .py 실행 불가.
    → importlib로 agent.py를 직접 로드하여 데몬 스레드로 실행.
    개발 환경: 기존 subprocess 방식 유지.
    """
    agent_main = AGENT_DIR / "agent.py"
    if not agent_main.exists():
        logger.error("에이전트 코드 없음: %s", agent_main)
        return None

    os.environ["AADS_SERVER_URL"] = cfg.get("server_url", DEFAULT_SERVER_URL)
    os.environ["AADS_AGENT_TOKEN"] = cfg.get("agent_token", "")
    os.environ["KAKAOBOT_INSTALL_DIR"] = str(INSTALL_DIR)
    os.environ["AADS_PC_AGENT_NODE_ROLE"] = str(cfg.get("node_role") or "interactive")

    # agent_id 영속화 — 재시작마다 새 UUID 생성하면 서버에 좀비 연결 누적
    import uuid as _uuid
    if not cfg.get("agent_id"):
        cfg["agent_id"] = str(_uuid.uuid4())[:12]
        save_config(cfg)
    os.environ["AADS_AGENT_ID"] = cfg["agent_id"]

    if getattr(sys, "frozen", False):
        # PyInstaller frozen EXE: importlib로 직접 로드 후 스레드 실행
        import importlib.util

        agent_dir_str = str(AGENT_DIR)
        if agent_dir_str not in sys.path:
            sys.path.insert(0, agent_dir_str)

        # 이전 에이전트의 Windows mutex 해제 (누수 방지 — 재시작 시 ERROR_ALREADY_EXISTS 차단)
        old_agent = sys.modules.get("agent_module")
        if old_agent and hasattr(old_agent, 'release_single_instance'):
            try:
                old_agent.release_single_instance()
            except Exception:
                pass


        # 이전 로드로 캐시된 모듈 제거 (재시작 시 stale 모듈 방지)
        stale = [k for k in sys.modules if k.startswith("commands") or k == "agent_module"]
        for k in stale:
            del sys.modules[k]

        spec = importlib.util.spec_from_file_location("agent_module", agent_main)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except ImportError as imp_err:
            logger.error("에이전트 임포트 실패 (코드 손상): %s — 복구 다운로드", imp_err)
            _redownload_agent(cfg)
            return None

        agent_instance = mod.PCAgent()
        exit_state: dict[str, int | None] = {"code": None}

        class _FakeProc:
            """Thread를 Popen 인터페이스처럼 래핑."""
            def __init__(self, t: threading.Thread, agent_obj) -> None:
                self._t = t
                self._agent = agent_obj

            @property
            def is_connected(self) -> bool:
                return bool(self._agent and getattr(self._agent, 'is_connected', False))

            @property
            def seconds_since_server_message(self) -> float | None:
                if not self._agent:
                    return None
                return getattr(self._agent, "seconds_since_server_message", None)

            def poll(self) -> int | None:
                if self._t.is_alive():
                    return None
                return exit_state["code"] if exit_state["code"] is not None else 0

            def terminate(self) -> None:
                if self._agent:
                    self._agent.stop()

            def wait(self, timeout: float | None = None) -> None:
                self._t.join(timeout=timeout)

        def _run_agent():
            import asyncio as _asyncio
            try:
                _asyncio.run(agent_instance.run())
                exit_state["code"] = 0
            except SystemExit as e:
                if isinstance(e.code, int):
                    exit_state["code"] = e.code
                else:
                    exit_state["code"] = 1
                logger.info("에이전트 스레드 종료 신호 수신 (code=%s)", exit_state["code"])
            except Exception as e:
                exit_state["code"] = 1
                logger.error("에이전트 스레드 오류: %s", e)

        t = threading.Thread(target=_run_agent, daemon=True, name="KakaoBotAgent")
        t.start()
        logger.info("에이전트 스레드 시작 (thread=%s)", t.name)
        return _FakeProc(t, agent_instance)
    else:
        # 개발 환경: 시스템 Python으로 subprocess 실행
        kwargs = _hidden_subprocess_kwargs()
        proc = subprocess.Popen(
            [sys.executable, str(agent_main)],
            cwd=str(AGENT_DIR),
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        logger.info("에이전트 시작 (PID %d)", proc.pid)
        return proc


# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------
def main() -> None:
    """런처 메인 진입점."""
    logger.info("=== KakaoBot 런처 시작 ===")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    launcher_started_at = time.time()
    launcher_start_count = _increment_launcher_start_count()

    # 단일 인스턴스 보장 — Windows named mutex
    if sys.platform == "win32":
        try:
            import ctypes
            _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "KakaoBotSaaS_SingleInstance_v1")
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                logger.warning("이미 실행 중인 KakaoBot이 있습니다 — 종료")
                sys.exit(0)
        except Exception as _mx_err:
            logger.debug("뮤텍스 생성 실패 (무시): %s", _mx_err)

    # 1) 설정 로드 / 첫 실행 시 토큰 입력
    cfg = load_config()
    if cfg is None or not cfg.get("agent_token"):
        token = ask_token_gui()
        if not token:
            logger.info("토큰 입력 취소 — 종료")
            sys.exit(0)
        cfg = {
            "server_url": DEFAULT_SERVER_URL,
            "agent_token": token,
        }
        save_config(cfg)

    # 매 실행마다 시작프로그램 등록 보장 (idempotent)
    register_startup()
    register_watchdog_task()

    # 원격 로그 핸들러 설치 — ERROR/WARNING 시 서버에 자동 전송 (v1.0.46)
    _install_remote_log_handler()

    # 1-b) 첫 실행 시 서버에 에이전트 등록
    if not cfg.get("registered"):
        try:
            import json as _json
            from urllib import request as _req
            reg_data = _json.dumps({
                "agent_token": cfg["agent_token"],
                "hostname": os.environ.get("COMPUTERNAME", "unknown"),
                "os_info": sys.platform,
            }).encode()
            reg_req = _req.Request(
                f"{HTTP_BASE}/api/v1/kakao-bot/agent/register",
                data=reg_data,
                headers={"Content-Type": "application/json"},
            )
            with _req.urlopen(reg_req, timeout=15) as resp:
                logger.info("에이전트 등록 완료: %s", resp.read().decode())
            cfg["registered"] = True
            save_config(cfg)
        except Exception as e:
            logger.warning("에이전트 등록 실패 (나중에 재시도): %s", e)

    # 2) 업데이트 확인 + 다운로드
    from updater import check_update, download_update
    try:
        need, remote_ver = check_update(cfg)
        if need:
            logger.info("업데이트 발견: %s → 다운로드 시작", remote_ver)
            download_update(cfg, remote_ver)
    except Exception as e:
        logger.warning("업데이트 확인 실패 (오프라인?): %s", e)

    # 3) 트레이 아이콘 + 에이전트 실행
    proc = run_agent(cfg)
    if proc is None:
        # ImportError 등으로 코드 손상 → 강제 재다운로드 후 1회 재시도
        logger.warning("에이전트 실행 실패 — 강제 재다운로드 후 재시도")
        if _redownload_agent(cfg):
            proc = run_agent(cfg)
        if proc is None:
            try:
                import tkinter as _tk
                from tkinter import messagebox as _mb
                _r = _tk.Tk(); _r.withdraw()
                _mb.showerror("KakaoBot", "에이전트 실행 실패.\n에이전트 코드를 다운로드할 수 없습니다.")
                _r.destroy()
            except Exception:
                pass
            sys.exit(1)

    # 설치/실행 성공 알림 — 최초 1회만
    if not cfg.get("setup_shown"):
        try:
            import tkinter as _tk2
            from tkinter import messagebox as _mb2
            _r2 = _tk2.Tk(); _r2.withdraw()
            _mb2.showinfo("KakaoBot", "설치 완료! 에이전트가 실행 중입니다.\n시스템 트레이에서 상태를 확인하세요.")
            _r2.destroy()
        except Exception:
            pass
        cfg["setup_shown"] = True
        save_config(cfg)

    # 트레이를 별도 스레드에서 실행 (메인 스레드에서 프로세스 감시)
    stop_requested = threading.Event()
    # mutable container로 전달하여 launcher가 proc을 교체해도 tray가 최신 인스턴스를 참조
    proc_ref = [proc]
    try:
        from tray import create_tray

        def on_quit():
            """트레이 종료 콜백."""
            disable_watchdog_for_user_exit()
            stop_requested.set()
            p = proc_ref[0]
            if p and p.poll() is None:
                p.terminate()

        tray_thread = threading.Thread(
            target=create_tray,
            args=(cfg, proc_ref, on_quit),
            daemon=True,
        )
        tray_thread.start()
    except ImportError:
        logger.warning("pystray 미설치 — 트레이 아이콘 없이 실행")

    # 4) 에이전트 프로세스 감시 + 주기적 업데이트 확인
    UPDATE_INTERVAL = 3600  # 1시간마다 업데이트 확인
    RECONNECT_WATCHDOG_TIMEOUT = 120  # 120초 이상 미연결 시 강제 재시작
    WORKER_ACTIVITY_TIMEOUT = 120  # 트레이만 살아 있고 WebSocket worker가 멈춘 상태 감지
    last_update_check = time.time()
    last_status_upload = 0.0
    disconnected_since = None
    worker_restart_count = 0
    observed_proc = proc
    _set_crash_count(0)  # 정상 시작 시 크래시 카운터 리셋

    try:
        while True:
            proc_ref[0] = proc  # tray가 항상 최신 에이전트 인스턴스를 참조
            if proc is not None and proc is not observed_proc:
                worker_restart_count += 1
                observed_proc = proc
            if time.time() - last_status_upload >= 60:
                last_status_upload = time.time()
                _send_launcher_status(
                    cfg,
                    launcher_started_at=launcher_started_at,
                    launcher_start_count=launcher_start_count,
                    worker_restart_count=worker_restart_count,
                    proc=proc,
                    disconnected_since=disconnected_since,
                )
            if proc is None:
                logger.error("proc is None — 에이전트 복구 시도")
                time.sleep(5)
                try:
                    proc = run_agent(cfg)
                except (SystemExit, Exception) as _e:
                    logger.error("에이전트 복구 실패: %s", _e)
                if proc is None:
                    time.sleep(30)
                    continue
            ret = proc.poll()
            if ret is not None:
                if stop_requested.is_set():
                    logger.info("사용자 종료 요청으로 런처 루프 종료")
                    break

                if ret == SELF_UPDATE_EXIT_CODE:
                    logger.info("self_update 종료 신호 감지 — 최신 agent ZIP 다운로드 후 재기동")
                    _set_crash_count(0)
                    try:
                        need, remote_ver = check_update(cfg)
                        if need:
                            download_update(cfg, remote_ver)
                            # 다운로드 성공 후 retry 카운터 리셋
                            retry_file = INSTALL_DIR / ".update_retry_count"
                            try:
                                retry_file.write_text("0", encoding="utf-8")
                            except Exception:
                                pass
                        else:
                            logger.info("self_update 신호였지만 서버 버전과 로컬 버전이 이미 일치")
                            # 이미 일치 = 다운로드 불필요 → retry 카운터 리셋
                            retry_file = INSTALL_DIR / ".update_retry_count"
                            try:
                                retry_file.write_text("0", encoding="utf-8")
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error("self_update 다운로드 실패 — 기존 코드로 재기동 시도: %s", e)
                    time.sleep(5)
                    try:
                        proc = run_agent(cfg)
                    except SystemExit:
                        logger.error("self_update run_agent() SystemExit — mutex 충돌")
                        proc = None
                    if proc is None:
                        continue
                    continue

                if ret == 0:
                    try:
                        need_upd, rv = check_update(cfg)
                        if need_upd:
                            ok_dl, used = _can_redownload()
                            if not ok_dl:
                                logger.error(
                                    "exit=0 VERSION mismatch but redownload circuit OPEN (%d/%d) — skip download",
                                    used, MAX_REDOWNLOADS_PER_HOUR,
                                )
                                time.sleep(60)
                                try:
                                    proc = run_agent(cfg)
                                except SystemExit:
                                    proc = None
                                if proc is None:
                                    continue
                                continue
                            logger.info("exit=0 VERSION mismatch - self-update (redownload %d/%d)", used + 1, MAX_REDOWNLOADS_PER_HOUR)
                            _set_crash_count(0)
                            _record_redownload()
                            download_update(cfg, rv)
                            time.sleep(3)
                            try:
                                proc = run_agent(cfg)
                            except SystemExit:
                                logger.error("exit=0 update run_agent() SystemExit — mutex 충돌, 재시도")
                                proc = None
                            if proc is None:
                                # 무한 재시도 (break 금지) — 다음 루프에서 None 복구 분기로 진입
                                continue
                            continue
                    except Exception:
                        pass
                    # exit code 0 with matching version = normal exit, not a crash
                    logger.info("에이전트 정상 종료 (코드 0, 버전 일치) — 크래시 카운트 생략하고 재기동")
                    time.sleep(5)
                    try:
                        proc = run_agent(cfg)
                    except SystemExit:
                        logger.error("exit=0 run_agent() SystemExit — mutex 충돌, 재시도")
                        proc = None
                    if proc is None:
                        continue
                    continue

                crash_n = _get_crash_count() + 1
                _set_crash_count(crash_n)
                logger.warning("에이전트 종료 (코드 %s) — 크래시 %d회", ret, crash_n)

                if crash_n >= MAX_CRASHES_BEFORE_REDOWNLOAD:
                    logger.warning("크래시 %d회 → 에이전트 코드 복구 다운로드", crash_n)
                    if _redownload_agent(cfg):
                        _set_crash_count(0)

                time.sleep(5)
                try:
                    proc = run_agent(cfg)
                except SystemExit:
                    logger.error("run_agent() SystemExit — mutex 충돌, 재시도")
                    proc = None
                if proc is None:
                    continue

            # 주기적 업데이트 확인
            if time.time() - last_update_check > UPDATE_INTERVAL:
                last_update_check = time.time()
                try:
                    need, remote_ver = check_update(cfg)
                    ok_dl, used = (True, 0)
                    if need:
                        ok_dl, used = _can_redownload()
                    if need and not ok_dl:
                        logger.error(
                            "주기 업데이트 발견(%s) but redownload circuit OPEN (%d/%d) — skip",
                            remote_ver, used, MAX_REDOWNLOADS_PER_HOUR,
                        )
                    elif need:
                        logger.info("업데이트 발견: %s — 에이전트 재시작 (redownload %d/%d)", remote_ver, used + 1, MAX_REDOWNLOADS_PER_HOUR)
                        proc.terminate()
                        proc.wait(timeout=10)
                        _record_redownload()
                        download_update(cfg, remote_ver)
                        _new_proc = None
                        for _retry in range(3):
                            try:
                                _new_proc = run_agent(cfg)
                                if _new_proc is not None:
                                    break
                            except (SystemExit, Exception) as _re:
                                logger.error("update run_agent() 실패 (시도 %d/3): %s", _retry + 1, _re)
                                time.sleep(2)
                        if _new_proc is not None:
                            proc = _new_proc
                        else:
                            logger.error("업데이트 후 에이전트 재시작 실패 — 폴백 재다운로드")
                            _redownload_agent(cfg)
                except Exception as e:
                    logger.warning("주기적 업데이트 실패: %s", e)

            # 연결 상태 watchdog — 장기 미연결 시 에이전트 강제 재시작
            if hasattr(proc, 'is_connected') and proc.is_connected:
                worker_stale_seconds = getattr(proc, "seconds_since_server_message", None)
                if worker_stale_seconds is not None and worker_stale_seconds > WORKER_ACTIVITY_TIMEOUT:
                    logger.warning(
                        "에이전트 worker activity %d초 정지 — 강제 재시작",
                        int(worker_stale_seconds),
                    )
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        pass
                    disconnected_since = None
                    _set_crash_count(0)
                    time.sleep(3)
                    try:
                        proc = run_agent(cfg)
                    except SystemExit:
                        logger.error("worker-stale run_agent() SystemExit — mutex 충돌, 재시도")
                        proc = None
                    if proc is None:
                        continue
            elif hasattr(proc, 'is_connected') and not proc.is_connected:
                if disconnected_since is None:
                    disconnected_since = time.time()
                elif time.time() - disconnected_since > RECONNECT_WATCHDOG_TIMEOUT:
                    logger.warning("에이전트 %d초 이상 미연결 — 강제 재시작", RECONNECT_WATCHDOG_TIMEOUT)
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        pass
                    disconnected_since = None
                    _set_crash_count(0)
                    time.sleep(3)
                    try:
                        proc = run_agent(cfg)
                    except SystemExit:
                        logger.error("watchdog run_agent() SystemExit — mutex 충돌, 재시도")
                        proc = None
                    if proc is None:
                        continue
            else:
                disconnected_since = None

            time.sleep(2)
    except KeyboardInterrupt:
        logger.info("런처 종료 요청")
        try:
            stop_requested.set()
        except NameError:
            pass
        if proc and proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main()
