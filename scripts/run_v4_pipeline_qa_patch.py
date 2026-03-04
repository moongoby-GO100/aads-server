"""
run_v4_pipeline_qa_patch.py — AADS 품질 게이트 연동 모듈 (T-029)

ShortFlow run_v4_pipeline.py 에 삽입하여 업로드 직전 품질 검수를 수행한다.

사용법:
    from run_v4_pipeline_qa_patch import quality_gate_before_upload

    # run_v4_pipeline.py 내 업로드 직전에 호출:
    verdict = quality_gate_before_upload(
        video_path="/data/shortflow/outputs/economy/video_20260304.mp4",
        channel="economy",
        video_id="economy_20260304"
    )
    if verdict == "publish":
        upload_to_youtube(video_path, ...)
    elif verdict == "hold":
        save_for_ceo_review(video_path, ...)
    else:  # "reject"
        log_rejection(video_path, ...)
        # 재렌더링 지시서를 참조하여 파라미터 보정 후 재합성
        trigger_re_render(video_path, channel)

환경변수:
    AADS_QA_URL  — AADS API base URL (기본: https://aads.newtalk.kr/api/v1/visual-qa)
    AADS_QA_TIMEOUT — 품질 게이트 요청 타임아웃 초 (기본: 120)
"""

import os
import json
import subprocess
import logging
from typing import Literal, Optional

logger = logging.getLogger("shortflow.qa")

# ── 설정 ────────────────────────────────────────────────────────────────────
AADS_QA_URL = os.environ.get("AADS_QA_URL", "https://aads.newtalk.kr/api/v1/visual-qa")
AADS_QA_TIMEOUT = int(os.environ.get("AADS_QA_TIMEOUT", "120"))
QA_CLIENT = "/root/aads_qa_client.sh"

QA_VERDICT = Literal["publish", "hold", "reject", "error"]


# ── 핵심 함수 ────────────────────────────────────────────────────────────────

def quality_gate_before_upload(
    video_path: str,
    channel: str,
    video_id: str,
    auto_correct: bool = True,
) -> QA_VERDICT:
    """
    업로드 직전 품질 게이트 호출.

    Returns:
        "publish"  — AUTO_PUBLISH (85%+): 즉시 업로드
        "hold"     — CONDITIONAL (70-84%): CEO 리뷰 대기
        "reject"   — AUTO_REJECT (<70%): 재렌더링 필요
        "error"    — AADS 서버 오류: 기본 업로드 진행 (fail-open)
    """
    if not os.path.exists(video_path):
        logger.error(f"[QA] 영상 파일 없음: {video_path}")
        return "error"

    if os.path.exists(QA_CLIENT):
        return _gate_via_shell_client(video_path, channel, video_id)
    else:
        return _gate_via_http(video_path, channel, video_id, auto_correct)


def _gate_via_shell_client(video_path: str, channel: str, video_id: str) -> QA_VERDICT:
    """aads_qa_client.sh 를 통한 품질 게이트"""
    try:
        result = subprocess.run(
            [QA_CLIENT, "quality-gate", video_path, "shortflow", channel, video_id],
            capture_output=True,
            text=True,
            timeout=AADS_QA_TIMEOUT,
        )
        logger.info(f"[QA] stdout: {result.stdout.strip()}")
        if result.stderr:
            logger.warning(f"[QA] stderr: {result.stderr.strip()}")

        rc = result.returncode
        if rc == 0:
            logger.info(f"[QA] AUTO_PUBLISH: {video_id}")
            return "publish"
        elif rc == 2:
            logger.warning(f"[QA] CONDITIONAL: {video_id} — CEO 리뷰 대기")
            return "hold"
        elif rc == 3:
            logger.warning(f"[QA] AUTO_REJECT: {video_id} — 재렌더링 필요")
            return "reject"
        else:
            logger.error(f"[QA] ERROR (rc={rc}): {result.stderr}")
            return "error"

    except subprocess.TimeoutExpired:
        logger.error(f"[QA] 타임아웃 ({AADS_QA_TIMEOUT}s): {video_id}")
        return "error"
    except Exception as e:
        logger.error(f"[QA] 예외 발생: {e}")
        return "error"


def _gate_via_http(
    video_path: str,
    channel: str,
    video_id: str,
    auto_correct: bool,
) -> QA_VERDICT:
    """HTTP 직접 호출 (aads_qa_client.sh 없을 때 fallback)"""
    try:
        import requests  # noqa: PLC0415

        resp = requests.post(
            f"{AADS_QA_URL}/quality-gate",
            json={
                "project_id": "shortflow",
                "video_path": video_path,
                "video_id": video_id,
                "channel_name": channel,
                "auto_correct": auto_correct,
            },
            timeout=AADS_QA_TIMEOUT,
            headers={"User-Agent": "shortflow-qa/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        action = data.get("action", "error")
        match_pct = data.get("match_percent", 0)
        logger.info(
            f"[QA] {video_id} → action={action}, match={match_pct}%, "
            f"verdict={data.get('verdict','')}"
        )
        if action == "publish":
            return "publish"
        elif action in ("ceo_review",):
            return "hold"
        elif action in ("re-render", "reject"):
            return "reject"
        else:
            return "error"

    except Exception as e:
        logger.error(f"[QA] HTTP 오류: {e}")
        return "error"


# ── run_v4_pipeline.py 삽입 예시 ─────────────────────────────────────────────
# 아래 코드를 run_v4_pipeline.py 의 업로드 직전 위치에 삽입하세요.
#
# --- 삽입 시작 ---
# from run_v4_pipeline_qa_patch import quality_gate_before_upload
#
# # 영상 합성 완료 후, upload_to_youtube() 호출 전:
# verdict = quality_gate_before_upload(
#     video_path=output_path,
#     channel=channel_name,
#     video_id=f"{channel_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
# )
#
# if verdict == "publish":
#     logger.info(f"품질 게이트 통과 — 업로드 진행: {output_path}")
#     upload_to_youtube(output_path, ...)
# elif verdict == "hold":
#     logger.warning(f"품질 게이트 CONDITIONAL — CEO 리뷰 대기: {output_path}")
#     # 파일을 /data/shortflow/review_queue/ 로 이동
#     import shutil, os
#     os.makedirs("/data/shortflow/review_queue", exist_ok=True)
#     shutil.move(output_path, f"/data/shortflow/review_queue/{os.path.basename(output_path)}")
# elif verdict == "reject":
#     logger.error(f"품질 게이트 AUTO_REJECT — 재렌더링 지시: {output_path}")
#     # 다음 크론 사이클에서 파라미터 보정 후 재합성
# else:  # error
#     logger.warning(f"품질 게이트 오류 — fail-open (업로드 진행): {output_path}")
#     upload_to_youtube(output_path, ...)
# --- 삽입 끝 ---


if __name__ == "__main__":
    """독립 실행 테스트"""
    import sys

    if len(sys.argv) < 4:
        print("Usage: python3 run_v4_pipeline_qa_patch.py <video_path> <channel> <video_id>")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    vpath, ch, vid = sys.argv[1], sys.argv[2], sys.argv[3]
    result = quality_gate_before_upload(vpath, ch, vid)
    print(f"Verdict: {result}")
    sys.exit(0 if result == "publish" else 1 if result == "error" else 2)
