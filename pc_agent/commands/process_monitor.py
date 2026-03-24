"""AADS: 프로세스 감시 — 등록된 프로세스 생존 여부를 주기적으로 확인."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MONITORS_DIR = os.path.join(os.path.expanduser("~"), ".aads_monitors")
_WATCHES_FILE = os.path.join(_MONITORS_DIR, "watches.json")

# psutil graceful 폴백
try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


@dataclass
class WatchConfig:
    """감시 대상 프로세스 설정."""
    process_name: str
    action: str  # "alert" | "restart"
    restart_command: str = ""
    added_at: str = ""
    last_checked: str = ""
    last_status: str = ""  # "running" | "stopped" | "restarted"
    alert_count: int = 0


class ProcessMonitor:
    """싱글톤 프로세스 감시자 — 30초 간격 체크."""
    _instance: Optional[ProcessMonitor] = None

    def __new__(cls) -> ProcessMonitor:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._watch_list: Dict[str, WatchConfig] = {}
        self._task: Optional[asyncio.Task] = None
        self._load_watches()

    # ── 영구 저장/로드 ──

    def _load_watches(self) -> None:
        """디스크에서 감시 목록 로드."""
        if not os.path.isfile(_WATCHES_FILE):
            return
        try:
            with open(_WATCHES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, wdata in data.items():
                self._watch_list[name] = WatchConfig(**wdata)
            logger.info("프로세스 감시: %d개 대상 로드", len(self._watch_list))
        except Exception as e:
            logger.error("프로세스 감시 목록 로드 실패: %s", e)

    def _save_watches(self) -> None:
        """감시 목록 디스크 저장."""
        try:
            os.makedirs(_MONITORS_DIR, exist_ok=True)
            data = {name: asdict(w) for name, w in self._watch_list.items()}
            with open(_WATCHES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("프로세스 감시 목록 저장 실패: %s", e)

    # ── 프로세스 존재 확인 ──

    def _is_process_running(self, process_name: str) -> bool:
        """프로세스 이름으로 실행 중인지 확인."""
        if _HAS_PSUTIL:
            for proc in psutil.process_iter(["name"]):
                try:
                    if proc.info["name"] and process_name.lower() in proc.info["name"].lower():
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return False

        # psutil 미설치 시 폴백
        try:
            # Windows tasklist
            result = subprocess.run(
                ["tasklist", "/fi", f"imagename eq {process_name}*", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
            if process_name.lower() in result.stdout.lower():
                return True
        except FileNotFoundError:
            # Linux ps
            try:
                result = subprocess.run(
                    ["pgrep", "-fi", process_name],
                    capture_output=True, text=True, timeout=10,
                )
                return result.returncode == 0
            except FileNotFoundError:
                pass
        except Exception as e:
            logger.error("프로세스 확인 실패 [%s]: %s", process_name, e)

        return False

    # ── 핵심 기능 ──

    def add_watch(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """프로세스 감시 등록."""
        if name in self._watch_list:
            return {"status": "error", "data": {"error": f"이미 감시 중: {name}"}}

        process_name = config.get("process_name", "")
        if not process_name:
            return {"status": "error", "data": {"error": "process_name 필수"}}

        action = config.get("action", "alert")
        if action not in ("alert", "restart"):
            return {"status": "error", "data": {"error": f"지원하지 않는 action: {action} (alert/restart)"}}

        if action == "restart" and not config.get("restart_command"):
            return {"status": "error", "data": {"error": "restart action은 restart_command 필수"}}

        watch = WatchConfig(
            process_name=process_name,
            action=action,
            restart_command=config.get("restart_command", ""),
            added_at=datetime.now().isoformat(),
        )
        self._watch_list[name] = watch
        self._save_watches()
        self._ensure_loop()

        return {"status": "success", "data": {
            "name": name,
            "process_name": process_name,
            "action": action,
            "running": self._is_process_running(process_name),
        }}

    def remove_watch(self, name: str) -> Dict[str, Any]:
        """감시 해제."""
        if name not in self._watch_list:
            return {"status": "error", "data": {"error": f"감시 대상 없음: {name}"}}
        del self._watch_list[name]
        self._save_watches()
        return {"status": "success", "data": {"removed": name}}

    def list_watches(self) -> Dict[str, Any]:
        """감시 목록 + 현재 상태 반환."""
        watches = []
        for name, w in self._watch_list.items():
            running = self._is_process_running(w.process_name)
            watches.append({
                "name": name,
                "process_name": w.process_name,
                "action": w.action,
                "restart_command": w.restart_command,
                "running": running,
                "last_checked": w.last_checked,
                "last_status": w.last_status,
                "alert_count": w.alert_count,
            })
        return {"status": "success", "data": {"watches": watches, "count": len(watches)}}

    # ── 체크 루프 ──

    def _ensure_loop(self) -> None:
        """체크 루프 실행 보장."""
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    self._task = loop.create_task(self._check_loop())
            except RuntimeError:
                pass

    async def _check_loop(self) -> None:
        """30초 간격으로 프로세스 생존 확인."""
        while True:
            try:
                await asyncio.sleep(30)
                now = datetime.now().isoformat()

                for name, watch in list(self._watch_list.items()):
                    running = self._is_process_running(watch.process_name)
                    watch.last_checked = now

                    if running:
                        watch.last_status = "running"
                        continue

                    # 프로세스 종료 감지
                    watch.alert_count += 1
                    logger.warning(
                        "프로세스 감시 [%s]: %s 종료 감지 (action=%s, count=%d)",
                        name, watch.process_name, watch.action, watch.alert_count,
                    )

                    if watch.action == "restart" and watch.restart_command:
                        try:
                            subprocess.Popen(
                                watch.restart_command,
                                shell=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            watch.last_status = "restarted"
                            logger.info(
                                "프로세스 감시 [%s]: %s 재시작 명령 실행",
                                name, watch.process_name,
                            )
                        except Exception as e:
                            watch.last_status = "stopped"
                            logger.error(
                                "프로세스 감시 [%s]: 재시작 실패 — %s", name, e,
                            )
                    else:
                        watch.last_status = "stopped"

                self._save_watches()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("프로세스 감시 체크 루프 에러: %s", e)


# 싱글톤 인스턴스
_monitor = ProcessMonitor()


# ── COMMAND_HANDLERS용 핸들러 함수 ──

async def monitor_add(params: Dict[str, Any]) -> Dict[str, Any]:
    """프로세스 감시 등록. params: name, config{process_name, action, restart_command}"""
    name = params.get("name", "")
    if not name:
        return {"status": "error", "data": {"error": "name 파라미터 필수"}}

    config = params.get("config", {})
    if not config:
        return {"status": "error", "data": {
            "error": "config 파라미터 필수 (process_name, action, restart_command)",
        }}

    return _monitor.add_watch(name, config)


async def monitor_remove(params: Dict[str, Any]) -> Dict[str, Any]:
    """프로세스 감시 해제. params: name"""
    name = params.get("name", "")
    if not name:
        return {"status": "error", "data": {"error": "name 파라미터 필수"}}
    return _monitor.remove_watch(name)


async def monitor_list(params: Dict[str, Any]) -> Dict[str, Any]:
    """프로세스 감시 목록 + 현재 상태."""
    return _monitor.list_watches()
