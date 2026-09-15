---
task_id: AADS-LAYOUT-003
title: LLM 폴백 체인 — 하드코딩 제거 및 Ops 설정페이지 이관
parent: 2026-09-11 CEO 지시 (litellm 유료 폴백 비활성화 + CLI 대체 + ops 설정페이지화)
status: as-built (구현 완료, 문서만 소급 작성)
author: AADS PM/CTO AI
date: 2026-09-15
---

# 배경

2026-09-11 CEO 지시:
1. LiteLLM 경유 유료 모델 폴백을 비활성화하라 (무료 모델만 유지).
2. 대체 폴백은 Claude CLI, Codex CLI 모델로 구성하라.
3. 폴백 순서/활성화는 하드코딩이 아니라 Ops 설정 페이지에서 관리 가능하게 PRD를 작성하고 저장 후 진행하라.

이 지시 직후 응답이 4회 연속 중단(장시간 응답 중단/생성 실패)되어 TODO(`c811bd16`, "다음단계 설계서 PRD 저장후 정리")가 `pending` 상태로 4일간 방치됐다. 2026-09-15 재확인 결과, **PRD 문서 저장만 누락되었을 뿐 실제 구현은 이미 완료되어 있었다** — 이 문서는 그 as-built 상태를 소급 기록하고 TODO를 공식 종료하기 위해 작성한다.

# 요구사항 대응표

| # | CEO 요구사항 | 구현 상태 | 근거 |
|---|---|---|---|
| 1 | LiteLLM 유료 폴백 비활성화 | ✅ 완료 | `llm_fallback_chains`: route=background step=3(groq-llama-70b, litellm_proxy) `is_enabled=false`. 무료 모델(step=4 groq-gpt-oss-120b, `is_free=true`)만 `is_enabled=true` [DB 조회] |
| 2 | Claude/Codex CLI 대체 폴백 | ✅ 완료 | background step 5(`claude_cli`)/6(`codex_cli`), chat_main step 3-4, runner step 1-2 모두 CLI backend로 등록 [DB 조회] |
| 3 | 하드코딩 제거, Ops 설정페이지 이관 | ✅ 완료 | DB 테이블 `llm_fallback_chains` + 엔진 `app/core/llm_fallback_engine.py`(list/update/reorder/invalidate) + API `app/api/llm_admin.py`(`GET/PATCH /llm-admin/chains`, `POST /chains/reorder`, `POST /chains/invalidate-cache`) + 대시보드 `/admin/model-routing` (`fallbackChain` 상태로 렌더) [코드 조회] |
| 4 | PRD 작성·저장 | ✅ 이 문서로 소급 완료 | 최초 PRD는 채팅 중 작성 중 응답이 중단되어 파일로 저장되지 못함 |

# 아키텍처 (as-built)

```
CEO/Admin ── /admin/model-routing (Next.js) ── fallbackChain state
                    │  GET/PATCH/POST
                    ▼
            /api/v1/llm-admin/chains  (app/api/llm_admin.py)
                    │
                    ▼
        app/core/llm_fallback_engine.py
          - list_chains()      : route_key별 step_order 순 조회
          - update_chain_step(): is_enabled/model_id/max_retries 갱신
          - reorder_chain()    : step_order 재배열
          - invalidate_fallback_cache()
                    │
                    ▼
         PostgreSQL: llm_fallback_chains
          (route_key, step_order, provider, model_id, backend,
           is_free, is_enabled, max_retries, timeout_seconds, conditions)
                    │
                    ▼
        app/core/anthropic_client.py
          "AADS-LLM-AUTH: DB(llm_fallback_chains) 우선, 환경변수 폴백"
```

route_key 3종 현재 운영 중: `background`(내부 자동화), `chat_main`(CEO 채팅), `runner`(Pipeline Runner).

# 검증 결과

| 항목 | 방법 | 결과 |
|---|---|---|
| API 헬스 | `curl /api/v1/ops/health-check` | 200 [실측, 2026-09-15] |
| 체인 데이터 정합성 | `SELECT ... FROM llm_fallback_chains` | 3개 route, 유료 litellm만 비활성, CLI 폴백 정상 등록 [DB 조회] |
| Admin API 라우트 존재 | 코드 조회 | `GET/PATCH /llm-admin/chains`, `POST /chains/reorder`, `POST /chains/invalidate-cache` 확인 |
| 대시보드 UI 실사용(E2E) | 브라우저 미실행 | ⚠️ 미검증 — 브라우저 브릿지 미연결로 실제 화면 클릭 테스트는 못 함. 코드상 `fallbackChain` 렌더 로직 확인만 완료 |

# 잔여 리스크 / 다음 단계

1. **E2E 미검증**: `/admin/model-routing` 화면에서 실제로 토글/재정렬이 저장되는지 브라우저 클릭 테스트가 안 됨. 브라우저 브릿지 연결 후 1회 확인 권장.
2. **TODO 종료 처리 누락**: `c811bd16`(다음단계 설계서 PRD) 4일간 미종료 — 이 문서 저장과 함께 `completed` 처리.
3. **관련 미완료 건**: 같은 세션에 GPT-Image-2.5 이미지 모델 연결(직전 턴 허위 완료 보고 확인됨, `AADS-image25-ntv2-aistudio-diag-20260915` 핸드오버 참조)이 별도로 진행 중.
