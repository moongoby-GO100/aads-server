#!/usr/bin/env python3
"""ARCHITECTURE-INDEX.md에 AADS-CRF v2.0 응답 플로우 아키텍처 항목을 반영한다."""
from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path("/root/aads/aads-server/docs/ARCHITECTURE-INDEX.md")

BLOCK = """
---

## 9. 응답 플로우 아키텍처 (AADS-CRF v2.0, 2026-09-08)

CEO 8섹션 응답 플로우 = `지시파악 → 목표 → 계획 → 실행순서 → 결과 → 검증 → 리스크 → 다음`

| 레이어 | 구현 위치 | 역할 |
|---|---|---|
| L1 프롬프트 | `prompt_assets.slug = global-ceo-8section-response-flow` (layer 1, priority 23) | 생성 시점에 8섹션 순서 지시 (1차 방어선) |
| 백엔드 검증 | `app/services/output_validator.py` — `_CEO_FLOW_GROUPS`, `evaluate_ceo_flow_coverage()` | 900자 이상 보고형에서 커버리지 5/8 미만이면 재작성 (하한선) |
| 프론트 렌더 | `aads-dashboard/src/app/chat/page.tsx` — `ResponseOverviewPanel` 8칩 | 칩 표시 + 본문 섹션 점프 (확인 경로) |

관련 문서

| 문서 | 경로 | 버전 |
|------|------|------|
| 응답 플로우 PRD | `docs/plans/20260908_AADS_CEO_RESPONSE_FLOW_PRD_v2.0.md` | v2.0 |
| 응답 플로우 기술문서 | `docs/AADS-RESPONSE-FLOW-TECHNICAL-SPEC-v2.0.md` | v2.0 |
| 섹션카드 목업 UI | `app/static/reports/20260908_response_section_card_mockup.html` | v2.0 |

운영 주의

- MCP 원격 쓰기 도구(`write_remote_file`/`patch_remote_file`)는 **활성 API 컨테이너 내부**에 기록된다.
  호스트 저장소 반영은 `docker cp <container>:/app/... /root/aads/aads-server/...` 전파가 필요하다.
  전파 없이 커밋하면 변경이 이미지/저장소에 포함되지 않는다.
"""

MARKER = "## 9. 응답 플로우 아키텍처 (AADS-CRF v2.0"


def main() -> int:
    if not TARGET.exists():
        print(f"[FAIL] not found: {TARGET}")
        return 2
    src = TARGET.read_text(encoding="utf-8")
    if MARKER in src:
        print("[SKIP] already applied")
        return 0
    TARGET.write_text(src.rstrip() + "\n" + BLOCK, encoding="utf-8")
    print(f"[OK] appended {len(BLOCK)} chars to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
