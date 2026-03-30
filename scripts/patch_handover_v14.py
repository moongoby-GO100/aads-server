#!/usr/bin/env python3
"""HANDOVER.md v12.22 → v14.0 패치 스크립트"""

import re

path = "/root/aads/aads-docs/HANDOVER.md"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

original = content  # for diff check at end

# ─────────────────────────────────────────────
# CHANGE 1: Update header
# ─────────────────────────────────────────────
content = content.replace(
    "# AADS HANDOVER v12.22\n최종 업데이트: 2026-03-09 | 버전: v12.22 — AADS-188E: approve-diff API 배포 + E2E 테스트 추가, 188D Monaco DiffEditor 배포 완료",
    "# AADS HANDOVER v14.0\n최종 업데이트: 2026-03-30 | 버전: v14.0 — 3월 9~30일 갭 12건 갱신: Pipeline Runner 체계, OAuth 전환, Blue-Green 배포, PC Agent, 메모리 진화, LiteLLM 확장"
)

# ─────────────────────────────────────────────
# CHANGE 2: Update AADS project status block
# ─────────────────────────────────────────────
old_status = "- **최근: AADS-188E 완료 (2026-03-09) — 전체 통합 검증 + E2E 테스트 66개 PASS**"
new_status = """- **최근: AADS-195+ (2026-03-22~30) — Pipeline Runner 실전 운용, Blue-Green 배포, PC Agent v1.0.13, Prompt Caching BP 구조**
- AADS-190 완료 (2026-03-10): 원격 쓰기/실행 9개 도구 + 서브에이전트 (spawn_subagent, spawn_parallel_subagents)
- AADS-191 완료: OAuth 전환 (ANTHROPIC_AUTH_TOKEN), R-AUTH 체계 확립, anthropic_client.py 중앙화
- Pipeline Runner 전면 도입: pipeline_c_start 도구 폐기 → pipeline_runner_submit으로 대체. Pipeline C 소스는 보존(레거시)
- Blue-Green 무중단 배포: deploy.sh, unified_healer.py bluegreen 복구, aads-server-green 컨테이너 대기
- PC Agent v1.0.13: 뮤텍스/단일인스턴스, EXE 빌드 GitHub Actions, 에러 리질리언스
- Prompt Caching 3-breakpoint: BP1(시스템)/BP2(도구)/BP3(히스토리), model_selector.py 적용
- 메모리 진화: Self-Evaluator LLM-free 재구현, Meta-Evaluator 트랜잭션 통합, visual memory
- LiteLLM 확장: OpenRouter 5종(grok-4-fast, deepseek-v3 등), Groq 7종, Gemini 3.0/3.1
- Watchdog 강화: 연쇄 재시작 방지, watchdog-host.sh systemd 등록
- SearXNG 검색 우선순위 삽입 (Gemini Grounding 앞)
- 대시보드: Tool 접기/펼치기, recovered 메시지 tool UI, SSE 끊김 복구
- 이전: AADS-188E 완료 (2026-03-09) — 전체 통합 검증 + E2E 테스트 66개 PASS"""

content = content.replace(old_status, new_status)

# ─────────────────────────────────────────────
# CHANGE 3: Update LiteLLM info
# ─────────────────────────────────────────────
content = content.replace(
    "- LiteLLM: http://litellm:4000 (Docker 내부), Gemini 3종 + Claude 3종, 일 $5 / 월 $150 상한",
    "- LiteLLM: http://litellm:4000 (Docker 내부), Gemini 3종 + Claude 3종 + OpenRouter 5종 + Groq 7종, 일 $5 / 월 $150 상한"
)

# ─────────────────────────────────────────────
# CHANGE 4: Add Pipeline Runner section after bridge.py section,
#           before ## 서버 현황
# ─────────────────────────────────────────────
pipeline_runner_section = """

---

## Pipeline Runner 시스템 (AADS-190+, 2026-03-10~)

### Pipeline C → Pipeline Runner 전환
- **Pipeline C** (pipeline_c.py, 104KB): 레거시 보존. 컨테이너 내 asyncio 실행. 도구명(pipeline_c_start 등) 폐기, 시스템 프롬프트에 "사용 금지" 명시.
- **Pipeline Runner** (pipeline-runner.sh): 활성 실행 시스템. 호스트 systemd 독립 프로세스.

### Pipeline Runner 실행 흐름
```
CEO 채팅 → intent_router(pipeline_runner) → chat_service(AutonomousExecutor)
    → tool_executor → POST /api/v1/pipeline/jobs (submit)
    → pipeline-runner.sh (호스트 systemd, 5초 폴링)
    → Claude Code CLI 실행 (6단계 모델+계정 폴백)
    → AI Reviewer 자동 검수 → awaiting_approval
    → CEO approve → git push → 프로젝트별 배포 → done
```

### Pipeline C vs Runner 비교

| 항목 | Pipeline C (보존) | Pipeline Runner (활성) |
|------|------------------|----------------------|
| 실행 위치 | 컨테이너 내 asyncio | 호스트 systemd 독립 프로세스 |
| 서버 재시작 영향 | 작업 소멸 | 무영향 |
| 계정 폴백 | 없음 | 6단계 (Sonnet/Opus/Haiku × 2계정) |
| AI 검수 | 없음 | AI Reviewer 자동 실행 |
| CEO 승인 | 없음 | approve/reject 필수 |
| 프로젝트 | AADS만 | AADS/KIS/GO100/SF/NTV2 전체 |

### 관련 파일
- `scripts/pipeline-runner.sh`: 메인 실행 스크립트 (systemd)
- `app/api/pipeline_runner_api.py`: REST API (submit/status/approve/reject)
- `app/services/pipeline_runner_service.py`: 비즈니스 로직
- `app/api/ceo_chat_tools.py`: pipeline_runner_submit/status/approve 도구

"""

# Insert before "## 서버 현황"
content = content.replace(
    "\n---\n\n## 서버 현황",
    pipeline_runner_section + "---\n\n## 서버 현황"
)

# ─────────────────────────────────────────────
# CHANGE 5: Update Docker containers
# ─────────────────────────────────────────────
content = content.replace(
    "- 주요 서비스: FastAPI, PostgreSQL, Dashboard (Docker Compose)",
    "- 주요 서비스: FastAPI, PostgreSQL, Dashboard, LiteLLM, Redis, SearXNG (Docker Compose 7컨테이너)"
)

# ─────────────────────────────────────────────
# CHANGE 6: Update GitHub PAT remaining days
# ─────────────────────────────────────────────
content = content.replace(
    "- **GitHub PAT 만료**: 2026-05-27 (잔여 약 80일)",
    "- **GitHub PAT 만료**: 2026-05-27 (잔여 약 58일)"
)

# ─────────────────────────────────────────────
# CHANGE 7: Add CTO-SYSTEM-MAP.md row to 참조 문서 table
# ─────────────────────────────────────────────
content = content.replace(
    "| AADS-BACKUP-STRATEGY | https://github.com/moongoby-GO100/aads-docs/blob/main/reports/AADS-BACKUP-STRATEGY-20260309.md | 백업·복원 정책 (PostgreSQL 일일 pg_dump, .env, Nginx/systemd, Docker 볼륨) |",
    "| AADS-BACKUP-STRATEGY | https://github.com/moongoby-GO100/aads-docs/blob/main/reports/AADS-BACKUP-STRATEGY-20260309.md | 백업·복원 정책 (PostgreSQL 일일 pg_dump, .env, Nginx/systemd, Docker 볼륨) |\n| CTO-SYSTEM-MAP.md | /root/aads/aads-server/docs/knowledge/CTO-SYSTEM-MAP.md | CTO 세션 컨텍스트 복원 (8.6KB, 전체 아키텍처 지도) |"
)

# ─────────────────────────────────────────────
# CHANGE 8: Append version history at the end
# ─────────────────────────────────────────────
version_history = """

---

## 버전 이력 (v12.22 이후)

| 버전 | 날짜 | 주요 변경 |
|------|------|----------|
| v12.22 | 2026-03-09 | AADS-188E: approve-diff API + E2E 66건 PASS |
| v14.0 | 2026-03-30 | 3월 갭 12건 갱신: Pipeline Runner, OAuth, Blue-Green, PC Agent, 메모리 진화, LiteLLM 확장, CTO 온보딩 |"""

content = content.rstrip() + version_history + "\n"

# ─────────────────────────────────────────────
# Write back
# ─────────────────────────────────────────────
with open(path, "w", encoding="utf-8") as f:
    f.write(content)

# Verify changes
checks = [
    ("CHANGE1 header",        "# AADS HANDOVER v14.0" in content),
    ("CHANGE1 date",          "2026-03-30" in content),
    ("CHANGE2 new status",    "AADS-195+" in content),
    ("CHANGE2 old preserved", "AADS-188E 완료 (2026-03-09)" in content),
    ("CHANGE3 LiteLLM",       "OpenRouter 5종" in content),
    ("CHANGE4 Pipeline Runner section", "Pipeline Runner 시스템" in content),
    ("CHANGE5 Docker 7컨테이너", "Docker Compose 7컨테이너" in content),
    ("CHANGE6 PAT 58일",      "잔여 약 58일" in content),
    ("CHANGE7 CTO-SYSTEM-MAP","CTO-SYSTEM-MAP.md" in content),
    ("CHANGE8 version history","## 버전 이력 (v12.22 이후)" in content),
]

print("=== 패치 결과 ===")
all_ok = True
for name, ok in checks:
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {name}")
    if not ok:
        all_ok = False

print()
if all_ok:
    print("모든 변경 성공.")
else:
    print("일부 변경 실패 — 확인 필요.")
