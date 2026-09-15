#!/usr/bin/env python3
"""NTV2 배포 스크립트(/srv/newtalk-v2/deploy.sh)에 중앙 원장 기록을 심는다.

왜 파일을 직접 고치는가
  NTV2 는 블루/그린이 아니라 컨테이너 교체 방식이고, 배포 사실을 남기는 파일이
  하나도 없었다. 그래서 채팅 아티팩트 '배포' 탭에 NTV2 가 영원히 나오지 않았다.
  실행 경로가 이 파일 하나뿐이므로 여기에 기록 호출을 붙이는 것이 가장 짧다.

원칙
  - 멱등하다. 이미 심어져 있으면 아무 것도 하지 않는다.
  - 기록 실패가 배포를 죽이면 안 된다(`|| true`).
  - 성공은 헬스체크를 통과한 뒤에만 남긴다. 확인하지 않은 것을 사실로 적지 않는다.

사용: python3 instrument_ntv2_deploy.py [--check]
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime

TARGET = "/srv/newtalk-v2/deploy.sh"
MARKER = "# AADS-LEDGER"

HELPER = f"""
{MARKER}: 배포 사실을 중앙 원장(deploy_runs)이 읽는 큐에 남긴다. 2026-09-15.
NTV2_RELEASE_SHA="$(git -C /srv/newtalk-v2 rev-parse --short HEAD 2>/dev/null || echo unknown)"
record_release() {{
    command -v aads-record-release >/dev/null 2>&1 || return 0
    aads-record-release NTV2 "$1" "$NTV2_RELEASE_SHA" "NTV2-DEPLOY-${{TARGET}}" "$2" || true
}}
"""

# (앵커, 앵커 뒤에 넣을 줄) — 앵커는 파일에서 정확히 1회만 나타나야 한다.
INSERTS = [
    (
        '            log "FAIL: 빌드 실패. 기존 서비스 영향 없음."',
        '            record_release failed "frontend 이미지 빌드 실패 — 기존 서비스 유지"',
    ),
    (
        '    log "FAIL: 헬스체크 실패"',
        '    record_release failed "헬스체크 60초 초과 (http://localhost:3000)"',
    ),
    (
        'log "NTV2 무중단 배포 성공 ($TARGET)"',
        'record_release deployed "헬스체크 통과 후 기록 ($TARGET)"',
    ),
]

ANCHOR_HELPER = "cd /srv/newtalk-v2"


def main() -> int:
    check_only = "--check" in sys.argv
    with open(TARGET, "r", encoding="utf-8") as fh:
        text = fh.read()

    if MARKER in text:
        print("[skip] 이미 심어져 있다 — 변경 없음")
        return 0

    lines = text.splitlines()

    if sum(1 for ln in lines if ln.strip() == ANCHOR_HELPER) != 1:
        print(f"[fatal] 헬퍼 앵커가 1회가 아니다: {ANCHOR_HELPER!r}", file=sys.stderr)
        return 1
    for anchor, _ in INSERTS:
        if lines.count(anchor) != 1:
            print(f"[fatal] 앵커가 1회가 아니다: {anchor!r}", file=sys.stderr)
            return 1

    out: list[str] = []
    for line in lines:
        out.append(line)
        if line.strip() == ANCHOR_HELPER:
            out.extend(HELPER.strip("\n").splitlines())
            continue
        for anchor, addition in INSERTS:
            if line == anchor:
                out.append(addition)
                break

    new_text = "\n".join(out) + "\n"
    if check_only:
        print("[check] 삽입 지점 4곳 확인 — 쓰지 않음")
        return 0

    backup = f"{TARGET}.bak_aads_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(TARGET, backup)
    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    print(f"[ok] 기록 호출 4곳 삽입 (백업: {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
