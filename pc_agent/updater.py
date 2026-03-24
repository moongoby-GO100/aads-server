"""KakaoBot SaaS — 자동 업데이트 모듈.

서버 API에서 최신 버전 확인 → zip 다운로드 → 검증 → 교체.
launcher.py에서 호출됨.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib import request, error

logger = logging.getLogger("updater")

# launcher.py와 동일한 경로 상수
INSTALL_DIR = Path(os.environ.get(
    "KAKAOBOT_INSTALL_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"), "KakaoBot"),
))
AGENT_DIR = INSTALL_DIR / "agent"
VERSION_FILE = AGENT_DIR / "VERSION"
HTTP_BASE = "https://aads.newtalk.kr"


def _get_local_version() -> str:
    """로컬 에이전트 버전 읽기. 없으면 '0.0.0'."""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _api_get(path: str, token: str = "") -> bytes:
    """서버 API GET 요청."""
    url = f"{HTTP_BASE}{path}"
    req = request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "KakaoBot-Updater/1.0")
    with request.urlopen(req, timeout=30) as resp:
        return resp.read()


def check_update(cfg: dict) -> tuple[bool, str]:
    """서버 최신 버전 확인.

    Returns:
        (업데이트 필요 여부, 서버 버전 문자열)
    """
    token = cfg.get("agent_token", "")
    data = _api_get("/api/v1/kakao-bot/agent/version", token)
    import json
    info = json.loads(data)
    remote_ver = info.get("version", "0.0.0")
    local_ver = _get_local_version()
    logger.info("버전 비교: 로컬=%s, 서버=%s", local_ver, remote_ver)
    return (local_ver != remote_ver, remote_ver)


def download_update(cfg: dict, version: str) -> None:
    """최신 에이전트 코드 zip 다운로드 → 교체.

    1. zip 다운로드 → 임시 폴더에 해제
    2. VERSION 파일 검증
    3. 기존 agent 폴더 교체
    """
    token = cfg.get("agent_token", "")
    logger.info("에이전트 v%s 다운로드 시작", version)

    zip_data = _api_get("/api/v1/kakao-bot/agent/download", token)

    # 임시 폴더에 해제
    tmp_dir = Path(tempfile.mkdtemp(prefix="kakaobot_update_"))
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            # zip bomb 방지: 전체 크기 100MB 제한
            total = sum(f.file_size for f in zf.infolist())
            if total > 100 * 1024 * 1024:
                raise ValueError(f"zip 크기 초과: {total} bytes")
            zf.extractall(tmp_dir)

        # zip 내부 구조 판별 (루트에 agent.py가 있거나 하위 폴더에 있을 수 있음)
        extracted = tmp_dir
        subdirs = list(tmp_dir.iterdir())
        if len(subdirs) == 1 and subdirs[0].is_dir():
            extracted = subdirs[0]

        # agent.py 존재 검증
        if not (extracted / "agent.py").exists():
            raise FileNotFoundError("다운로드한 zip에 agent.py가 없습니다")

        # 기존 폴더 백업 후 교체
        backup_dir = INSTALL_DIR / "agent_backup"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if AGENT_DIR.exists():
            AGENT_DIR.rename(backup_dir)

        shutil.copytree(extracted, AGENT_DIR)
        logger.info("에이전트 업데이트 완료: v%s", version)

        # 백업 정리
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

    except Exception:
        # 실패 시 백업 복원
        backup_dir = INSTALL_DIR / "agent_backup"
        if backup_dir.exists() and not AGENT_DIR.exists():
            backup_dir.rename(AGENT_DIR)
            logger.info("업데이트 실패 — 백업에서 복원")
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
