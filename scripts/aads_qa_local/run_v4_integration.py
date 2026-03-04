#!/usr/bin/env python3
"""
AADS Quality Gate Integration for run_v4_pipeline.py
T-029: 211서버 ShortFlow 검수 클라이언트 배포 + run_v4_pipeline.py 통합

사용법 (run_v4_pipeline.py에서 import):
    from aads_qa_local.run_v4_integration import AadsQualityGate, quality_gate_check

환경변수:
    AADS_API_URL     AADS 서버 URL (기본: https://aads.newtalk.kr/api/v1)
    AADS_MONITOR_KEY 모니터링 키
    GOOGLE_API_KEY   Gemini Vision API 키 (로컬 감사용)

반환 코드:
    "publish"   → AUTO_PUBLISH (85%+) — 즉시 업로드
    "hold"      → CONDITIONAL (70-84%) — CEO 리뷰 큐 저장
    "reject"    → AUTO_REJECT (<70%) — 재렌더링
    "error"     → 오류 — 로그 확인
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# ── 로거 ────────────────────────────────────────────────────────────────────
logger = logging.getLogger("aads_qa")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[AADS-QA] %(asctime)s %(levelname)s %(message)s",
                                       datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# ── 설정 ────────────────────────────────────────────────────────────────────
AADS_API_URL = os.environ.get("AADS_API_URL", "https://aads.newtalk.kr/api/v1")
AADS_MONITOR_KEY = os.environ.get("AADS_MONITOR_KEY", "")
_SCRIPT_DIR = Path(__file__).parent
_QUALITY_GATE_SH = _SCRIPT_DIR / "quality_gate.sh"
_AADS_QA_CLIENT = Path("/root/aads_qa_client.sh")

# 반환 코드 상수
ACTION_PUBLISH = "publish"
ACTION_HOLD = "hold"
ACTION_REJECT = "reject"
ACTION_ERROR = "error"

# 종료 코드 매핑 (quality_gate.sh)
_EXIT_CODE_MAP = {
    0: ACTION_PUBLISH,
    2: ACTION_HOLD,
    3: ACTION_REJECT,
}


class AadsQualityGate:
    """AADS 품질 게이트 클라이언트.

    run_v4_pipeline.py의 업로드 직전 단계에서 사용:

        gate = AadsQualityGate(project="shortflow", channel="economy")
        action = gate.check(video_path, video_id)
        if action == "publish":
            upload_to_youtube(video_path)
        elif action == "hold":
            save_for_ceo_review(video_path, video_id)
        else:
            log_rejection(video_path, video_id)
    """

    def __init__(
        self,
        project: str = "shortflow",
        channel: str = "economy",
        timeout: int = 120,
        auto_correct: bool = True,
    ):
        self.project = project
        self.channel = channel
        self.timeout = timeout
        self.auto_correct = auto_correct

    # ── 공개 API ───────────────────────────────────────────────────────────

    def check(self, video_path: str, video_id: Optional[str] = None) -> str:
        """영상 품질 게이트를 수행하고 action 문자열을 반환.

        Returns:
            "publish"  — AUTO_PUBLISH: 즉시 업로드 가능
            "hold"     — CONDITIONAL: CEO 리뷰 필요
            "reject"   — AUTO_REJECT: 재렌더링 필요
            "error"    — 오류 발생
        """
        if not os.path.exists(video_path):
            logger.error("영상 파일 없음: %s", video_path)
            return ACTION_ERROR

        vid = video_id or Path(video_path).stem
        logger.info("품질 게이트 시작: project=%s channel=%s video_id=%s",
                    self.project, self.channel, vid)

        # 1순위: quality_gate.sh (로컬 Gemini Vision)
        action = self._run_via_shell(video_path, vid)
        if action is not None:
            return action

        # 2순위: aads_qa_client.sh (AADS API 직접 호출)
        action = self._run_via_client(video_path, vid)
        if action is not None:
            return action

        # 3순위: Python requests 직접 호출
        return self._run_via_requests(video_path, vid)

    def register_benchmark(self, video_path: str) -> bool:
        """벤치마크 영상 등록 (채널별 최초 1회).

        Returns:
            True  — 등록 성공
            False — 실패
        """
        if not os.path.exists(video_path):
            logger.error("벤치마크 파일 없음: %s", video_path)
            return False

        logger.info("벤치마크 등록: %s/%s ← %s",
                    self.project, self.channel, video_path)

        if _QUALITY_GATE_SH.exists():
            ret = subprocess.run(
                [str(_QUALITY_GATE_SH), "benchmark", video_path,
                 self.project, self.channel],
                capture_output=True, text=True, timeout=120,
            )
            if ret.returncode == 0:
                logger.info("벤치마크 등록 완료 (quality_gate.sh)")
                return True
            logger.warning("quality_gate.sh benchmark 실패: %s", ret.stderr.strip())

        # fallback: AADS extract-spec API
        try:
            import requests  # noqa: PLC0415
            resp = requests.post(
                f"{AADS_API_URL}/visual-qa/extract-spec",
                json={
                    "project_id": self.project,
                    "channel_name": self.channel,
                    "video_path": video_path,
                    "benchmark_frames": [],
                },
                headers=self._headers(),
                timeout=60,
            )
            ok = resp.status_code < 300
            if ok:
                logger.info("벤치마크 등록 완료 (API)")
            else:
                logger.warning("벤치마크 API 실패 %d: %s", resp.status_code, resp.text[:200])
            return ok
        except Exception as exc:
            logger.error("벤치마크 API 예외: %s", exc)
            return False

    # ── 내부 메서드 ────────────────────────────────────────────────────────

    def _run_via_shell(self, video_path: str, video_id: str) -> Optional[str]:
        """quality_gate.sh 를 통한 품질 게이트."""
        if not _QUALITY_GATE_SH.exists():
            return None
        try:
            ret = subprocess.run(
                [str(_QUALITY_GATE_SH), "video", video_path,
                 self.project, self.channel, video_id],
                capture_output=True, text=True, timeout=self.timeout,
            )
            action = _EXIT_CODE_MAP.get(ret.returncode)
            if action:
                logger.info("[quality_gate.sh] %s (exit=%d)", action.upper(), ret.returncode)
                if ret.stdout.strip():
                    logger.debug(ret.stdout.strip())
                return action
            if ret.returncode not in (126, 127):
                logger.warning("quality_gate.sh 비정상 종료 %d: %s",
                               ret.returncode, ret.stderr.strip())
        except subprocess.TimeoutExpired:
            logger.error("quality_gate.sh 타임아웃 (%ds)", self.timeout)
        except Exception as exc:
            logger.warning("quality_gate.sh 실행 불가: %s", exc)
        return None

    def _run_via_client(self, video_path: str, video_id: str) -> Optional[str]:
        """aads_qa_client.sh 를 통한 품질 게이트."""
        if not _AADS_QA_CLIENT.exists():
            return None
        try:
            ret = subprocess.run(
                [str(_AADS_QA_CLIENT), "quality-gate",
                 video_path, self.project, self.channel, video_id],
                capture_output=True, text=True, timeout=self.timeout,
            )
            action = _EXIT_CODE_MAP.get(ret.returncode)
            if action:
                logger.info("[aads_qa_client.sh] %s (exit=%d)", action.upper(), ret.returncode)
                return action
        except Exception as exc:
            logger.warning("aads_qa_client.sh 실행 불가: %s", exc)
        return None

    def _run_via_requests(self, video_path: str, video_id: str) -> str:
        """Python requests 로 AADS API 직접 호출 (최후 수단)."""
        try:
            import requests  # noqa: PLC0415
            resp = requests.post(
                f"{AADS_API_URL}/visual-qa/quality-gate",
                json={
                    "project_id": self.project,
                    "video_path": video_path,
                    "video_id": video_id,
                    "channel_name": self.channel,
                    "auto_correct": self.auto_correct,
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            data = resp.json()
            action_raw = data.get("action", "error")
            action = {
                "publish": ACTION_PUBLISH,
                "ceo_review": ACTION_HOLD,
                "re-render": ACTION_REJECT,
                "reject": ACTION_REJECT,
            }.get(action_raw, ACTION_ERROR)
            logger.info("[requests] API action=%s verdict=%s match=%s%%",
                        action_raw,
                        data.get("verdict", "?"),
                        data.get("match_percent", "?"))
            return action
        except Exception as exc:
            logger.error("AADS API 직접 호출 실패: %s", exc)
            return ACTION_ERROR

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "User-Agent": "curl/7.64.0"}
        if AADS_MONITOR_KEY:
            h["X-Monitor-Key"] = AADS_MONITOR_KEY
        return h


# ── 편의 함수 ────────────────────────────────────────────────────────────────

def quality_gate_check(
    video_path: str,
    channel: str,
    video_id: Optional[str] = None,
    project: str = "shortflow",
) -> str:
    """단순 품질 게이트 함수 (AadsQualityGate 래퍼).

    run_v4_pipeline.py 에서 최소 수정으로 사용:

        from aads_qa_local.run_v4_integration import quality_gate_check
        action = quality_gate_check(output_path, channel_name)
        if action != "publish":
            return  # 업로드 건너뜀

    Args:
        video_path: 검수할 영상 파일 경로
        channel:    채널명 (economy, health, tech, ...)
        video_id:   고유 ID (None이면 파일명 사용)
        project:    프로젝트 ID (기본: shortflow)

    Returns:
        "publish" | "hold" | "reject" | "error"
    """
    gate = AadsQualityGate(project=project, channel=channel)
    return gate.check(video_path, video_id)


# ── run_v4_pipeline.py 적용 예시 ─────────────────────────────────────────────

RUN_V4_PATCH_EXAMPLE = '''
# ============================================================
# run_v4_pipeline.py 패치 예시
# 파일 상단에 import 추가:
# ============================================================

import os
import sys

# AADS QA 경로 추가 (배포 위치에 따라 조정)
sys.path.insert(0, "/root/aads_qa")
from run_v4_integration import quality_gate_check

# ============================================================
# 기존 upload_video() 또는 post_process() 함수 내에서:
# ============================================================

def post_process_and_upload(output_path: str, channel: str, video_date: str):
    """영상 후처리 + AADS 품질 게이트 + 업로드."""

    video_id = f"{channel}_{video_date}"

    # ── AADS 품질 게이트 ──────────────────────────────────────
    action = quality_gate_check(output_path, channel, video_id)

    if action == "publish":
        print(f"✅ AUTO_PUBLISH: {video_id} — 업로드 진행")
        upload_to_youtube(output_path, channel)

    elif action == "hold":
        print(f"⚠️  CONDITIONAL: {video_id} — CEO 리뷰 대기")
        save_for_review(output_path, video_id)
        # Telegram/슬랙 알림 (선택)
        notify_ceo(f"[ShortFlow] {video_id} CEO 리뷰 필요")

    else:  # reject / error
        print(f"❌ AUTO_REJECT: {video_id} — 재렌더링 필요")
        log_rejection(output_path, video_id)
        # 재렌더링 트리거 (선택)
        # trigger_rerender(video_id)
'''

if __name__ == "__main__":
    print("run_v4_integration.py — AADS Quality Gate Integration Module")
    print("=" * 60)
    print(RUN_V4_PATCH_EXAMPLE)
    print("\n사용법: python3 run_v4_integration.py --help 는 지원하지 않음.")
    print("아래처럼 import 하여 사용하세요:")
    print("  from run_v4_integration import quality_gate_check, AadsQualityGate")
