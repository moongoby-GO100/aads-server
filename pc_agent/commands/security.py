"""AADS: 보안 잠금 — 명령 승인 제어 + 감사 로그."""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

_SECURITY_DIR = os.path.join(os.path.expanduser("~"), ".aads_security")
_AUDIT_FILE = os.path.join(_SECURITY_DIR, "audit.jsonl")
_LOCKS_FILE = os.path.join(_SECURITY_DIR, "locks.json")

# 기본 잠금 명령 타입
_DEFAULT_LOCKED: Set[str] = {"power_control", "process_kill"}


class SecurityManager:
    """싱글톤 보안 매니저 — 명령 잠금 + 감사 로그."""
    _instance: Optional[SecurityManager] = None

    def __new__(cls) -> SecurityManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._locked_commands: Set[str] = set(_DEFAULT_LOCKED)
        self._audit_log: deque[Dict[str, Any]] = deque(maxlen=100)
        self._load_locks()

    # ── 영구 저장/로드 ──

    def _load_locks(self) -> None:
        """디스크에서 잠금 목록 로드."""
        if not os.path.isfile(_LOCKS_FILE):
            return
        try:
            with open(_LOCKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._locked_commands = set(data.get("locked", []))
            logger.info("보안: %d개 잠금 명령 로드", len(self._locked_commands))
        except Exception as e:
            logger.error("보안 잠금 로드 실패: %s", e)

    def _save_locks(self) -> None:
        """잠금 목록 디스크 저장."""
        try:
            os.makedirs(_SECURITY_DIR, exist_ok=True)
            with open(_LOCKS_FILE, "w", encoding="utf-8") as f:
                json.dump({"locked": sorted(self._locked_commands)}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("보안 잠금 저장 실패: %s", e)

    # ── 핵심 기능 ──

    def check_command(self, command_type: str) -> bool:
        """명령이 잠금 상태인지 확인. True면 차단."""
        return command_type in self._locked_commands

    def log_execution(self, command_type: str, params: Dict[str, Any],
                      status: str, blocked: bool = False) -> None:
        """실행 이력 기록 (메모리 + JSONL 파일)."""
        # 파라미터 요약 (민감 정보 제거, 200자 제한)
        params_summary = str({k: v for k, v in params.items()
                             if k not in ("password", "token", "secret")})
        if len(params_summary) > 200:
            params_summary = params_summary[:200] + "..."

        entry = {
            "timestamp": datetime.now().isoformat(),
            "command_type": command_type,
            "params_summary": params_summary,
            "status": status,
            "blocked": blocked,
        }
        self._audit_log.append(entry)

        # JSONL 영구 저장
        try:
            os.makedirs(_SECURITY_DIR, exist_ok=True)
            with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("감사 로그 기록 실패: %s", e)

    def add_lock(self, command_type: str) -> Dict[str, Any]:
        """명령 잠금 추가."""
        if command_type in self._locked_commands:
            return {"status": "error", "data": {"error": f"이미 잠금 상태: {command_type}"}}
        self._locked_commands.add(command_type)
        self._save_locks()
        return {"status": "success", "data": {
            "locked": command_type,
            "total_locked": len(self._locked_commands),
        }}

    def remove_lock(self, command_type: str) -> Dict[str, Any]:
        """명령 잠금 해제."""
        if command_type not in self._locked_commands:
            return {"status": "error", "data": {"error": f"잠금되지 않은 명령: {command_type}"}}
        self._locked_commands.discard(command_type)
        self._save_locks()
        return {"status": "success", "data": {
            "unlocked": command_type,
            "total_locked": len(self._locked_commands),
        }}

    def get_locked_commands(self) -> list[str]:
        """잠금된 명령 목록 반환."""
        return sorted(self._locked_commands)

    def get_audit_log(self, limit: int = 20) -> list[Dict[str, Any]]:
        """최근 감사 로그 반환."""
        entries = list(self._audit_log)
        return entries[-limit:]


# 싱글톤 인스턴스
_manager = SecurityManager()


# ── 외부 API (다른 모듈에서 사용 가능) ──

def get_security_manager() -> SecurityManager:
    """SecurityManager 싱글톤 인스턴스 반환."""
    return _manager


# ── COMMAND_HANDLERS용 핸들러 함수 ──

async def security_lock(params: Dict[str, Any]) -> Dict[str, Any]:
    """명령 타입 잠금. params: command_type"""
    command_type = params.get("command_type", "")
    if not command_type:
        return {"status": "error", "data": {"error": "command_type 파라미터 필수"}}
    result = _manager.add_lock(command_type)
    _manager.log_execution("security_lock", params, result["status"])
    return result


async def security_unlock(params: Dict[str, Any]) -> Dict[str, Any]:
    """명령 타입 잠금 해제. params: command_type"""
    command_type = params.get("command_type", "")
    if not command_type:
        return {"status": "error", "data": {"error": "command_type 파라미터 필수"}}
    result = _manager.remove_lock(command_type)
    _manager.log_execution("security_unlock", params, result["status"])
    return result


async def security_locked_list(params: Dict[str, Any]) -> Dict[str, Any]:
    """잠금된 명령 목록 조회."""
    locked = _manager.get_locked_commands()
    return {"status": "success", "data": {"locked_commands": locked, "count": len(locked)}}


async def security_audit(params: Dict[str, Any]) -> Dict[str, Any]:
    """감사 로그 조회. params: limit (기본 20)"""
    limit = int(params.get("limit", 20))
    entries = _manager.get_audit_log(limit)
    return {"status": "success", "data": {"entries": entries, "count": len(entries)}}
