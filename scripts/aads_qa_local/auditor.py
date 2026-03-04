#!/usr/bin/env python3
"""
AADS Local Video Auditor — T-028 (ShortFlow 211서버 배포용)
영상에서 프레임을 추출하고 Gemini Vision으로 품질을 분석합니다.

사용법:
  python3 auditor.py --help
  python3 auditor.py video <video_path> --project shortflow --channel economy --video-id eco_20260304
  python3 auditor.py benchmark <video_path> --project shortflow --channel economy

환경변수:
  GOOGLE_API_KEY    Gemini Vision API 키
  AADS_API_URL      AADS 서버 URL (기본: https://aads.newtalk.kr/api/v1)
  AADS_MONITOR_KEY  모니터링 키
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 의존성 체크 ──────────────────────────────────────────────────────────────

try:
    import requests
except ImportError:
    print("ERROR: requests 미설치 — pip3 install requests", file=sys.stderr)
    sys.exit(1)

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# ── 상수 ────────────────────────────────────────────────────────────────────

AADS_API_URL = os.environ.get("AADS_API_URL", "https://aads.newtalk.kr/api/v1")
AADS_MONITOR_KEY = os.environ.get("AADS_MONITOR_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

VIDEO_AUDIT_PROMPT = """
영상 품질 검수관으로서 아래 프레임들을 6가지 기준으로 심사하세요.

## 검수 기준 (각 10점, 총 60점)
1. visual_clarity (시각적 선명도): 해상도, 블러, 노이즈 없음
2. composition (구도): 화면 구성, 레이아웃, 여백 균형
3. color_grading (색상): 자연스러운 색감, 과보정 없음, 화이트밸런스
4. text_quality (텍스트): 자막/제목 가독성, 폰트, 위치 적절
5. brand_consistency (브랜드): 인트로/아웃트로, 로고, 색상 통일
6. content_quality (콘텐츠): 정보 전달력, 영상 흐름, 전반적 완성도

## 반환 형식 (반드시 JSON)
{
  "scores": {
    "visual_clarity": {"score": 8, "issues": [], "fixes": []},
    "composition": {"score": 7, "issues": ["..."], "fixes": ["..."]},
    "color_grading": {"score": 9, "issues": [], "fixes": []},
    "text_quality": {"score": 8, "issues": ["..."], "fixes": ["..."]},
    "brand_consistency": {"score": 7, "issues": ["..."], "fixes": ["..."]},
    "content_quality": {"score": 8, "issues": [], "fixes": []}
  },
  "total_score": 47,
  "verdict": "PASS",
  "summary": "전반적으로 품질 양호, 텍스트 가독성 개선 필요",
  "critical_issues": []
}

판정 기준: AUTO_PUBLISH 51+(85%), CONDITIONAL 42-50(70-84%), AUTO_REJECT 41이하
"""


# ── 유틸 ─────────────────────────────────────────────────────────────────────

def extract_frames(video_path: str, n_frames: int = 5) -> List[str]:
    """ffmpeg로 영상에서 n개 프레임을 추출, 경로 목록 반환."""
    tmpdir = tempfile.mkdtemp(prefix="aads_frames_")
    out_pattern = os.path.join(tmpdir, "frame_%02d.jpg")

    try:
        # 영상 길이 파악
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30
        )
        probe_stdout = probe.stdout.decode("utf-8", errors="replace")
        duration = 0.0
        if probe.returncode == 0:
            info = json.loads(probe.stdout)
            duration = float(info.get("format", {}).get("duration", 0))

        # 균등 간격으로 프레임 추출
        if duration > 0:
            interval = duration / (n_frames + 1)
            frames = []
            for i in range(1, n_frames + 1):
                ts = interval * i
                out_path = os.path.join(tmpdir, f"frame_{i:02d}.jpg")
                ret = subprocess.run(
                    ["ffmpeg", "-ss", str(ts), "-i", video_path,
                     "-vframes", "1", "-q:v", "2", out_path, "-y"],
                    capture_output=True, timeout=30
                )
                if ret.returncode == 0 and os.path.exists(out_path):
                    frames.append(out_path)
            return frames
        else:
            # duration 파악 실패 시 fps 기반 추출
            ret = subprocess.run(
                ["ffmpeg", "-i", video_path, "-vf", f"fps=1/{max(1, 10//n_frames)}",
                 "-vframes", str(n_frames), out_pattern, "-y"],
                capture_output=True, timeout=60
            )
            return sorted([f for f in Path(tmpdir).glob("frame_*.jpg")])

    except Exception as e:
        print(f"WARNING: 프레임 추출 실패 — {e}", file=sys.stderr)
        return []


def encode_image(path: str) -> str:
    """이미지를 base64로 인코딩."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def analyze_with_gemini(frame_paths: List[str]) -> Dict[str, Any]:
    """Gemini Vision으로 프레임 분석."""
    if not GEMINI_AVAILABLE:
        return {"error": "google-generativeai 미설치", "verdict": "ERROR"}

    if not GOOGLE_API_KEY:
        return {"error": "GOOGLE_API_KEY 미설정", "verdict": "ERROR"}

    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash-exp")

    parts: List[Any] = [VIDEO_AUDIT_PROMPT]
    for i, fp in enumerate(frame_paths[:6]):  # 최대 6프레임
        try:
            img_data = encode_image(fp)
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": img_data
                }
            })
        except Exception as e:
            print(f"WARNING: 프레임 {i} 인코딩 실패 — {e}", file=sys.stderr)

    try:
        response = model.generate_content(parts)
        text = response.text.strip()
        # JSON 추출
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        return {"error": str(e), "verdict": "ERROR", "total_score": 0, "summary": str(e)}


def submit_to_aads(result: Dict[str, Any], project: str, channel: str,
                   video_id: str, video_path: str) -> Dict[str, Any]:
    """AADS Context API에 결과 전송."""
    try:
        payload = {
            "project_id": project,
            "channel_name": channel,
            "video_id": video_id,
            "video_path": video_path,
            "local_audit": result,
            "verdict": result.get("verdict", "UNKNOWN"),
            "total_score": result.get("total_score", 0),
            "summary": result.get("summary", ""),
        }
        headers = {"Content-Type": "application/json"}
        if AADS_MONITOR_KEY:
            headers["X-Monitor-Key"] = AADS_MONITOR_KEY

        resp = requests.post(
            f"{AADS_API_URL}/visual-qa/qa-results",
            json=payload, headers=headers, timeout=30
        )
        return resp.json() if resp.status_code < 300 else {"status": resp.status_code, "text": resp.text}
    except Exception as e:
        return {"error": str(e)}


def register_benchmark(video_path: str, project: str, channel: str) -> Dict[str, Any]:
    """벤치마크 영상의 스펙을 AADS API에 등록."""
    try:
        frames = extract_frames(video_path, n_frames=3)
        if not frames:
            return {"error": "프레임 추출 실패"}

        images = []
        for fp in frames:
            images.append(encode_image(fp))

        payload = {
            "project_id": project,
            "channel_name": channel,
            "benchmark_frames": images,
            "video_path": video_path,
        }
        headers = {"Content-Type": "application/json"}
        if AADS_MONITOR_KEY:
            headers["X-Monitor-Key"] = AADS_MONITOR_KEY

        resp = requests.post(
            f"{AADS_API_URL}/visual-qa/extract-spec",
            json=payload, headers=headers, timeout=60
        )
        return resp.json() if resp.status_code < 300 else {"status": resp.status_code, "text": resp.text}
    except Exception as e:
        return {"error": str(e)}


# ── 명령 처리 ────────────────────────────────────────────────────────────────

def cmd_video(args: argparse.Namespace) -> int:
    """영상 품질 검수 → AADS 전송."""
    if not os.path.exists(args.video_path):
        print(f"ERROR: 파일 없음 — {args.video_path}", file=sys.stderr)
        return 1

    print(f"[AADS Auditor] 프레임 추출 중: {args.video_path}")
    frames = extract_frames(args.video_path, n_frames=5)

    if not frames:
        # ffmpeg 없거나 영상 손상 — API 직접 호출로 fallback
        print("WARNING: 프레임 추출 실패, AADS API 직접 호출로 fallback", file=sys.stderr)
        try:
            resp = requests.post(
                f"{AADS_API_URL}/visual-qa/quality-gate",
                json={
                    "project_id": args.project,
                    "video_path": args.video_path,
                    "video_id": args.video_id,
                    "channel_name": args.channel,
                    "auto_correct": True,
                },
                timeout=120
            )
            data = resp.json()
        except Exception as e:
            print(f"ERROR: API 호출 실패 — {e}", file=sys.stderr)
            return 1
    else:
        print(f"[AADS Auditor] {len(frames)}개 프레임 추출 완료, Gemini 분석 중...")
        data = analyze_with_gemini(frames)

        if data.get("verdict") == "ERROR":
            print(f"WARNING: Gemini 분석 실패 — {data.get('error')}", file=sys.stderr)
            print("[AADS Auditor] AADS API fallback 시도 중...")
            try:
                resp = requests.post(
                    f"{AADS_API_URL}/visual-qa/quality-gate",
                    json={
                        "project_id": args.project,
                        "video_path": args.video_path,
                        "video_id": args.video_id,
                        "channel_name": args.channel,
                        "auto_correct": True,
                    },
                    timeout=120
                )
                data = resp.json()
            except Exception as e:
                print(f"ERROR: API 호출도 실패 — {e}", file=sys.stderr)
                return 1

        # 결과 AADS에 전송
        submit_result = submit_to_aads(
            data, args.project, args.channel, args.video_id, args.video_path
        )
        data["aads_submit"] = submit_result

    # 결과 출력
    verdict = data.get("verdict", "UNKNOWN")
    total = data.get("total_score", data.get("match_percent", 0))
    summary = data.get("summary", "")
    action = data.get("action", "")

    print("=== AADS Local Auditor 결과 ===")
    print(f"Project : {args.project} / {args.channel} / {args.video_id}")
    print(f"Verdict : {verdict} ({total})")
    print(f"Summary : {summary}")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))

    # action 기반 종료 코드 (AADS API fallback)
    if action in ("publish",):
        return 0
    elif action in ("ceo_review",):
        return 2
    elif action in ("re-render", "reject"):
        return 3

    # local verdict 기반 종료 코드
    if verdict in ("AUTO_PUBLISH",) or (isinstance(total, (int, float)) and total >= 51):
        return 0
    elif verdict in ("CONDITIONAL",) or (isinstance(total, (int, float)) and 42 <= total < 51):
        return 2
    elif verdict in ("AUTO_REJECT", "FAIL"):
        return 3
    else:
        return 1


def cmd_benchmark(args: argparse.Namespace) -> int:
    """벤치마크 스펙 등록."""
    if not os.path.exists(args.video_path):
        print(f"ERROR: 파일 없음 — {args.video_path}", file=sys.stderr)
        return 1

    print(f"[AADS Auditor] 벤치마크 등록 중: {args.video_path}")
    result = register_benchmark(args.video_path, args.project, args.channel)

    if "error" in result:
        print(f"ERROR: 벤치마크 등록 실패 — {result['error']}", file=sys.stderr)
        return 1

    print("=== 벤치마크 등록 완료 ===")
    print(f"Project : {args.project} / {args.channel}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Status  : {result.get('status', 'ok')}")
        print(f"Message : {result.get('message', '')}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="auditor.py",
        description="AADS Local Video Auditor — T-028 (ShortFlow 211서버용)",
    )
    parser.add_argument("--json", action="store_true", help="JSON 형식으로 출력")

    sub = parser.add_subparsers(dest="cmd")

    # video 서브커맨드
    p_video = sub.add_parser("video", help="영상 품질 검수")
    p_video.add_argument("video_path", help="영상 파일 경로")
    p_video.add_argument("--project", default="shortflow", help="프로젝트 ID")
    p_video.add_argument("--channel", required=True, help="채널명 (economy, health, ...)")
    p_video.add_argument("--video-id", required=True, help="영상 고유 ID")

    # benchmark 서브커맨드
    p_bm = sub.add_parser("benchmark", help="벤치마크 스펙 등록")
    p_bm.add_argument("video_path", help="벤치마크 영상 파일 경로")
    p_bm.add_argument("--project", default="shortflow", help="프로젝트 ID")
    p_bm.add_argument("--channel", required=True, help="채널명")

    args = parser.parse_args()

    if args.cmd is None:
        parser.print_help()
        return 0
    elif args.cmd == "video":
        return cmd_video(args)
    elif args.cmd == "benchmark":
        return cmd_benchmark(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
