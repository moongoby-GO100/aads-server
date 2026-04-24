# AADS Chat Execution Session Phase 2 Report

작성일: 2026-04-24 KST
릴리스 버전: `0.2.1`

## 백업

- 스냅샷 디렉터리:
  - `/root/aads/aads-server/reports/backups/20260424_chat_execution_session`
  - `/root/aads/aads-server/reports/backups/20260424_chat_execution_session_phase2`

## 실제 반영 범위

- `chat_turn_executions` 테이블과 `chat_messages.execution_id`, `chat_sessions.current_execution_id`를 서버 startup auto-migration에 추가했다.
- 메시지 전송 시 `execution_id`를 먼저 만들고, assistant placeholder/final row를 같은 execution에 묶어 재사용하도록 전환했다.
- Redis stream key를 세션 추론 중심에서 `execution_id` 우선으로 읽고 쓰도록 정리했다.
- `GET /api/v1/chat/sessions/{session_id}/execution`
- `GET /api/v1/chat/executions/{execution_id}`
- `GET /api/v1/chat/executions/{execution_id}/events`
- `streaming-status`와 수동 `/resume`는 execution 상태를 우선 사용하도록 변경했다.
- 서버 startup 복구는 legacy `recovered` 추론 대신 `chat_turn_executions.status in ('running', 'retrying')` execution만 대상으로 다시 붙도록 바꿨다.
- `deploy.sh`는 `code` 모드 health 대기 시간을 60초로 보강해 graceful restart 직후 false negative를 줄였다.

## 검증

- `python -m py_compile`
  - `app/main.py`
  - `app/services/chat_service.py`
  - `app/routers/chat.py`
- `bash -n /root/aads/aads-server/deploy.sh`
- `bash -n /root/aads/aads-dashboard/deploy.sh`
- `curl http://127.0.0.1:8100/api/v1/health`
  - `200 OK`
- `curl http://127.0.0.1:8100/api/v1/chat/executions/{execution_id}`
  - 인증 없는 직접 호출 기준 `401 Unauthorized` 확인
  - 즉, execution 라우트가 live에 등록된 상태임을 확인

## 배포 메모

- `code` 모드 재배포 중 `aads-api` graceful drain이 길어지면서 기존 30초 health 대기에서 false negative가 발생했다.
- 실제 프로세스는 직후 정상 복귀했고, health는 `ok`, 컨테이너 상태는 `healthy`로 확인됐다.
- 후속 재발 방지를 위해 `AADS_DEPLOY_MAX_WAIT` 기본값을 `code` 모드에서 60초로 상향했다.

## 남은 과제

- legacy placeholder/recovered 경로는 아직 fallback 호환 코드가 남아 있다.
- Codex 재시도는 동일 모델 continuation 재호출이며, 터미널 PTY처럼 같은 프로세스 재부착은 아니다.
- `pytest`는 현재 작업 venv에 모듈이 없어 실행하지 못했다.
