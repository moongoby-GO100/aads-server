"""프로젝트별 Git 작업 파일시스템 잠금 (cross-process safe).

경로 A(채팅 직접 수정)와 경로 B(Pipeline Runner)가 동시에
같은 프로젝트의 git add/commit/push를 실행하면 충돌 발생.
flock 기반 advisory lock으로 순차 처리를 보장한다.

사용법:
    async with git_project_lock("AADS"):
        # git add, commit, push 등
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

_GIT_LOCK_DIR = "/tmp"
_GIT_LOCK_TIMEOUT = 60  # 초


@asynccontextmanager
async def git_project_lock(project: str, timeout: float = _GIT_LOCK_TIMEOUT):
    """프로젝트별 git 작업 flock 획득. timeout 초과 시 TimeoutError."""
    lock_path = os.path.join(_GIT_LOCK_DIR, f"aads-git-lock-{project.upper()}")
    fd = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)

        # asyncio에서 blocking flock을 non-blocking + 폴링으로 처리
        elapsed = 0.0
        interval = 0.5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                elapsed += interval
                if elapsed >= timeout:
                    raise TimeoutError(
                        f"Git 잠금 획득 실패({timeout}초 초과): {project} — "
                        f"다른 작업(Pipeline Runner 또는 Chat-Direct)이 git 작업 중입니다"
                    )
                await asyncio.sleep(interval)

        logger.debug(f"git_lock_acquired: {project}")
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            logger.debug(f"git_lock_released: {project}")
