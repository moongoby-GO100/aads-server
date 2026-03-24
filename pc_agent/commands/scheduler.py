"""AADS: 작업 스케줄러 — PC에서 특정 시간에 명령 자동 실행."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_JOBS_DIR = os.path.join(os.path.expanduser("~"), ".aads_schedules")
_JOBS_FILE = os.path.join(_JOBS_DIR, "jobs.json")


@dataclass
class ScheduledJob:
    """예약 작업 정의."""
    name: str
    command_type: str
    params: Dict[str, Any]
    schedule_type: str  # "once", "interval", "daily"
    at: str = ""  # HH:MM (daily용)
    interval_minutes: int = 0  # interval용
    delay_minutes: int = 0  # once용
    created_at: str = ""
    next_run: str = ""
    last_run: str = ""
    run_count: int = 0
    enabled: bool = True


class SchedulerManager:
    """싱글톤 작업 스케줄러."""
    _instance: Optional[SchedulerManager] = None

    def __new__(cls) -> SchedulerManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._jobs: Dict[str, ScheduledJob] = {}
        self._task: Optional[asyncio.Task] = None
        self._load_jobs()

    def _load_jobs(self) -> None:
        """디스크에서 작업 목록 로드."""
        if not os.path.isfile(_JOBS_FILE):
            return
        try:
            with open(_JOBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, jdata in data.items():
                self._jobs[name] = ScheduledJob(**jdata)
            logger.info("스케줄러: %d개 작업 로드", len(self._jobs))
        except Exception as e:
            logger.error("스케줄러 작업 로드 실패: %s", e)

    def _save_jobs(self) -> None:
        """작업 목록을 디스크에 저장."""
        try:
            os.makedirs(_JOBS_DIR, exist_ok=True)
            data = {name: asdict(job) for name, job in self._jobs.items()}
            with open(_JOBS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("스케줄러 작업 저장 실패: %s", e)

    def _calc_next_run(self, job: ScheduledJob) -> str:
        """다음 실행 시간 계산."""
        now = datetime.now()
        if job.schedule_type == "once":
            return (now + timedelta(minutes=job.delay_minutes)).isoformat()
        elif job.schedule_type == "interval":
            return (now + timedelta(minutes=job.interval_minutes)).isoformat()
        elif job.schedule_type == "daily":
            if not job.at:
                return (now + timedelta(days=1)).isoformat()
            try:
                hh, mm = job.at.split(":")
                target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                return target.isoformat()
            except (ValueError, IndexError):
                return (now + timedelta(days=1)).isoformat()
        return now.isoformat()

    def add_job(self, name: str, command_type: str, params: Dict[str, Any],
                schedule: Dict[str, Any]) -> Dict[str, Any]:
        """작업 등록."""
        if name in self._jobs:
            return {"status": "error", "data": {"error": f"이미 존재하는 작업: {name}"}}

        now = datetime.now().isoformat()
        job = ScheduledJob(
            name=name,
            command_type=command_type,
            params=params,
            schedule_type=schedule.get("type", "once"),
            at=schedule.get("at", ""),
            interval_minutes=int(schedule.get("interval_minutes", 0)),
            delay_minutes=int(schedule.get("delay_minutes", 0)),
            created_at=now,
        )
        job.next_run = self._calc_next_run(job)
        self._jobs[name] = job
        self._save_jobs()
        self._ensure_loop()
        return {"status": "success", "data": {
            "name": name,
            "command_type": command_type,
            "schedule_type": job.schedule_type,
            "next_run": job.next_run,
        }}

    def remove_job(self, name: str) -> Dict[str, Any]:
        """작업 삭제."""
        if name not in self._jobs:
            return {"status": "error", "data": {"error": f"작업을 찾을 수 없습니다: {name}"}}
        del self._jobs[name]
        self._save_jobs()
        return {"status": "success", "data": {"removed": name}}

    def list_jobs(self) -> Dict[str, Any]:
        """등록된 작업 목록 반환."""
        jobs = []
        for name, job in self._jobs.items():
            jobs.append({
                "name": name,
                "command_type": job.command_type,
                "schedule_type": job.schedule_type,
                "at": job.at,
                "interval_minutes": job.interval_minutes,
                "next_run": job.next_run,
                "last_run": job.last_run,
                "run_count": job.run_count,
                "enabled": job.enabled,
            })
        return {"status": "success", "data": {"jobs": jobs, "count": len(jobs)}}

    def _ensure_loop(self) -> None:
        """체크 루프가 돌고 있는지 확인, 없으면 시작."""
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    self._task = loop.create_task(self._check_loop())
            except RuntimeError:
                pass

    async def _check_loop(self) -> None:
        """1분 간격으로 실행할 작업 확인 후 실행."""
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now()
                to_remove = []

                for name, job in list(self._jobs.items()):
                    if not job.enabled or not job.next_run:
                        continue
                    try:
                        next_dt = datetime.fromisoformat(job.next_run)
                    except (ValueError, TypeError):
                        continue

                    if now >= next_dt:
                        await self._execute_job(job)
                        job.last_run = now.isoformat()
                        job.run_count += 1

                        if job.schedule_type == "once":
                            to_remove.append(name)
                        else:
                            job.next_run = self._calc_next_run(job)

                for name in to_remove:
                    self._jobs.pop(name, None)

                if to_remove or any(True for _ in self._jobs):
                    self._save_jobs()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("스케줄러 체크 루프 에러: %s", e)

    async def _execute_job(self, job: ScheduledJob) -> None:
        """작업 실행 — COMMAND_HANDLERS에서 핸들러 찾아 호출."""
        try:
            from pc_agent.commands import COMMAND_HANDLERS
            handler = COMMAND_HANDLERS.get(job.command_type)
            if not handler:
                logger.error("스케줄러: 알 수 없는 명령 타입: %s", job.command_type)
                return
            result = await handler(job.params)
            logger.info("스케줄러 실행 [%s] %s → %s",
                        job.name, job.command_type, result.get("status", "unknown"))
        except Exception as e:
            logger.error("스케줄러 실행 에러 [%s]: %s", job.name, e)


# 싱글톤 인스턴스
_manager = SchedulerManager()


async def schedule_add(params: Dict[str, Any]) -> Dict[str, Any]:
    """작업 스케줄 등록. params: name, command_type, command_params, schedule"""
    name = params.get("name", "")
    if not name:
        return {"status": "error", "data": {"error": "name 파라미터 필수"}}

    command_type = params.get("command_type", "")
    if not command_type:
        return {"status": "error", "data": {"error": "command_type 파라미터 필수"}}

    command_params = params.get("command_params", {})
    schedule = params.get("schedule", {})
    if not schedule or "type" not in schedule:
        return {"status": "error", "data": {
            "error": "schedule 파라미터 필수 (type: once/interval/daily)",
        }}

    stype = schedule["type"]
    if stype not in ("once", "interval", "daily"):
        return {"status": "error", "data": {
            "error": f"지원하지 않는 schedule type: {stype} (once/interval/daily)",
        }}

    return _manager.add_job(name, command_type, command_params, schedule)


async def schedule_remove(params: Dict[str, Any]) -> Dict[str, Any]:
    """작업 스케줄 삭제. params: name"""
    name = params.get("name", "")
    if not name:
        return {"status": "error", "data": {"error": "name 파라미터 필수"}}
    return _manager.remove_job(name)


async def schedule_list(params: Dict[str, Any]) -> Dict[str, Any]:
    """등록된 작업 스케줄 목록."""
    return _manager.list_jobs()
