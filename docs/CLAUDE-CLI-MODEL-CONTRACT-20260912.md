# Claude CLI 설정·실행 모델 일치 개선 보고서

작성일: 2026-09-12. 범위: `/settings`의 Claude CLI 모델과 채팅 릴레이·Python/셸 러너 실행 경로. 배포 및 최종 검증 결과는 후속 절에 별도 기록한다.

## 1. 확인한 원인

- API의 `claude-opus`는 Opus 5를 의미했지만 호스트 릴레이는 Opus 4.6으로 해석했다.
- 릴레이는 등록되지 않은 모델을 오류로 처리하지 않고 `claude-opus-4-6`으로 실행했다. Sonnet 5와 Fable 계열도 영향을 받았다.
- Python/셸 러너는 명시된 전체 모델 ID를 `opus`·`sonnet` 등 이동 가능한 CLI 별칭으로 축약했다. 셸에는 이전 버전을 다른 버전으로 바꾸는 규칙도 있었다.
- 최종 `modelUsage`의 첫 항목을 실제 모델로 기록했다. 이 항목이 하위 에이전트 모델일 수 있어 주 응답 모델의 증거로 부적절했다.
- CLI 재개 세션을 대화·계정으로만 구분해 모델 변경이 세션 경계에 반영되지 않았다.

읽기 전용 감사 당시 운영 API와 호스트 릴레이 코드, 설정 API, CLI 버전 2.1.259, `chat_turn_executions` 및 릴레이 실행 로그를 대조했다. 최근 24시간 범위에서 Opus 5 요청/Opus 4.6 실행 완료 5건을 확인했다. 이는 당시 조사 범위의 수치이며 전체 장애 건수는 아니다. 과거 DB 기록을 소급 수정하지 않는다.

## 2. 요구사항·설계

1. `scripts/claude_model_contract.py`를 API·호스트 릴레이·러너의 공통 모델 해석 규칙으로 사용한다. 표준 라이브러리만 사용하며 별도 서비스나 DB 변경이 필요 없다.
2. 명시된 전체 모델 ID는 버전을 유지한다. 허용 목록은 구문·정책 허용 목록이지 계정의 사용 권한 보장이 아니다. 새 모델은 목록·테스트·배포를 함께 갱신한다.
3. 알 수 없는 모델은 릴레이에서 HTTP 400으로 거부하고 CLI를 시작하지 않는다. API는 릴레이 계약 버전이 맞지 않으면 해당 릴레이 호출을 중단한다. 기존의 명시적 재시도·폴백 정책 자체를 없애지는 않는다.
4. 요청값, CLI 인자, 실제 응답 모델, 검증 여부, 불일치 여부, 사용 모델 목록을 구분한다. 실제 모델은 최상위 `assistant.message.model`에서 확인한다. `system.init`이나 집계 사용량만으로 확인 완료라고 기록하지 않는다.
5. 실제 모델을 확인하지 못했거나 하나의 호출에서 여러 주 모델이 관찰되면 `unverified`로 기록한다. 텍스트 출력 러너도 동일하다. 정확한 CLI 인자는 별도 실행 로그로 보존한다.
6. API와 릴레이의 재개 키를 대화·계정·정규 모델 ID로 구분한다. 모델 변경 후 첫 요청은 기존 전체 문맥 전달 경로를 사용한다. 순차 배포 중 구형 API는 기존 재개 규칙을 유지한다.
7. DB의 오래된 별칭 메타데이터가 명시된 버전을 다른 버전으로 바꾸지 못하도록 조회 단계에서도 동등성을 검사한다.
8. 전체 호출 사용량·비용을 첫 모델의 부분 집계로 덮어쓰지 않는다. 이 집계는 하위 에이전트까지 포함하므로 주 모델만의 비용으로 해석해서는 안 된다.

| 설정/요청 ID | 고정 실행 ID |
| --- | --- |
| `claude-opus`, `claude-opus-5`, CLI `opus` | `claude-opus-5` |
| `claude-opus-46`, `claude-opus-4-6` | `claude-opus-4-6` |
| `claude-sonnet` | `claude-sonnet-4-6` (기존 AADS 의미 유지) |
| `claude-sonnet-5`, CLI `sonnet` | `claude-sonnet-5` |
| `claude-haiku`, CLI `haiku` | `claude-haiku-4-5-20251001` |
| `claude-fable-5` | `claude-fable-5` |
| `claude-fable-5-1`, `claude-fable-5.1`, `claude-fable-latest` | `claude-fable-5-1` |

## 3. 검증·인수 기준

- 설정 모델별로 실제 subprocess 경계의 `--model` 인자를 검증한다. 가짜 CLI를 사용하므로 과금이나 고객 대화 변경이 없다.
- 알 수 없는 모델은 subprocess를 시작하지 않는다.
- 구형 릴레이 계약 차단, 계정/모델별 재개 격리, 과거 DB 별칭 차단을 테스트한다.
- 하위 에이전트가 사용량 목록의 첫 항목인 경우에도 주 모델 판정이 바뀌지 않는다.
- 응답 증거 누락·복수 주 모델은 확인 불가로 남는다.
- Python/셸 러너 및 원격 복사본 템플릿의 규칙 일치를 검사한다.
- 원격 동기화는 공통 모듈을 먼저 설치하고 해시를 검증한다. 미커밋 작업 파일의 자동 배포를 차단한다.
- 운영 반영은 릴레이 유휴 확인, 현재 운영 버전 기반 격리 릴리스, 후보 직접 health, 짧은 nginx 전환 잠금, 진행 중 스트림 종료, 동일 이미지 standby, 외부 health, 5분 오류 감시를 통과해야 완료로 판단한다.

## 4. 운영상 한계·주의사항

- 모델 ID 일치는 계정 권한·할당량·과금 가능 여부와 별개다. Fable을 실제 호출하는 유료 검증은 이 회귀 테스트에 포함하지 않는다.
- 하위 에이전트는 기존 별도 Sonnet 정책을 유지한다. 따라서 `used_models`에 다른 모델이 있다는 사실만으로 주 모델 불일치로 판정하면 안 된다.
- `unverified`는 실행 실패나 모델명 누락 버그가 아니라 증거 부족 표시다. 요청 모델을 실제 모델로 가장하는 것보다 보수적으로 기록한다.
- 기존 UI의 모델 라벨을 실제 응답 검증 배지로 간주해서는 안 된다. 이번 변경은 실행 경로와 기록 정확성에 집중한다.
- 장애 시 해당 호출의 요청 모델, CLI 인자, 응답 모델 증거 및 명시적 폴백 이력을 함께 확인한다. 실제 실행 증거 없이 과거 기록을 수정하지 않는다.
- 원격 러너가 작업 중이면 강제 중단하지 않고 적용을 보류한다. 배포 상태는 소스 저장, 호스트 재시작, API 전환, 원격 러너 반영을 각각 구분한다.

## 5. 출처

- [Anthropic Claude Code 모델 설정](https://code.claude.com/docs/en/model-config): 전체 모델 ID로 버전 지정, 이동 별칭의 의미, 버전 요구사항, 재개 시 모델 및 Fable 과금 관련 주의사항. 2026-09-12 조회.
- 내부 코드: `app/services/model_selector.py`, `app/services/model_registry.py`, `app/services/pipeline_runner_service.py`, `scripts/claude_relay_server.py`, `scripts/pipeline-runner.sh`, `scripts/sync_pipeline_runner_remote.sh`.
- 운영 증거: `/api/v1/llm-models?provider=anthropic`, `/api/v1/settings/runner-models`, `/api/v1/settings/directive-models`, `chat_turn_executions.requested_model/actual_model`, `claude-relay.service`의 `CLI: model=` 로그. 인증 토큰과 고객 대화 원문은 문서에 포함하지 않았다.

## 6. 적용 상태

구현·검증 진행 중. 이 문서의 존재만으로 운영 배포 완료를 의미하지 않는다. 최종 결과는 실제 검증 후 추가한다.
