# Pipeline C 에러 복구 체계 구축 (2026-03-12)

## 배경
- Pipeline C "Session ID already in use" 충돌로 전체 클로드봇 에러
- 에러 발생 시 채팅 AI에 알림이 가지 않아 CEO가 직접 발견해야 했음
- 채팅 AI가 에러난 작업을 kill/재실행할 수단이 없었음

## 수정 내역

### 1. Session ID 충돌 근본 수정 (`pipeline_c.py`)
- `_run_claude_code` (detached) + `_run_claude_code_direct` 양쪽 모두
- `continue_session=True`여도 항상 새 UUID 생성
- 기존: continue_session이면 기존 session-id 재사용 → 충돌
- 수정: 무조건 `self.claude_session_id = str(uuid.uuid4())`

### 2. 에러 시 채팅 AI 자동 트리거 (`pipeline_c.py`)
5개 에러/알림 경로 모두에 `_trigger_ai_reaction()` 호출:
| 경로 | 트리거 내용 |
|------|------------|
| 초기 Claude Code 실행 오류 | 실패 원인+해결방안 보고 요청 |
| 재작업 오류 | 재작업 실패 원인 보고 요청 |
| 승인 대기 | 변경사항 요약+승인 판단 정보 (기존) |
| run() 전체 예외 | 오류 원인 분석+대안 제시 (기존) |
| approve() 배포 예외 | 배포 오류 원인+해결방안 보고 |

### 3. Cancel/Retry 도구 추가
**`pipeline_c_cancel(job_id)`** — 강제 취소
- 메모리에 있으면: session-id로 원격 Claude 프로세스 자동 kill + DB error 전환
- 메모리에 없으면: DB 상태만 cancelled로 전환

**`pipeline_c_retry(job_id)`** — 에러/취소 작업 재실행
- 메모리 또는 DB에서 원본 instruction 조회
- 동일 지시로 새 파이프라인 시작 (새 job_id 발급)
- error/done 상태에서만 가능 (running이면 먼저 cancel 필요)

### 4. 기존 수정 (같은 세션)
- **model_selector.py**: IndentationError + betas 파라미터 SDK 호환성 수정
- **ceo_chat_tools.py**: python -c, kill/pkill/killall 화이트리스트 추가, SSH 포트 지원, NTV2 dual workdir
- **project_config.py**: NTV2/SF 서버 IP(114.207.244.86) + port(7916) 수정
- **dashboard page.tsx**: 시스템 트리거 메시지 시각적 구분

### 5. 공동 메모리(ai_observations) 반영
- `deploy_verification_mandatory`: 배포 전 필수 검증 절차 (6개 프로젝트)
- `no_false_reports`: 검증 없이 "됩니다" 보고 금지 (6개 프로젝트)
- `pipeline_c_error_handling`: 도구 5종 사용법 + 에러 시 cancel→분석→retry 플로우 (6개 프로젝트)

## 커밋 이력
| 커밋 | 내용 |
|------|------|
| `9f339e2` | 시스템 트리거 메시지 시각적 구분 (dashboard) |
| `19ad11d` | model_selector.py IndentationError 수정 |
| `5f65238` | betas 파라미터 SDK 호환성 수정 |
| `0808bb7` | python -c 화이트리스트 복원 + assistant-last 방어 |
| `a1d258a` | kill/pkill/killall 화이트리스트 추가 |
| `57c41f0` | NTV2/SF SSH config 수정 (IP+포트+전 SSH 호출) |
| `ef51218` | NTV2 dual workdir path validation |
| `9ed9a83` | Pipeline C 에러 트리거 + Session ID 충돌 수정 |
| `ca38323` | Pipeline C cancel/retry 도구 추가 |

## 검증 결과
- [x] pipeline_c.py 문법+import 검증 통과
- [x] ceo_chat_tools.py 문법+import 검증 통과
- [x] 도구 스키마 5종 등록 확인
- [x] execute_tool 분기 5종 매칭 확인
- [x] cancel 실제 테스트 (에러 작업 DB 업데이트) 통과
- [x] aads-api 재시작 후 에러 0건
- [x] 211서버 stuck Claude 프로세스 전부 kill 완료
