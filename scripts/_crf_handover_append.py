#!/usr/bin/env python3
"""HANDOVER.md에 AADS-CRF v2.0 작업 기록을 추가한다 (append-only)."""
from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path("/root/aads/aads-server/docs/HANDOVER.md")
MARKER = "AADS-CRF v2.0 — CEO 8섹션 응답 플로우"

BLOCK = """

---

## [2026-09-08 KST] AADS-CRF v2.0 — CEO 8섹션 응답 플로우 (완료)

CEO 지시: "기획문서, 기술문서, 아키텍처 버전업 + 즉시 구현 적용 + 섹션카드 목업 UI"

**8섹션**: 지시파악 → 목표 → 계획 → 실행순서 → 결과 → 검증 → 리스크 → 다음

| 레이어 | 변경 | 커밋 |
|---|---|---|
| L1 프롬프트 | `prompt_assets` slug `global-ceo-8section-response-flow` (layer 1, priority 23) UPSERT — 141→142행 | DB (배포 불필요) |
| 백엔드 | `app/services/output_validator.py` — `_CEO_FLOW_GROUPS`, `evaluate_ceo_flow_coverage()`, 재시도 프롬프트 8섹션화 | aads-server `9f8edcd9` |
| 프론트 | `src/app/chat/page.tsx` — 응답 개요 7칩 → 8칩 (`지시파악`, `실행순서` 추가) | aads-dashboard `ef8f825` |
| 문서 | PRD v2.0, 기술문서 v2.0, ARCHITECTURE-INDEX §9 | aads-server `9f8edcd9` |
| 목업 | `app/static/reports/20260908_response_section_card_mockup.html` | aads-server `9f8edcd9` / dashboard `3f9e30b` |

**배포**: aads-server hot-reload(85 모듈), aads-dashboard blue/green → `aads-dashboard:ef8f825a60f4` 양 슬롯 healthy.
**검증**: `compiled_prompt_provenance.applied_assets`에 신규 slug 확인(1,208자), 배포 번들에 `지시파악` 문자열 확인.

**중요 운영 함정 (신규 발견)**
MCP 원격 쓰기 도구(`write_remote_file`/`patch_remote_file`)는 **활성 API 컨테이너 내부**(`aads-server-green:/app`)에
기록되며 호스트 저장소에 자동 반영되지 않는다. 반드시 `docker cp <container>:/app/... /root/aads/aads-server/...`로
전파한 뒤 커밋해야 한다. 또한 컨테이너 `/app/docs`는 read-only이므로 `scripts/`에 쓴 뒤 전파해야 한다.
"""


def main() -> int:
    if not TARGET.exists():
        print(f"[FAIL] not found: {TARGET}")
        return 2
    src = TARGET.read_text(encoding="utf-8")
    if MARKER in src:
        print("[SKIP] already recorded")
        return 0
    TARGET.write_text(src.rstrip() + BLOCK, encoding="utf-8")
    print(f"[OK] appended {len(BLOCK)} chars to HANDOVER.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
