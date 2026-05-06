"""
AADS-195: PC 제어 에이전트 매니저 (싱글톤).
WebSocket 연결 관리, 명령 전송/결과 수신.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import tempfile
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket

from app.models.pc_agent import AgentInfo, CommandResult, StreamConfig, WSMessage

logger = logging.getLogger(__name__)

_DEVICE_COMMAND_WHITELIST = frozenset({
    "screenshot",
    "install_apk",
    "list_apps",
    "tap",
    "swipe",
    "input_text",
    "get_device_info",
})
_DEVICE_COMMAND_BLACKLIST = frozenset({
    "factory_reset",
    "root",
    "device_wipe",
    "wipe",
    "reboot_bootloader",
    "reboot_recovery",
})
_ANDROID_AGENT_COMMAND_MAP = {
    "screenshot": "screenshot",
    "list_apps": "app_list",
    "tap": "tap",
    "swipe": "swipe",
    "input_text": "key_input",
}
_ANDROID_DEVICE_INFO_PROPS = {
    "manufacturer": "ro.product.manufacturer",
    "brand": "ro.product.brand",
    "model": "ro.product.model",
    "device": "ro.product.device",
    "android_version": "ro.build.version.release",
    "sdk_int": "ro.build.version.sdk",
}


class _AgentConnection:
    """에이전트 WebSocket 연결 + 메타데이터."""

    def __init__(self, agent_id: str, websocket: WebSocket, info: AgentInfo) -> None:
        self.agent_id = agent_id
        self.websocket = websocket
        self.info = info


class PCAgentManager:
    """PC 에이전트 연결 및 명령 관리 (싱글톤)."""

    def __init__(self) -> None:
        self._agents: Dict[str, _AgentConnection] = {}
        self._pending_commands: Dict[str, asyncio.Event] = {}
        self._results: Dict[str, CommandResult] = {}
        self._streaming_subscribers: Dict[str, Set[WebSocket]] = {}  # agent_id → 대시보드 WS

    # ── 에이전트 등록/해제 ──────────────────────────────────────────

    def register_agent(
        self, agent_id: str, websocket: WebSocket, info: Dict[str, Any]
    ) -> AgentInfo:
        """에이전트 등록."""
        agent_info = AgentInfo(
            agent_id=agent_id,
            hostname=info.get("hostname", ""),
            os_info=info.get("os_info", ""),
        )
        self._agents[agent_id] = _AgentConnection(agent_id, websocket, agent_info)
        logger.info("pc_agent_registered agent_id=%s hostname=%s", agent_id, agent_info.hostname)
        return agent_info

    def unregister_agent(self, agent_id: str, websocket: WebSocket | None = None) -> bool:
        """에이전트 해제.

        websocket이 주어지면 현재 등록된 연결과 같을 때만 해제한다. 같은 agent_id의
        재연결이 새 WebSocket으로 교체된 뒤, 예전 연결의 finally가 새 연결을 지우는
        상황을 막기 위한 guard다.
        """
        conn = self._agents.get(agent_id)
        if conn is None:
            return False
        if websocket is not None and conn.websocket is not websocket:
            logger.info("pc_agent_unregister_skipped_stale agent_id=%s", agent_id)
            return False
        del self._agents[agent_id]
        logger.info("pc_agent_unregistered agent_id=%s", agent_id)
        return True

    # ── 명령 전송/결과 ──────────────────────────────────────────────

    async def send_command(
        self, agent_id: str, command_type: str, params: Dict[str, Any]
    ) -> str:
        """에이전트에 명령 전송, command_id 반환."""
        conn = self._agents.get(agent_id)
        if conn is None:
            raise ValueError(f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")

        command_id = str(uuid.uuid4())

        # 결과 대기용 이벤트 생성
        self._pending_commands[command_id] = asyncio.Event()
        self._results[command_id] = CommandResult(
            command_id=command_id,
            agent_id=agent_id,
        )

        # WebSocket으로 명령 전송
        msg = WSMessage(
            type="command",
            id=command_id,
            payload={"command_type": command_type, "params": params},
        )
        await conn.websocket.send_json(msg.model_dump(mode="json"))
        logger.info(
            "pc_agent_command_sent agent_id=%s command_id=%s type=%s",
            agent_id, command_id, command_type,
        )
        return command_id

    async def get_result(self, command_id: str, timeout: float = 30.0) -> CommandResult:
        """명령 결과 대기."""
        event = self._pending_commands.get(command_id)
        if event is None:
            result = self._results.get(command_id)
            if result:
                return result
            raise ValueError(f"command_id '{command_id}'를 찾을 수 없습니다.")

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            result = self._results.get(command_id)
            if result:
                result.status = "timeout"
                result.completed_at = datetime.utcnow()
            self._pending_commands.pop(command_id, None)
            logger.warning("pc_agent_command_timeout command_id=%s", command_id)
            if result:
                return result
            return CommandResult(
                command_id=command_id, agent_id="", status="timeout",
            )

        self._pending_commands.pop(command_id, None)
        return self._results[command_id]

    def receive_result(self, command_id: str, result: Dict[str, Any]) -> None:
        """에이전트로부터 결과 수신."""
        stored = self._results.get(command_id)
        if stored is None:
            logger.warning("pc_agent_unknown_result command_id=%s", command_id)
            return

        stored.status = result.get("status", "success")
        stored.result = result.get("data")
        stored.completed_at = datetime.utcnow()

        event = self._pending_commands.get(command_id)
        if event:
            event.set()
        logger.info("pc_agent_result_received command_id=%s status=%s", command_id, stored.status)

    def update_heartbeat(self, agent_id: str) -> None:
        """에이전트 하트비트 갱신."""
        conn = self._agents.get(agent_id)
        if conn:
            conn.info.last_heartbeat = datetime.utcnow()

    # ── 스트리밍 ──────────────────────────────────────────────────

    def add_stream_subscriber(self, agent_id: str, ws: WebSocket) -> None:
        """스트리밍 구독자 등록."""
        if agent_id not in self._streaming_subscribers:
            self._streaming_subscribers[agent_id] = set()
        self._streaming_subscribers[agent_id].add(ws)
        logger.info("stream_subscriber_added agent_id=%s total=%d", agent_id, len(self._streaming_subscribers[agent_id]))

    def remove_stream_subscriber(self, agent_id: str, ws: WebSocket) -> int:
        """스트리밍 구독자 해제. 남은 구독자 수 반환."""
        subs = self._streaming_subscribers.get(agent_id)
        if subs:
            subs.discard(ws)
            remaining = len(subs)
            if remaining == 0:
                del self._streaming_subscribers[agent_id]
            logger.info("stream_subscriber_removed agent_id=%s remaining=%d", agent_id, remaining)
            return remaining
        return 0

    async def start_stream(self, agent_id: str, config: StreamConfig) -> str:
        """에이전트에 스트리밍 시작 명령 전송."""
        conn = self._agents.get(agent_id)
        if conn is None:
            raise ValueError(f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")

        command_id = str(uuid.uuid4())
        msg = WSMessage(
            type="command",
            id=command_id,
            payload={"command_type": "stream_start", "params": config.model_dump()},
        )
        await conn.websocket.send_json(msg.model_dump(mode="json"))
        logger.info("stream_start_sent agent_id=%s config=%s", agent_id, config.model_dump())
        return command_id

    async def stop_stream(self, agent_id: str) -> str:
        """에이전트에 스트리밍 중지 명령 전송."""
        conn = self._agents.get(agent_id)
        if conn is None:
            raise ValueError(f"에이전트 '{agent_id}'가 연결되어 있지 않습니다.")

        command_id = str(uuid.uuid4())
        msg = WSMessage(
            type="command",
            id=command_id,
            payload={"command_type": "stream_stop", "params": {}},
        )
        await conn.websocket.send_json(msg.model_dump(mode="json"))
        logger.info("stream_stop_sent agent_id=%s", agent_id)
        return command_id

    async def broadcast_frame(self, agent_id: str, frame_data: str) -> None:
        """모든 구독자에게 스트리밍 프레임 전송."""
        subs = self._streaming_subscribers.get(agent_id)
        if not subs:
            return

        msg = {"type": "stream_frame", "frame": frame_data}
        dead: list[WebSocket] = []
        for ws in subs:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)

        for ws in dead:
            subs.discard(ws)
        if dead:
            logger.debug("stream_dead_subscribers agent_id=%s removed=%d", agent_id, len(dead))
            if not subs:
                del self._streaming_subscribers[agent_id]

    # ── 종료 ──────────────────────────────────────────────────────

    async def close_all_connections(self, reason: str = "server_shutdown") -> int:
        """모든 에이전트 연결을 1012로 정상 종료."""
        closed = 0
        for agent_id, conn in list(self._agents.items()):
            try:
                await conn.websocket.close(code=1012, reason=reason)
                closed += 1
            except Exception:
                pass
        self._agents.clear()
        self._streaming_subscribers.clear()
        logger.info("pc_agent_all_connections_closed count=%d reason=%s", closed, reason)
        return closed

    # ── 조회 ──────────────────────────────────────────────────────

    def list_agents(self) -> list[AgentInfo]:
        """연결된 에이전트 목록."""
        return [conn.info for conn in self._agents.values()]

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """특정 에이전트 정보 조회."""
        conn = self._agents.get(agent_id)
        return conn.info if conn else None

    def connected_count(self) -> int:
        return len(self._agents)

    # ── Android device_command ───────────────────────────────────────

    async def execute_device_command(
        self,
        device_id: str,
        command: str,
        args: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """MCP용 Android 통합 device_command 실행."""
        command = (command or "").strip().lower()
        args = args if isinstance(args, dict) else {}

        if not command:
            return {
                "status": "error",
                "error": "command 필수",
                "allowed_commands": sorted(_DEVICE_COMMAND_WHITELIST),
            }
        nested_commands = {
            str(args.get("command") or "").strip().lower(),
            str(args.get("action") or "").strip().lower(),
            str(args.get("subcommand") or "").strip().lower(),
        }
        if command in _DEVICE_COMMAND_BLACKLIST:
            return {
                "status": "error",
                "error": f"위험 명령 '{command}'은(는) 차단되었습니다.",
            }
        if any(value in _DEVICE_COMMAND_BLACKLIST for value in nested_commands if value):
            return {
                "status": "error",
                "error": "위험 명령 파라미터가 감지되어 요청이 차단되었습니다.",
            }
        if command not in _DEVICE_COMMAND_WHITELIST:
            return {
                "status": "error",
                "error": f"지원하지 않는 command: {command}",
                "allowed_commands": sorted(_DEVICE_COMMAND_WHITELIST),
            }

        if command == "install_apk":
            return await self._execute_adb_device_command(device_id, command, args)

        android_agents = self._list_android_agents()
        if not device_id and len(android_agents) > 1:
            return {
                "status": "error",
                "command": command,
                "backend": "android_agent",
                "error": "여러 Android Agent가 연결되어 있습니다. device_id를 지정해주세요.",
                "available_devices": android_agents,
            }

        agent_id = self._resolve_android_agent_id(device_id)
        if agent_id:
            agent_result = await self._execute_android_agent_command(agent_id, command, args)
            if agent_result.get("status") == "success":
                return agent_result
            if device_id or not agent_result.get("fallback_to_adb"):
                agent_result.pop("fallback_to_adb", None)
                return agent_result

        return await self._execute_adb_device_command(device_id, command, args)

    def _resolve_android_agent_id(self, device_id: str) -> str:
        android_devices = self._list_android_agents()
        if not android_devices:
            return ""

        if device_id:
            match = next(
                (
                    str(device.get("device_id") or "")
                    for device in android_devices
                    if device.get("device_id") == device_id
                ),
                "",
            )
            return match

        if len(android_devices) == 1:
            return str(android_devices[0].get("device_id") or "")

        return ""

    def _list_android_agents(self) -> list[Dict[str, Any]]:
        from app.services.device_manager import device_manager

        android_devices = device_manager.get_devices("android")
        result: list[Dict[str, Any]] = []
        for device in android_devices:
            result.append(
                {
                    "agent_id": device.get("agent_id"),
                    "device_id": device.get("agent_id"),
                    "backend": "android_agent",
                    "status": "connected",
                    "hostname": device.get("hostname", ""),
                    "capabilities": device.get("capabilities", []),
                }
            )
        return result

    async def _execute_android_agent_command(
        self,
        agent_id: str,
        command: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        from app.services.device_manager import device_manager

        capabilities = set(device_manager.get_device_capabilities(agent_id))

        if command == "get_device_info":
            required_caps = {"permission_status", "shell_limited"}
            if not required_caps.issubset(capabilities):
                return {
                    "status": "error",
                    "device_id": agent_id,
                    "command": command,
                    "backend": "android_agent",
                    "error": "연결된 Android Agent가 get_device_info에 필요한 capability를 지원하지 않습니다.",
                    "fallback_to_adb": True,
                }
            return await self._get_android_agent_device_info(agent_id)

        mapped_command = _ANDROID_AGENT_COMMAND_MAP.get(command)
        if not mapped_command or mapped_command not in capabilities:
            return {
                "status": "error",
                "device_id": agent_id,
                "command": command,
                "backend": "android_agent",
                "error": f"연결된 Android Agent가 '{command}' 명령을 지원하지 않습니다.",
                "fallback_to_adb": True,
            }

        normalized_args = self._normalize_android_agent_args(command, args)
        result = await device_manager.send_command(
            agent_id,
            mapped_command,
            normalized_args,
            timeout=self._coerce_float(args.get("timeout"), default=30.0),
        )
        data = result.data or {}
        if result.status != "success":
            return {
                "status": "error",
                "device_id": agent_id,
                "command": command,
                "backend": "android_agent",
                "error": str(data.get("error") or f"Android Agent 명령 실패: {command}"),
                "data": data,
                "fallback_to_adb": False,
            }

        if command == "screenshot":
            data = self._normalize_screenshot_payload(data, args)

        return {
            "status": "success",
            "device_id": agent_id,
            "command": command,
            "backend": "android_agent",
            "data": data,
        }

    async def _get_android_agent_device_info(self, agent_id: str) -> Dict[str, Any]:
        from app.services.device_manager import device_manager

        commands: list[tuple[str, str, Dict[str, Any], float]] = [
            ("permission_status", "permission_status", {}, 15.0),
        ]
        for field, prop in _ANDROID_DEVICE_INFO_PROPS.items():
            commands.append(
                (field, "shell_limited", {"command": f"getprop {prop}"}, 10.0)
            )
        commands.append(
            ("android_id", "shell_limited", {"command": "settings get secure android_id"}, 10.0)
        )
        commands.append(
            ("kernel", "shell_limited", {"command": "uname -a"}, 10.0)
        )

        results = await asyncio.gather(
            *[
                device_manager.send_command(agent_id, command_type, params, timeout=timeout)
                for _, command_type, params, timeout in commands
            ],
            return_exceptions=True,
        )

        device_info = device_manager.get_device(agent_id)
        data: Dict[str, Any] = {
            "agent_id": agent_id,
            "device_type": "android",
            "capabilities": device_manager.get_device_capabilities(agent_id),
        }
        if device_info is not None:
            data["connected_at"] = device_info.connected_at.isoformat()
            if device_info.hostname:
                data["hostname"] = device_info.hostname
            if device_info.os_info:
                data["os_info"] = device_info.os_info

        for (field, _, _, _), result in zip(commands, results):
            if isinstance(result, Exception):
                logger.warning("android_agent_device_info_failed agent_id=%s field=%s err=%s", agent_id, field, result)
                continue
            payload = result.data or {}
            if result.status != "success":
                continue
            if field == "permission_status":
                data["permission_status"] = payload
                package_name = str(payload.get("package") or "").strip()
                if package_name:
                    data["package"] = package_name
                sdk_value = payload.get("sdk_int")
                if sdk_value not in (None, ""):
                    data["sdk_int"] = self._coerce_int(sdk_value)
                continue
            value = str(payload.get("stdout") or "").strip()
            if value:
                data[field] = value

        sdk_value = data.get("sdk_int")
        if isinstance(sdk_value, str):
            data["sdk_int"] = self._coerce_int(sdk_value)

        return {
            "status": "success",
            "device_id": agent_id,
            "command": "get_device_info",
            "backend": "android_agent",
            "data": data,
        }

    def _normalize_android_agent_args(
        self,
        command: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        if command == "input_text":
            return {
                "text": str(args.get("text") or ""),
                "append": bool(args.get("append", True)),
            }
        if command == "swipe":
            return {
                "x1": self._coerce_int(args.get("x1")),
                "y1": self._coerce_int(args.get("y1")),
                "x2": self._coerce_int(args.get("x2")),
                "y2": self._coerce_int(args.get("y2")),
                "duration_ms": self._coerce_int(
                    args.get("duration_ms", args.get("duration", 400)),
                    default=400,
                ),
            }
        if command == "tap":
            return {
                "x": self._coerce_int(args.get("x")),
                "y": self._coerce_int(args.get("y")),
            }
        if command == "screenshot":
            return {
                "timeout_ms": self._coerce_int(args.get("timeout_ms"), default=8000),
            }
        return dict(args)

    async def _execute_adb_device_command(
        self,
        device_id: str,
        command: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        resolved_device_id, error_result = await asyncio.to_thread(
            self._resolve_adb_device_id,
            device_id,
        )
        if error_result is not None:
            return error_result

        try:
            result = await asyncio.to_thread(
                self._execute_adb_device_command_sync,
                resolved_device_id,
                command,
                args,
            )
        except ValueError as exc:
            return {
                "status": "error",
                "device_id": resolved_device_id,
                "command": command,
                "backend": "adb",
                "error": str(exc),
            }
        except Exception as exc:
            logger.exception(
                "device_command_adb_failed device_id=%s command=%s err=%s",
                resolved_device_id,
                command,
                exc,
            )
            return {
                "status": "error",
                "device_id": resolved_device_id,
                "command": command,
                "backend": "adb",
                "error": str(exc),
            }

        result["device_id"] = resolved_device_id
        result["command"] = command
        result["backend"] = "adb"
        return result

    def _resolve_adb_device_id(
        self,
        device_id: str,
    ) -> tuple[str, Dict[str, Any] | None]:
        try:
            devices = self._list_adb_devices()
        except RuntimeError as exc:
            return "", {
                "status": "error",
                "command": "device_command",
                "backend": "adb",
                "error": str(exc),
            }

        connected = [device for device in devices if device.get("status") == "device"]

        if device_id:
            match = next(
                (device for device in devices if device.get("device_id") == device_id),
                None,
            )
            if match is None:
                return "", {
                    "status": "error",
                    "command": "device_command",
                    "backend": "adb",
                    "error": f"Android 디바이스 '{device_id}'가 연결되어 있지 않습니다.",
                    "available_devices": connected,
                }
            if match.get("status") != "device":
                return "", {
                    "status": "error",
                    "command": "device_command",
                    "backend": "adb",
                    "error": (
                        f"Android 디바이스 '{device_id}' 상태가 '{match.get('status')}'입니다. "
                        "USB 디버깅 승인 상태를 확인하세요."
                    ),
                    "available_devices": connected,
                }
            return str(match["device_id"]), None

        if len(connected) == 1:
            return str(connected[0]["device_id"]), None

        if not connected:
            return "", {
                "status": "error",
                "command": "device_command",
                "backend": "adb",
                "error": "연결된 Android 디바이스가 없습니다. USB/ADB 연결 상태를 확인하세요.",
                "available_devices": [],
            }

        return "", {
            "status": "error",
            "command": "device_command",
            "backend": "adb",
            "error": "여러 Android 디바이스가 연결되어 있습니다. device_id를 지정해주세요.",
            "available_devices": connected,
        }

    def _list_adb_devices(self) -> list[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["adb", "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("ADB가 설치되어 있지 않습니다.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ADB 디바이스 조회가 시간 초과되었습니다.") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip() or "adb devices failed"
            raise RuntimeError(f"ADB 디바이스 조회 실패: {stderr}")

        devices: list[Dict[str, Any]] = []
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("List of devices attached"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            item: Dict[str, Any] = {
                "device_id": parts[0],
                "backend": "adb",
                "status": parts[1],
            }
            for token in parts[2:]:
                if ":" not in token:
                    continue
                key, value = token.split(":", 1)
                item[key] = value
            devices.append(item)
        return devices

    def _execute_adb_device_command_sync(
        self,
        device_id: str,
        command: str,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        if command == "screenshot":
            return self._adb_screenshot(device_id, args)
        if command == "install_apk":
            return self._adb_install_apk(device_id, args)
        if command == "list_apps":
            return self._adb_list_apps(device_id, args)
        if command == "tap":
            return self._adb_tap(device_id, args)
        if command == "swipe":
            return self._adb_swipe(device_id, args)
        if command == "input_text":
            return self._adb_input_text(device_id, args)
        if command == "get_device_info":
            return self._adb_get_device_info(device_id)
        raise ValueError(f"지원하지 않는 device_command: {command}")

    def _adb_screenshot(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = self._run_adb(device_id, "exec-out", "screencap", "-p", timeout=20)
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("screenshot", result))

        png_bytes = bytes(result.stdout or b"")
        if not png_bytes:
            raise ValueError("스크린샷 데이터를 받지 못했습니다.")

        path = self._store_screenshot_bytes(
            png_bytes,
            str(args.get("output_path") or "").strip(),
        )
        data: Dict[str, Any] = {
            "path": path,
            "mime": "image/png",
            "bytes": len(png_bytes),
        }
        if args.get("include_base64"):
            data["base64"] = base64.b64encode(png_bytes).decode("ascii")
        return {"status": "success", "data": data}

    def _adb_install_apk(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        apk_path = str(args.get("apk_path") or args.get("path") or "").strip()
        if not apk_path:
            raise ValueError("install_apk에는 args.apk_path가 필요합니다.")
        if not os.path.exists(apk_path):
            raise ValueError(f"APK 파일을 찾을 수 없습니다: {apk_path}")

        adb_args = ["install"]
        if bool(args.get("replace", True)):
            adb_args.append("-r")
        if bool(args.get("grant_permissions", False)):
            adb_args.append("-g")
        adb_args.append(apk_path)
        result = self._run_adb(device_id, *adb_args, timeout=180, text=True)
        if result.returncode != 0 or "Success" not in (result.stdout or ""):
            raise ValueError(self._format_adb_error("install_apk", result))

        return {
            "status": "success",
            "data": {
                "apk_path": apk_path,
                "stdout": (result.stdout or "").strip(),
                "replaced": bool(args.get("replace", True)),
            },
        }

    def _adb_list_apps(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        adb_args = ["shell", "pm", "list", "packages"]
        if bool(args.get("third_party_only", False)):
            adb_args.append("-3")
        result = self._run_adb(device_id, *adb_args, timeout=20, text=True)
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("list_apps", result))

        packages = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.append(line.split("package:", 1)[1].strip())

        return {
            "status": "success",
            "data": {
                "packages": packages,
                "count": len(packages),
                "third_party_only": bool(args.get("third_party_only", False)),
            },
        }

    def _adb_tap(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        x = self._coerce_int(args.get("x"))
        y = self._coerce_int(args.get("y"))
        if x is None or y is None:
            raise ValueError("tap에는 args.x, args.y가 필요합니다.")
        result = self._run_adb(
            device_id,
            "shell",
            "input",
            "tap",
            str(x),
            str(y),
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("tap", result))
        return {"status": "success", "data": {"x": x, "y": y}}

    def _adb_swipe(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        x1 = self._coerce_int(args.get("x1"))
        y1 = self._coerce_int(args.get("y1"))
        x2 = self._coerce_int(args.get("x2"))
        y2 = self._coerce_int(args.get("y2"))
        if None in (x1, y1, x2, y2):
            raise ValueError("swipe에는 args.x1, args.y1, args.x2, args.y2가 필요합니다.")
        duration_ms = self._coerce_int(
            args.get("duration_ms", args.get("duration", 400)),
            default=400,
        )
        result = self._run_adb(
            device_id,
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("swipe", result))
        return {
            "status": "success",
            "data": {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "duration_ms": duration_ms,
            },
        }

    def _adb_input_text(self, device_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        text = str(args.get("text") or "")
        if not text:
            raise ValueError("input_text에는 args.text가 필요합니다.")

        result = self._run_adb(
            device_id,
            "shell",
            "input",
            "text",
            self._escape_adb_text(text),
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(self._format_adb_error("input_text", result))
        return {
            "status": "success",
            "data": {
                "text": text,
                "length": len(text),
            },
        }

    def _adb_get_device_info(self, device_id: str) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "device_id": device_id,
            "device_type": "android",
            "state": self._run_adb(
                device_id,
                "get-state",
                timeout=10,
                text=True,
            ).stdout.strip(),
        }

        for field, prop in _ANDROID_DEVICE_INFO_PROPS.items():
            value = self._run_adb(
                device_id,
                "shell",
                "getprop",
                prop,
                timeout=10,
                text=True,
            ).stdout.strip()
            if value:
                data[field] = value

        android_id = self._run_adb(
            device_id,
            "shell",
            "settings",
            "get",
            "secure",
            "android_id",
            timeout=10,
            text=True,
        ).stdout.strip()
        if android_id:
            data["android_id"] = android_id

        kernel = self._run_adb(
            device_id,
            "shell",
            "uname",
            "-a",
            timeout=10,
            text=True,
        ).stdout.strip()
        if kernel:
            data["kernel"] = kernel

        size = self._run_adb(
            device_id,
            "shell",
            "wm",
            "size",
            timeout=10,
            text=True,
        ).stdout.strip()
        if size:
            data["screen_size"] = size

        density = self._run_adb(
            device_id,
            "shell",
            "wm",
            "density",
            timeout=10,
            text=True,
        ).stdout.strip()
        if density:
            data["screen_density"] = density

        if isinstance(data.get("sdk_int"), str):
            data["sdk_int"] = self._coerce_int(data["sdk_int"])

        return {"status": "success", "data": data}

    def _run_adb(
        self,
        device_id: str,
        *args: str,
        timeout: int = 30,
        text: bool = False,
        serial: str | None = None,
    ) -> subprocess.CompletedProcess:
        adb_serial = serial or device_id
        cmd = ["adb", "-s", adb_serial, *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=text,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise ValueError("ADB가 설치되어 있지 않습니다.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"ADB 명령 시간 초과: {' '.join(args)}") from exc

    def _format_adb_error(self, command: str, result: subprocess.CompletedProcess) -> str:
        stdout = result.stdout.decode("utf-8", errors="ignore") if isinstance(result.stdout, bytes) else (result.stdout or "")
        stderr = result.stderr.decode("utf-8", errors="ignore") if isinstance(result.stderr, bytes) else (result.stderr or "")
        detail = stderr.strip() or stdout.strip() or f"returncode={result.returncode}"
        return f"{command} 실행 실패: {detail}"

    def _store_screenshot_bytes(self, image_bytes: bytes, output_path: str) -> str:
        if output_path:
            path = output_path
        else:
            fd, path = tempfile.mkstemp(prefix="aads-device-", suffix=".png")
            os.close(fd)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(image_bytes)
        return path

    def _normalize_screenshot_payload(
        self,
        data: Dict[str, Any],
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized = dict(data)
        image_b64 = str(normalized.pop("base64", "") or "")
        if not image_b64:
            return normalized
        try:
            image_bytes = base64.b64decode(image_b64, validate=True)
        except Exception:
            if args.get("include_base64"):
                normalized["base64"] = image_b64
            return normalized

        normalized["path"] = self._store_screenshot_bytes(
            image_bytes,
            str(args.get("output_path") or "").strip(),
        )
        normalized["bytes"] = int(normalized.get("bytes") or len(image_bytes))
        if args.get("include_base64"):
            normalized["base64"] = image_b64
        return normalized

    def _escape_adb_text(self, text: str) -> str:
        escaped: list[str] = []
        for ch in text:
            if ch == " ":
                escaped.append("%s")
            elif ch in "\\\"'&<>|;()$`":
                escaped.append("\\" + ch)
            else:
                escaped.append(ch)
        return "".join(escaped)

    def _coerce_int(self, value: Any, default: int | None = None) -> int | None:
        try:
            if value in (None, ""):
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _coerce_float(self, value: Any, default: float) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default


# 싱글톤 인스턴스
pc_agent_manager = PCAgentManager()
