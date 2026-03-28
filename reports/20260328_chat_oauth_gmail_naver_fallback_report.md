# 채팅 OAuth Gmail 한도 → Naver 전환 실패 원인 및 조치 (2026-03-28)

## 요약

- **현상**: Gmail 계정이 rate limit에 걸렸을 때 Naver로 자동 전환되어야 하나 동작하지 않는 것으로 보고됨.
- **원인 1 (핵심)**: CLI Relay(`claude_relay_server.py`)는 AADS `session_id`당 **하나의** Claude CLI `session_id`만 `/tmp/claude_relay_sessions.json`에 보관한다. 슬롯1(Gmail) 요청에서 `system/init`으로 CLI 세션이 잡힌 뒤 `result`에서 429 등으로 실패해도 그 매핑이 저장될 수 있다. 이후 폴백으로 슬롯2(Naver)를 호출하면 **같은 AADS 세션**에 대해 `--resume <Gmail 쪽 CLI 세션>`이 붙어 Naver OAuth 토큰으로는 무결성이 깨져 재시도가 실패한다.
- **원인 2 (UI 인지)**: 채팅 상단 **Naver/Gmail 토글**은 `POST /settings/auth-keys` → `auth_provider.set_token_order()`만 바꾼다. 이는 **LiteLLM/환경변수 API 키 순서**용이며, **호스트 CLI Relay + `.env.oauth`** 경로와는 연결되어 있지 않다. 즉 “Naver로 스위치”를 눌러도 릴레이 슬롯 순서(코드상 1→2 고정 폴백)에는 반영되지 않는다.

## 코드 조치

- 파일: `app/services/model_selector.py`
- 내용: `_stream_cli_relay`가 `error`로 끝난 뒤 다음 폴백(다른 OAuth 슬롯 또는 SDK)으로 넘어가기 전에
  - 메모리 `_cli_session_map`에서 해당 `session_id` 제거
  - `DELETE {CLAUDE_RELAY_URL}/sessions/{session_id}` 호출로 릴레이 쪽 매핑 제거
- 효과: Gmail 실패 직후 Naver 재시도는 **새 CLI 대화**로 진행(요청 본문의 메시지 히스토리는 그대로 전달).

## 검증

- 로컬: pre-commit(ruff) + 단위 테스트 + LLM smoke (커밋 시)
- 배포 게이트: `deploy.sh code` — Phase 0.5 컨테이너 내 `py_compile` + `app.main` import 통과

## 배포

- **완료** (2026-03-28 KST): `bash /root/aads/aads-server/deploy.sh code`
  - `app` 볼륨 마운트(`/root/aads/aads-server/app:/app/app`)로 코드 즉시 반영, `supervisorctl` graceful 재시작
  - Health `http://localhost:8100/api/v1/health` OK (~12초), DB 스키마·채팅 테이블·LLM 상태 검증 통과

## 후속 권장

- (선택) `_cli_session_map`을 `session_id`만이 아니라 `session_id + oauth_slot` 단위로 관리하면, 한 세션에서 슬롯 전환 없이 다시 슬롯1을 쓸 때 resume 일관성을 더 엄밀히 맞출 수 있음.
- (선택) UI 토글과 Relay `CURRENT_OAUTH`를 연동하려면 Relay `POST /oauth/switch` 또는 동일 의미의 백엔드 API가 필요함 (현재는 분리됨).
