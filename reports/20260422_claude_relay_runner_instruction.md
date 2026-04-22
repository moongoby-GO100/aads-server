# 2026-04-22 Claude Relay Runner Instruction

## 실측 요약

- 확인 시각: `2026-04-22 13:30:16 KST`
- systemd 유닛: `/etc/systemd/system/claude-relay.service`
- 실제 실행 파일: `/root/aads/aads-server/scripts/claude_relay_server.py`
- 현재 워크트리 관련 변경:
  - `scripts/claude_relay_server.py`
  - `scripts/claude-docker-wrapper.sh`
- 로컬 검증:
  - `python3 -m py_compile scripts/claude_relay_server.py` 통과
  - `bash -n scripts/claude-docker-wrapper.sh scripts/claude-oauth-wrapper.sh` 통과

## 핵심 문제

호스트 CentOS7에서 직접 `claude` CLI를 실행하면 GLIBC 호환성 문제가 발생할 수 있다.
이를 피하려면 호스트 `claude-relay`가 호스트 CLI 대신 Docker 내부 `/app/scripts/claude-oauth-wrapper.sh`를 통해 Claude CLI를 실행해야 한다.

## 현재 코드 반영 내용

1. `scripts/claude_relay_server.py`
   - `readline()` 기반 NDJSON 파서를 청크 기반 `_iter_ndjson_lines()`로 교체
   - Docker 래퍼 `scripts/claude-docker-wrapper.sh`가 존재하면 `CLAUDE_BIN=""` 뿐 아니라 `CLAUDE_BIN=claude`인 경우에도 래퍼를 우선 사용하도록 보강
2. `scripts/claude-docker-wrapper.sh`
   - 호스트에서 `docker exec`로 컨테이너 내부 Claude OAuth 래퍼를 실행
   - MCP config를 컨테이너 안으로 복사해 동일 세션 컨텍스트 유지

## Runner 지시문

아래 지시문으로 `pipeline_runner_submit(project="AADS", session_id="9102c970-905b-41c9-9de0-f8752f7a5833")` 제출:

```text
TASK_ID: AADS-185
TITLE: Claude relay Docker wrapper 반영 및 운영 검증
PRIORITY: P1
SIZE: M
MODEL: sonnet
DESCRIPTION:
- /root/aads/aads-server/scripts/claude_relay_server.py 와 scripts/claude-docker-wrapper.sh 기준으로 Claude relay를 안정화하세요.
- 목적은 호스트 CentOS7에서 직접 claude CLI를 실행하지 않고 docker exec 기반 wrapper로 우회하는 것입니다.
- 기존 워크트리 변경을 존중하고, unrelated diff는 건드리지 마세요.
- 완료 전 HANDOVER.md 갱신 규칙을 따르세요.

필수 작업:
1. scripts/claude_relay_server.py 변경분 확인
   - _iter_ndjson_lines() 적용 유지
   - CLAUDE_BIN 해석이 wrapper 우선인지 확인
2. scripts/claude-docker-wrapper.sh를 git 추적 대상으로 포함
3. 필요한 최소 테스트 수행
   - python3 -m py_compile scripts/claude_relay_server.py
   - bash -n scripts/claude-docker-wrapper.sh scripts/claude-oauth-wrapper.sh
4. 운영 반영
   - systemctl restart claude-relay
   - curl -s http://localhost:8199/health 확인
   - Claude 계정 폴백 경로 1회 실검증 (Gmail/Naver 중 최소 한 슬롯 성공 여부와 실패 시 로그 확보)
5. 결과를 세션 9102c970-905b-41c9-9de0-f8752f7a5833 에 보고

주의:
- docker compose 전체 재기동 금지
- secret/.env 값 노출 금지
- 무조건 health/log 실측 결과를 포함
```
