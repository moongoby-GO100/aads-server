# PC Agent 실시간 화면 스트리밍 — 검증 결과

## 2026-09-10 — Runner 저장소 판정 최종 보완

### STEP 0 기존 구현 조사 및 분류

| 항목 | 분류 | 결과 |
| --- | --- | --- |
| `is_aads_backend_instruction` / `is_aads_dashboard_instruction` | 수정 | 본문 전체 정규식 추론을 canonical TARGET 판정 결과 사용으로 축소했다. |
| `resolve_project_workdir` | 수정 | AADS TARGET 파싱 실패를 호출자에게 반환해 임의 기본 경로 선택을 막는다. |
| `aads_instruction_target` | 신규 | 모든 `TARGET:` 행을 검사해 두 정확한 루트만 허용하고 unknown·접두사 사칭·중복·혼합·다중대상을 fail-closed 처리한다. |
| `pre_validate`, `run_job`, `deploy_job`, `reject_job` | 수정 | 동일한 resolver 실패 시 `invalid_aads_target`으로 종료해 실행/승인/원복 판정이 갈라지지 않게 했다. |
| 프로젝트 락, DB `FOR UPDATE SKIP LOCKED` claim, isolated worktree/bluegreen 경로 | 유지 | 기존 락·fencing·worktree 계약은 변경하지 않았다. |
| `tests/unit/test_pipeline_runner_worktree_policy.py` | 수정 | canonical 문법과 fail-closed 경로가 네 lifecycle 지점에 적용됨을 정적 회귀로 추가했다. |
| 삭제 | 없음 | 삭제 대상 및 호출처 영향 없음. |

### 변경 파일

- `scripts/pipeline-runner.sh`
- `scripts/pipeline-runner.sh.local` — primary와 동일하게 유지
- `tests/unit/test_pipeline_runner_worktree_policy.py`
- `HANDOVER.md`, `RESULT.md` — 지시된 운영 절차와 미실행 항목 기록

지시서 외 파일 변경은 없다. 이전 보존 아티팩트의 범위 안에서 runner·동기화 템플릿·정책 테스트·필수 인수인계 문서만 수정했다.

### 검증 및 미실행

- 코드 수준 확인: primary runner와 `.local`에 동일 판정 로직 및 동일 lifecycle guard를 반영했다.
- 실행 검증은 사용자 제한(파일 변경만 허용)에 따라 수행하지 않았다. 따라서 pytest, bash 문법 검사, Git 상태/차이 조회, DB `pipeline_jobs` 및 `chat_workspace_change_ledger` preflight도 미실행이다.
- 커밋, push, 빌드, 배포, runner 교체 및 API 직접 재시작은 수행하지 않았다.
- 런타임 반영 완료 여부는 `HANDOVER.md`의 안전한 유휴 교체 절차에서 canonical digest를 가진 새 PID와 첫 DB claim fencing을 실측하기 전까지 미확정이다.

## 구현 요약

모든 5개 파일에 스트리밍 기능이 이미 구현 완료 상태. 서버 재시작 후 정상 동작 확인.

### 구현된 파일

| 파일 | 상태 | 변경 내용 |
|------|------|-----------|
| `app/models/pc_agent.py` | ✅ 완료 | `StreamConfig` 모델 (fps/quality/scale/monitor), `WSMessage.type: str` |
| `app/services/pc_agent_manager.py` | ✅ 완료 | `_streaming_subscribers`, `add/remove_stream_subscriber`, `start/stop_stream`, `broadcast_frame` |
| `app/api/pc_agent.py` | ✅ 완료 | WS `/pc-agent/stream/{agent_id}`, POST `start`/`stop`, `stream_frame` 핸들링 |
| `pc_agent/commands/screen_stream.py` | ✅ 완료 | `ScreenStreamer` 클래스 (캡처 루프, JPEG+base64, 듀얼모니터 지원) |
| `pc_agent/agent.py` | ✅ 완료 | `stream_start`/`stream_stop` 명령 핸들러 (ws 참조 직접 전달) |

---

## 검증 체크리스트

### ✅ 구현 목표
PC Agent 실시간 화면 스트리밍 (서버 WS 릴레이 + 클라이언트 캡처/전송, 1~5fps)

### ✅ 검증 방법

```bash
# 에이전트 목록 확인
curl -s http://localhost:8100/api/v1/pc-agent/agents

# 스트리밍 시작
curl -s -X POST "http://localhost:8100/api/v1/pc-agent/stream/{agent_id}/start" \
  -H "Content-Type: application/json" \
  -d '{"fps":2,"quality":50,"scale":0.5,"monitor":-1}'

# 스트리밍 중지
curl -s -X POST "http://localhost:8100/api/v1/pc-agent/stream/{agent_id}/stop"
```

### ✅ 완료 기준
- `/pc-agent/stream/{agent_id}/start` → `{"command_id":"...", "status":"streaming", "config":{...}}` 반환
- `/pc-agent/stream/{agent_id}/stop` → `{"command_id":"...", "status":"stopped"}` 반환
- 미연결 에이전트 → `404` + 한국어 에러 메시지

### ✅ 실패 기준
- 엔드포인트 404 응답 → **통과** (재시작 후 정상 라우팅)
- `stream_start` 시 서버 크래시 → **통과** (에러 없음)
- 에이전트 미연결 시 500 → **통과** (404 정상 반환)

### ✅ 서비스 재시작 확인
```
$ docker ps --filter name=aads-server
e5468bd447b4  aads-server  Up 3 minutes (healthy)
```
재시작 후 5개 에이전트 자동 재연결 확인.

### ✅ 에러 로그 0건
```
$ docker logs --since 30s aads-server 2>&1 | grep -i error
(출력 없음)
```

---

## 테스트 결과

| 테스트 | 결과 | 응답 |
|--------|------|------|
| `GET /pc-agent/agents` | ✅ 200 | 5개 에이전트 연결 |
| `POST /stream/{id}/start` (유효 에이전트) | ✅ 200 | `{"status":"streaming"}` |
| `POST /stream/{id}/stop` (유효 에이전트) | ✅ 200 | `{"status":"stopped"}` |
| `POST /stream/nonexistent/start` | ✅ 404 | 한국어 에러 메시지 |
| 에러 로그 | ✅ 0건 | - |

검증 완료: 2026-03-24T07:32 UTC
