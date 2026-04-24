"""HANDOVER.md 갱신 — Governance v2.1 마감 작업 추가"""

HANDOVER = "/root/aads/aads-server/HANDOVER.md"

with open(HANDOVER, "r") as f:
    content = f.read()

NEW_ENTRY = """- **2026-04-25 Governance v2.1 마감 (직접 작업)**:
  - **P0 temperature 배선 완료**: `model_selector.py`에 `contextvars` 기반 `_ctx_temperature`를 도입해 `call_stream()` → `_stream_litellm_anthropic` / `_stream_litellm_openai` / `_stream_cli_relay` 3개 LLM 경로 모두에 인텐트별 temperature를 전달한다. `resolve_intent_temperature()` → `intent_policies.temperature` DB 조회 → 하드코딩 맵 폴백 체인으로 작동. 실측 검증: greeting=0.1, strategy=0.15, code_task=0.15, casual=0.2.
  - **P0 W3 DB 마이그레이션 완료**: `scripts/migrations/20260424_governance_v2_1_w3.sql` 실행으로 `prompt_assets`, `prompt_asset_versions`, `session_blueprints`, `prompt_change_requests`, `cr_approvals`, `compiled_prompt_provenance` 6개 테이블 생성. `session_blueprints`에 `default.standard` 시드 삽입.
  - **P1 prompt_compiler 활성화**: W3 테이블 생성으로 `PromptCompiler.compile()` (chat_service.py L3873)이 실제 `prompt_assets` + `session_blueprints` DB 조회 경로로 작동 시작. `record_prompt_provenance()`로 `compiled_prompt_provenance`에 빌드 이력 저장.
  - **P0 feature_flags.py 호스트 패치**: `governance_enabled()` helper 함수를 호스트 파일에 추가 (로컬 워크트리에만 존재하던 상태 보정).
  - **runner-af09281f 정리**: depends_on이 rejected_done인 영구 대기 러너를 error 상태로 전환.
  - **runner-34c0836a 제출**: Admin Dashboard 4개 페이지(governance/model-parity/deploy/sessions) 일괄 구현 러너 (실행 중).
  - **API Hot-Reload**: 54개 모듈 재로드 완료, health-check 전항목 정상 확인.
"""

# "## 현재 진행 상태" 다음 첫 번째 "- " 앞에 삽입
marker = "## 현재 진행 상태 (2026-04-24)"
if marker in content:
    content = content.replace(marker, "## 현재 진행 상태 (2026-04-25)\n" + NEW_ENTRY)
    with open(HANDOVER, "w") as f:
        f.write(content)
    print("HANDOVER.md updated OK")
else:
    print("WARNING: marker not found, appending instead")
    with open(HANDOVER, "a") as f:
        f.write("\n\n## 2026-04-25 Governance v2.1 마감\n" + NEW_ENTRY)
    print("HANDOVER.md appended OK")
