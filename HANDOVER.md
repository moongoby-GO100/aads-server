# AADS HANDOVER
최종 업데이트: 2026-04-23

## AADS-189A
- `migrations/053_llm_model_registry.sql` 추가로 `llm_models`, `llm_key_audit_logs` 테이블을 도입했다.
- `app/services/model_registry.py` 추가로 provider 템플릿 기반 모델 레지스트리, provider 요약, 수동/자동 sync, cache invalidation 공통 계층을 구현했다.
- `app/api/llm_keys.py`는 create/update/activate/deactivate 시 priority 충돌 검증, 감사 로그 적재, stale key cache 제거, registry sync를 수행한다.
- `app/api/llm_models.py`와 `app/main.py` 라우터 등록으로 `/api/v1/llm-models`, `/api/v1/llm-models/providers/summary`, `/api/v1/llm-models/sync` API를 제공한다.
- `app/services/model_selector.py`, `app/services/pipeline_runner_service.py`, `app/api/pipeline_runner.py`, `app/services/code_reviewer.py`가 DB 레지스트리의 실행 가능 모델 필터를 우선 사용하고, 활성 모델이 비어 있으면 기존 하드코딩 경로로 안전 폴백한다.
- `tests/unit/test_model_registry.py`로 provider 정규화, unknown provider review 상태, executable filter 폴백 규칙을 검증한다.

## AADS-188
- `app/api/llm_keys.py` 추가로 `llm_api_keys` 조회·추가·수정·비활성화 API 제공.
- `app/main.py`에 `/api/v1/llm-keys` 라우터 등록.
- 대시보드 Settings 탭에서 LLM API 키 관리 UI를 연동하도록 백엔드 계약 추가.

## AADS-187
- `scripts/update_claude_all_servers.sh` 전면 재작성.
- 서버 114를 첫 순서로 즉시 처리하도록 배치.
- Claude Code CLI, Codex CLI, `claude-agent-sdk` 버전 전후 비교와 변경 시 Telegram 알림 추가.
- `/root/aads/.env` 로드, `/root/tmp` 기반 pip 설치, 서버별 실패 내성, 최종 성공/실패 요약 전송 추가.

## 운영 반영 포인트
- 목표 cron 라인: `0 4 * * * /root/aads/aads-server/scripts/update_claude_all_servers.sh >> /var/log/claude_update.log 2>&1`
- 현재 워크스페이스에는 실제 시스템 crontab과 원격 서버 상태가 없어서 파일 수정만 반영됨.
