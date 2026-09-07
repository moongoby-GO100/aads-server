#!/usr/bin/env python3
"""AADS-CRF v2.0 — 채팅 응답 개요 칩을 CEO 8섹션 플로우로 확장한다.

칩: 지시파악 / 목표 / 계획 / 실행순서 / 결과 / 검증 / 리스크 / 다음
대상: /root/aads/aads-dashboard/src/app/chat/page.tsx
"""
from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path("/root/aads/aads-dashboard/src/app/chat/page.tsx")

REPLACEMENTS: list[tuple[str, str]] = [
    # 1) 타입 확장
    (
        "  hasGoal: boolean;\n  hasPlan: boolean;\n  hasProgress: boolean;",
        "  hasBrief: boolean;\n  hasGoal: boolean;\n  hasPlan: boolean;\n"
        "  hasSteps: boolean;\n  hasProgress: boolean;",
    ),
    # 2) 감지 로직 확장
    (
        '    hasGoal: /(목표|요청|목적|완료\\s*기준|성공\\s*기준)/.test(content),\n'
        '    hasPlan: /(계획|플랜|작업\\s*순서|수행\\s*계획|조치\\s*계획|진행\\s*방식)/.test(content),\n',
        '    hasBrief: /(지시\\s*(?:확인|파악|정리|요약|범위)|요청\\s*(?:확인|파악|정리|요약|범위)|질문\\s*(?:확인|파악))/.test(content),\n'
        '    hasGoal: /(목표|요청|목적|완료\\s*기준|성공\\s*기준)/.test(content),\n'
        '    hasPlan: /(계획|플랜|작업\\s*순서|수행\\s*계획|조치\\s*계획|진행\\s*방식)/.test(content),\n'
        '    hasSteps: /(실행\\s*순서|작업\\s*순서|진행\\s*순서|실행\\s*단계|수행\\s*단계|수행\\s*내역|조치\\s*내역|\\d\\s*단계)/.test(content),\n',
    ),
    # 3) 칩 8개로 확장
    (
        '  const indicators = [\n'
        '    { label: "목표", ok: overview.hasGoal },\n'
        '    { label: "계획", ok: overview.hasPlan },\n'
        '    { label: "진행", ok: overview.hasProgress },\n'
        '    { label: "결과", ok: overview.hasResult },\n'
        '    { label: "검증", ok: overview.hasVerification },\n'
        '    { label: "리스크", ok: overview.hasRisk },\n'
        '    { label: "다음", ok: Boolean(overview.nextAction) },\n'
        '  ];',
        '  const indicators = [\n'
        '    { label: "지시파악", ok: overview.hasBrief },\n'
        '    { label: "목표", ok: overview.hasGoal },\n'
        '    { label: "계획", ok: overview.hasPlan },\n'
        '    { label: "실행순서", ok: overview.hasSteps || overview.hasProgress },\n'
        '    { label: "결과", ok: overview.hasResult },\n'
        '    { label: "검증", ok: overview.hasVerification },\n'
        '    { label: "리스크", ok: overview.hasRisk },\n'
        '    { label: "다음", ok: Boolean(overview.nextAction) },\n'
        '  ];',
    ),
    # 4) 칩 클릭 → 본문 점프 키워드 확장
    (
        '      목표: ["목표", "요청", "목적", "완료기준", "성공기준"],',
        '      지시파악: ["지시파악", "지시확인", "지시정리", "요청확인", "요청파악", "요청정리", "요청범위", "질문파악"],\n'
        '      목표: ["목표", "요청", "목적", "완료기준", "성공기준"],',
    ),
    (
        '      진행: ["진행", "수행내역", "조치내역", "작업내역", "적용범위", "반영상태"],',
        '      실행순서: ["실행순서", "작업순서", "진행순서", "실행단계", "수행단계", "수행내역", "조치내역", "작업내역", "적용범위", "반영상태"],',
    ),
    # 5) 리드 요약에서 지시파악/실행순서 헤딩도 우선 인식
    (
        '/^(✅|⚠️|❌|결론|요약|판정|현황|답변|목표|계획|플랜|진행|수행 내역|결과|검증|리스크|다음)/',
        '/^(✅|⚠️|❌|결론|요약|판정|현황|답변|지시 파악|지시파악|요청 파악|목표|계획|플랜|실행순서|실행 순서|진행|수행 내역|결과|검증|리스크|다음)/',
    ),
]


def main() -> int:
    if not TARGET.exists():
        print(f"[FAIL] target not found: {TARGET}")
        return 2
    src = TARGET.read_text(encoding="utf-8")
    original = src
    applied, skipped = [], []
    for idx, (old, new) in enumerate(REPLACEMENTS, start=1):
        count = src.count(old)
        if count == 1:
            src = src.replace(old, new, 1)
            applied.append(idx)
        elif count == 0 and new.split("\n")[0] in src:
            skipped.append((idx, "already-applied"))
        else:
            skipped.append((idx, f"match_count={count}"))
    if not applied:
        print(f"[SKIP] no replacement applied. skipped={skipped}")
        return 1
    backup = TARGET.with_suffix(TARGET.suffix + ".bak_crf")
    backup.write_text(original, encoding="utf-8")
    TARGET.write_text(src, encoding="utf-8")
    print(f"[OK] applied={applied} skipped={skipped} backup={backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
