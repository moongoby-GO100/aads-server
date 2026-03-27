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

DEFAULT_SERVER_URL = "wss://aads.newtalk.kr"
HTTP_BASE = "https://aads.newtalk.kr"

# ---------------------------------------------------------------------------
# 로깅 설정
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "launcher.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("launcher")


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
def register_startup() -> None:
    """HKCU Run에 런처 등록."""
    if sys.platform != "win32":
        return
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        # PyInstaller EXE면 sys.executable, 아니면 스크립트 경로
        exe_path = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" "{__file__}"'
        winreg.SetValueEx(key, "KakaoBot", 0, winreg.REG_SZ, exe_path)
        winreg.CloseKey(key)
        logger.info("시작프로그램 등록 완료")
    except Exception as e:
        logger.warning("시작프로그램 등록 실패: %s", e)


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

    if getattr(sys, "frozen", False):
        # PyInstaller frozen EXE: importlib로 직접 로드 후 스레드 실행
        import importlib.util

        agent_dir_str = str(AGENT_DIR)
        if agent_dir_str not in sys.path:
            sys.path.insert(0, agent_dir_str)

        spec = importlib.util.spec_from_file_location("agent_module", agent_main)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class _FakeProc:
            """Thread를 Popen 인터페이스처럼 래핑."""
            def __init__(self, t: threading.Thread) -> None:
                self._t = t

            def poll(self) -> int | None:
                return None if self._t.is_alive() else 0

            def terminate(self) -> None:
                pass  # 데몬 스레드는 메인 종료 시 자동 종료

            def wait(self, timeout: float | None = None) -> None:
                self._t.join(timeout=timeout)

        t = threading.Thread(target=mod.main, daemon=True, name="KakaoBotAgent")
        t.start()
        logger.info("에이전트 스레드 시작 (thread=%s)", t.name)
        return _FakeProc(t)
    else:
        # 개발 환경: 시스템 Python으로 subprocess 실행
        proc = subprocess.Popen(
            [sys.executable, str(agent_main)],
            cwd=str(AGENT_DIR),
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
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
        register_startup()

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
        logger.error("에이전트 실행 실패 — 코드를 먼저 다운로드하세요")
        sys.exit(1)

    # 트레이를 별도 스레드에서 실행 (메인 스레드에서 프로세스 감시)
    try:
        from tray import create_tray

        def on_quit():
            """트레이 종료 콜백."""
            if proc and proc.poll() is None:
                proc.terminate()

        tray_thread = threading.Thread(
            target=create_tray,
            args=(cfg, proc, on_quit),
            daemon=True,
        )
        tray_thread.start()
    except ImportError:
        logger.warning("pystray 미설치 — 트레이 아이콘 없이 실행")

    # 4) 에이전트 프로세스 감시 + 주기적 업데이트 확인
    UPDATE_INTERVAL = 3600  # 1시간마다 업데이트 확인
    last_update_check = time.time()

    try:
        while True:
            ret = proc.poll()
            if ret is not None:
                logger.warning("에이전트 종료 (코드 %s) — 5초 후 재시작", ret)
                time.sleep(5)
                proc = run_agent(cfg)
                if proc is None:
                    break

            # 주기적 업데이트 확인
            if time.time() - last_update_check > UPDATE_INTERVAL:
                last_update_check = time.time()
                try:
                    need, remote_ver = check_update(cfg)
                    if need:
                        logger.info("업데이트 발견: %s — 에이전트 재시작", remote_ver)
                        proc.terminate()
                        proc.wait(timeout=10)
                        download_update(cfg, remote_ver)
                        proc = run_agent(cfg)
                except Exception as e:
                    logger.warning("주기적 업데이트 실패: %s", e)

            time.sleep(2)
    except KeyboardInterrupt:
        logger.info("런처 종료 요청")
        if proc and proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main()
