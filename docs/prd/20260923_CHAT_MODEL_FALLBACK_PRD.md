# AADS 채팅 모델별 폴백 정책 — PRD·기술 설계 v1.0

- 작성 근거: CEO «기획 설계 PRD작성해서 저장하고 진행해»
- 실측 기준: 2026-09-23 08:57:46 KST (호스트 date)
- 프로젝트: AADS / 우선순위: P1 / 구현 크기: L
- 상태: 설계 저장, 구현·검증은 Pipeline Runner에서 수행. 운영 적용 완료 문서가 아님.
- 선행 문서: `docs/PRD-AADS-FALLBACK-OPS-v1.0.md`, `docs/prd/AADS-FALLBACK-SETTINGS-PRD.md`.
- 이번 문서는 기존 통합 관리 설계의 **채팅 모델별 정책**을 구체화한다. 러너 크기별 배정·background_llm·agent_* 정책은 변경하지 않는다.

## 1. 제품 목표와 범위

CEO와 채팅 사용자가 선택한 모델의 특성을 유지하면서 한도·일시 장애를 복구하고, 실제 답변 모델과 전환 이유를 확인하게 한다. 운영자는 기존 `/admin/model-routing`에서 모델별 후보·금지 경로·실제 적용 버전을 확인한다.

포함: 채팅 최초 호출, 계정 교체, 429, 첫 응답 타임아웃, missing-done, 재개 경로의 공통 정책; 모델 고정/자동 전환; 정책 관리 API; SSE/DB 이력; 화면 검증.

제외: 모든 회사 모델의 재조사, 구독 재결제/재로그인, 러너 재설정, 성능 우열 확정, 자동 유료 API 활성화, 미디어 생성 폴백, 관련 없는 스트리밍 구조 재작성.

## 2. 현재 확인한 근거와 문제

| 근거 | 확인 결과 | 해결 방향 |
|---|---|---|
| `chat_service.py::_cross_provider_chat_fallback_chain` | GPT-5.6, Opus 5 등 고정 목록과 Gemini/DeepSeek 제외 조건 존재 | 모든 채팅 복구 진입점이 같은 resolver를 사용 |
| `model_selector.py::_configured_llm_fallback_candidates` | route_key=llm 공통 목록 조회와 provider 제외 조건 공존 | 요청 모델별 정책 및 기능·권한 필터 통합 |
| `fallback_chain_loader.py` | DB 조회, 30초 캐시, 조회 실패 시 오래된 캐시 반환 | 버전·캐시 연령·비상 동작 계약 추가 |
| `llm_models` SELECT | Opus 5.5, GPT-6 Sol/Luna의 tier와 fallback_group이 NULL; Astra는 S | 별칭과 실행 ID를 정규화하고 정책용 그룹 명시 |
| `model_routing_preferences` SELECT | llm 경로에 이전 세대 후보가 남음; Gemini API 후보 비활성 | 기존 운영자의 비활성·과금 설정 보존 |
| dashboard ChatBubble/page.tsx | requested_model, fallback_reason, model_fallback 표시 일부 존재 | 기존 이벤트·표시 재사용, 새로고침/재개 일관성 보강 |
| Git/ledger | model_selector.py 등 다른 세션 dirty, chat_service.py 타 세션 dirty 기록 | 본체 직접 수정 금지; 격리 worktree 및 병합 전 재검사 |

위 모델명은 운영 DB 등록값이다. DB의 verified는 이번 턴의 실호출 또는 공식 성능 검증을 뜻하지 않는다. 아래 그룹은 운영 정책 초안이며 동급 성능의 실측 주장이 아니다.

## 3. 사용자 경로

1. 첫 진입: 채팅 입력창과 모델 선택은 유지. 모델 메뉴에 «선택 모델 유지»/«장애 시 자동 전환»을 표시한다. 기존 수동 선택 세션은 고정 동작을 보존하고, 기존 자동 세션은 자동 동작을 보존한다. 신규 세션 기본은 장애 시 자동 전환이며 하향·유료 전환은 꺼짐.
2. 반복 사용: 세션 정책을 서버에 저장하고 다른 세션·새로고침·모바일 재접속에서도 복원한다. 한 응답의 전환이 다음 질문의 선택 모델을 영구 변경하지 않는다.
3. 실패 복구: 동일 모델의 다른 사용 가능 계정을 우선 사용. 정책에 따른 대체 모델로 전환하면 «요청 모델 → 실제 모델 / 한도 또는 장애»를 응답에 표시한다.
4. 후보 소진·고정 모드 실패: 입력과 부분 답변을 보존하고 «다시 시도»와 «다른 모델 선택» 제공. 새 시도는 새 idempotency key를 사용하며 이미 끝난 도구를 다시 실행하지 않는다.
5. 운영: 관리 화면에서 후보 순서·제외 이유·정책 버전·최근 오류를 보고, 미리보기 검증 후 저장·이전 버전 복원. 일반 채팅에 운영용 ID·계정 이메일·토큰은 노출하지 않는다.
6. 권한/세션 만료: 관리자 API는 테넌트·역할 검증, 일반 사용자는 자기 세션 정책만 수정. 401/403/네트워크 실패는 재로그인·재시도 경로를 제공하고 입력을 보존한다.

## 4. 정책과 모델 연결표

공통 순서: 요청 모델/계정 → 같은 실행 모델의 다른 허용 계정 → 다른 제공사의 허용 동급 CLI → 같은 그룹의 남은 후보 → 명시 허용된 하향 후보 → 정책이 허용한 외부 API → 복구 안내.

| 요청 모델(운영 DB ID) | 우선 대체 모델 | 추가 후보 | 조건 |
|---|---|---|---|
| claude-opus-5-5 / 최신 claude-opus 별칭 | gpt-6-astra | claude-fable-5-1, gpt-6-sol | 추가 후보는 품질 하향 허용 시에만 |
| gpt-6-astra | claude-opus-5-5 | claude-fable-5-1, gpt-6-sol | 상동 |
| claude-fable-5-1 | gpt-6-sol | claude-sonnet-5 | standard 그룹 제안, 배포 전 기능 검증 |
| claude-sonnet-5 / claude-sonnet | gpt-6-sol | claude-fable-5-1 | 동일 계정의 공동 한도면 Claude 후보 생략 |
| gpt-6-sol | claude-sonnet-5 | claude-fable-5-1 | 운영자가 순서 조정 가능 |
| gpt-6-luna | claude-haiku의 실행 모델 | gpt-6-sol, claude-sonnet-5 | 상향은 별도 allow_upgrade 설정, 기본 꺼짐 |
| claude-haiku | gpt-6-luna | gpt-6-sol, claude-sonnet-5 | 상동 |
| 그 외 기존 선택 가능 모델 | 검증된 fallback_group의 다른 제공사 후보 | 정책에 등록된 후보만 | 그룹 미정·기능 미확인은 자동 추론하지 않고 제외 이유 표시 |

- paid_api_enabled, 공급자 금지, 테넌트 권한, 사용자가 선택한 fixed, 기능 호환성은 후보 순위보다 먼저 적용한다.
- Gemini CLI와 Gemini API는 backend로 구분한다. 기존 비활성 Gemini/API 경로를 이 작업에서 켜지 않는다. 향후 허용 시 외부 API는 LiteLLM 경유.
- 이름 문자열만으로 최신 여부·성능·CLI 가능 여부를 판정하지 않는다. model_id와 execution_model_id 별칭을 정규화하고 `(provider, backend, execution_model_id, account)`로 중복 제거한다.
- is_active/is_selectable/is_executable/retired_at 및 실제 계정·backend 가용성으로 필터한다. supports_tools/vision, 구조화 출력, context 한도를 입력 요구사항과 비교; capability 미확인은 필요한 기능에 한해 제외한다.
- fixed는 동일 모델 계정 교체까지만 허용한다. 다른 모델 전환은 최초 호출·429·타임아웃·재개·missing-done 어디서도 우회할 수 없다.

## 5. 오류·계정·실행 안전 설계

| 오류 범주 | 처리 | 금지 사항 |
|---|---|---|
| 401/403 인증 | 해당 계정 제외, 같은 모델의 다른 계정 확인 | 토큰 삭제·자동 재로그인·다른 테넌트 계정 사용 |
| 계정/주간 quota 429 | 검증된 reset_at까지 해당 quota scope 차단 | 같은 계정의 별칭 모델로 회피 반복 |
| temporary 429/overload/5xx | Retry-After 존중, 제한적 재시도/다른 후보 | 임의로 주간 한도로 기록 |
| 첫 응답 timeout/빈 응답 | 공통 예산 안에서 전환 | 대기·슬롯 스캔을 모델 시도로 계산 |
| 도구 업무 오류/잘못된 입력 | 사용자에게 원인과 수정 경로 반환 | 모델 장애로 오인하여 쓰기 도구 재실행 |
| 사용자 취소/owner lease 상실 | 즉시 중지 | 폴백·재개로 취소 우회 |
| 부분 응답 후 stream 종료 | 동일 execution의 보존된 상태로 복구 | 부분 답변 유실·도구 중복 부작용·완료 이벤트 중복 |

- 전체 실행의 deadline과 실제 모델 호출 attempt budget을 최초/재개 경로가 공유한다. 현재 운영 상한을 읽어 사용하고 하위 폴백에서 새 예산을 만들지 않는다.
- 동일 후보 순환 금지, provider 장애 범위의 후보 건너뛰기, reset_at 경과 후 반개방 실호출 1회로 복구 확인. 시간 경과만으로 실측 잔량을 0%/100%로 확정하지 않는다.
- 실행 소유권은 DB owner_instance+owner_epoch; 대기 중 lease heartbeat. 도구 결과/side-effect receipt를 이어받고 안전한 재개 근거 없으면 자동 재실행하지 않는다.
- 정책 변경 중 실행은 시작 시 policy_version을 고정하되 보안 차단·모델 종료·계정 비활성은 최신 상태로 확인한다.

## 6. 데이터·API 계약 (구현 예정)

기존 `model_routing_preferences`, `llm_models`, `fallback_chain_loader`, `llm_fallback_engine`을 재사용한다. tenant scope/revision/모델별 정책을 수용하지 못하는 경우에만 additive migration을 만든다. 신규 전역 defaults로 기존 테넌트 설정을 덮어쓰지 않는다.

정책 최소 필드: requested_model_key, mode(fixed/auto), ordered_candidates, allow_downgrade=false, allow_upgrade=false, allow_paid_api=false, required_capabilities, enabled, revision, updated_by, updated_at. 계정 시크릿은 저장하지 않는다.

- resolver 입력: tenant, user, execution, 요청 모델, 입력 기능 요구, policy_version, 사용한 후보/남은 예산.
- 출력: 정규화 후보 목록, 후보별 허용/제외 사유, 적용 정책 버전. API dry-run과 실제 실행이 같은 함수를 사용.
- 기존 관리 API 확장 우선. 필요 시 제안 경로 `/api/v1/ops/chat-fallback-policies` GET/PUT, `/preview` POST. 실제 라우트와 OpenAPI를 검증 보고에 기록.
- PUT은 expected_revision 비교(CAS), 관리자·테넌트 검증, 모델 FK/가용성/순환 검증, 감사 before/after, 트랜잭션 저장. stale revision은 409.
- 기존 chat session 설정 API에 mode/허용 플래그를 추가. 필드 부재는 기존 수동/자동 의미를 보존.
- 캐시 정상 TTL은 기존 30초 정책을 유지하고 변경 후 모든 worker의 버전 갱신을 검증. DB 장애 시 bounded last-known-good 정책만 허용하고 최신 차단 상태 확인 불가 후보는 보수적으로 제외. 오래된 캐시의 무제한 사용·고정 모델명으로의 우회 금지.
- 기능 플래그 OFF는 기존 경로로 돌아가되, 기존 fixed 사용자 선택과 과금 금지 조건은 유지. 문서만으로 안전을 가정하지 않고 rollback 회귀 테스트 필수.

## 7. 스트리밍·화면 계약

기존 model_fallback 이벤트를 하위 호환 확장: execution_id, requested_model, from_model, to_model, reason_code, attempt, policy_version, timestamp. 실제 모델 결정 후 done에 actual_model/requested_model/fallback_reason과 이력을 저장한다. 계정은 감사 로그의 비밀 없는 내부 식별자로만 관리한다.

재접속/SSE replay/DB 이력 로드에서 동일 전환 배지를 중복 생성하지 않는다. 전환 이력과 실제 최종 응답 모델은 분리한다. 오류를 자연어 delta로만 흘려서 정상 답변으로 저장하지 않는다. session model 선택값을 실제 모델값으로 덮어쓰지 않는다.

관리 페이지는 기존 `/admin/model-routing`을 확장하고 채팅 `/chat`은 간단한 모드 선택과 결과 표시만 추가한다. 모바일 줄바꿈·터치·재연결·세션 복구를 검증한다.

## 8. 완료 기준과 검증 매트릭스

| ID | 시나리오 | 합격 기준 |
|---|---|---|
| AC01 | 요청 모델 성공 | 다른 모델 호출 0, 요청/실제 모델 일치 |
| AC02 | 동일 모델 첫 계정 quota | 사용 가능 다른 계정 호출, 다른 모델 전환 없음 |
| AC03 | 모든 동일 모델 계정 불가 | auto에서 표의 허용 동급 후보 선택 |
| AC04 | fixed + 429/timeout/missing-done/resume | 모든 진입점에서 다른 모델 호출 0 |
| AC05 | alias·retired·disabled·capability 불일치 | 중복/부적합 후보 호출 0, preview 사유와 실행 일치 |
| AC06 | paid/하향/상향/Gemini 금지 | 설정 OFF 후보 호출 0; 허용 플래그 조합 검증 |
| AC07 | 재시도 소진/순환/DB 실패 | 전역 attempt/deadline 준수, 명확한 terminal 상태 |
| AC08 | partial + 쓰기 도구 + 중단 | 부분 응답 보존, side effect 1회, 완료 1회 |
| AC09 | 취소·lease 교체·재개 | 이전 owner 후속 호출/도구 실행 0 |
| AC10 | 모델 선택/전환/새로고침/세션 이동 | 사용자 선택 유지, 최종 모델·전환 사유 동일 |
| AC11 | 정책 수정·권한·CAS·멀티워커 | 권한 없는 수정 차단, 409 검증, 30초 내 새 버전 적용 |
| AC12 | 임시429/weekly/reset 경과 | 오류 scope 정확, 재측정 전 잔량 미확인 표시 |
| AC13 | 기능 flag rollback/정책 revision 복원 | 이전 정책 복원, fixed/과금 금지 보존 |

단위 테스트는 네트워크 없는 fault injection, 통합 테스트는 resolver→실제 stream adapter mock→SSE→DB persistence 경로로 작성한다. production quota 소진 유발 금지. 실호출 비용은 측정할 수 있을 때만 기재한다.

UI: Playwright로 /chat, /admin/model-routing 인증·표시·저장·재접속 캡처. 불가 시 HTTP→API health→컨테이너 상태 폴백을 수행하고 화면 완료로 간주하지 않는다.

## 9. 실행·릴리스·롤백

1. Runner 착수 프리플라이트: /root/aads/AGENTS.md 및 두 repo AGENTS, git status, pipeline_jobs, ledger, aag brief, 최신 origin/main 확인. model_selector.py·chat_service.py 동시 작업과 겹치면 의존성/대기 설정. 본체 dirty 수집·덮어쓰기 금지.
2. 격리 worktree에 본 문서를 포함해 정책 resolver/API/테스트 구현, 다음으로 dashboard 연결·통합 검증. 같은 계약 의존이 있어 직렬 체크포인트로 실행한다.
3. 역할 산출물: Developer=resolver/DB/API/UI, QA=AC01~13 및 화면 근거, Reviewer=권한·과금·중복실행 차단 검수, SRE=릴리스 검증. 러너 기본 모델 배정을 따르고 모델을 별도 강제하지 않는다.
4. 리뷰를 통과한 선별 커밋만 반영. 운영 정책 before/after와 롤백 SQL/이전 revision 준비. 이번 채팅에서 운영 설정을 선반영하지 않는다.
5. push·배포는 승인된 Pipeline 단계에서 수행. API는 deploy.sh bluegreen, SHA당 image 1회 빌드, --no-build candidate/standby, candidate health 이후 짧은 nginx lock, routed health 실패 즉시 rollback, 동일 digest standby, DB fencing, 5분 P0/P1 관찰. 대시보드 변경은 해당 저장소 배포 규칙 준수.
6. 실패 시 기능 플래그/정책 revision 복원, 필요 시 새 revert 커밋과 라우팅 rollback. additive DB 필드는 보존하고 데이터 삭제 금지.
7. 최종 보고: PRD/코드/커밋/푸시/DB/배포/화면/실호출을 각각 판정하고 DB handover에 결과·SHA·테스트·미완료 기록. 구현 완료와 운영 반영을 구분한다.

## 10. 남은 리스크

- 제안 모델 그룹의 응답 품질·지연·비용 최적성은 미측정이다. 이름이나 tier만으로 우열을 확정하지 않는다.
- 운영 DB의 검증 플래그와 계정 실사용 가능 여부가 다를 수 있다. 후보별 backend·기능·권한을 착수 때 재검증한다.
- 기존 공통 llm 폴백 변경은 다른 서비스에 영향을 줄 수 있으므로 채팅 정책 범위를 분리하고 러너·백그라운드 설정 회귀를 검사한다.
- ledger와 Git 상태가 다르면 정리 완료로 단정하지 말고 변경 소유권을 확인한다. 충돌 해결 전 배포하지 않는다.
