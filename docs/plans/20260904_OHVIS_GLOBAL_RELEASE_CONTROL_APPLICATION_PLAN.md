# OHVIS Global Release Control 적용 기획서

- 작성 시각: 2026-09-04 06:13 KST
- 대상: OHVIS 배포 시스템, AADS Global Release Control, 고백(Go-Back/Rollback) 시스템
- 근거: `/root/aads/AGENTS.md`, `deploy.sh`, `/root/aads/aads-dashboard/deploy.sh`, `docker-compose.prod.yml`, `docs/operations/BLUEGREEN_RELEASE_GATES.md`, chat session `e29895a3-c27c-4d57-8478-4bdb9a0f75eb`

## 1. 요약

OHVIS에 반영할 핵심은 "커밋된 단일 릴리스 SHA를 양쪽 슬롯에 동일 이미지로 배포하고, 검증 실패 시 즉시 이전 active로 되돌리는 전역 릴리스 통제"이다.

AADS 운영 구현은 이미 API와 Dashboard 모두에서 다음 계약을 갖고 있다.

- clean release context에서 이미지 1회 빌드
- candidate 슬롯 `--no-build --no-deps` 시작
- candidate direct health 통과 후 nginx 전환
- routed/external health 실패 시 upstream 롤백
- 이전 active 슬롯 drain 후 같은 이미지 digest로 standby 동기화
- 5분 P0/P1 모니터링 후 release certification

OHVIS에 그대로 반영하면 배포 중단과 미반영 슬롯 문제는 크게 줄어든다. 다만 OHVIS는 사용자향 SaaS/외부 채팅/브라우저 자동화 흐름이 섞일 가능성이 높으므로, API/Dashboard뿐 아니라 Worker, WebSocket/SSE, PC/Browser Agent, 외부 채팅 게이트웨이까지 release 대상과 비대상을 명확히 나눠야 한다.

## 2. 현재 AADS 구현 판정

| 항목 | AADS 현재 상태 | OHVIS 반영 의미 |
|---|---|---|
| 전역 규칙 | `/root/aads/AGENTS.md`에 release contract 명시 | OHVIS도 repo 루트 운영 규칙으로 승격 필요 |
| API BG | `deploy.sh`가 기본 `bluegreen`이며 legacy 모드는 BG로 전환 | 직접 restart 경로 제거 가능 |
| Dashboard BG | `/root/aads/aads-dashboard/deploy.sh`가 Dashboard Blue/Green 수행 | 프론트 배포 중 화면 갱신 리스크를 통제 가능 |
| 단일 이미지 | API/Dashboard 모두 `AADS_RELEASE_SHA` 이미지 태그 사용 | 슬롯 간 버전 불일치 방지 |
| no-build 슬롯 시작 | candidate/standby 모두 `--no-build --no-deps` 게이트 존재 | standby 동기화 중 새 빌드 혼입 방지 |
| nginx 락 | build 중 락 미보유, 전환 구간만 shared lock 사용 | 다중 배포 대기시간 감소 |
| rollback | 전환 후 health 실패 시 upstream 복원 | 실패 릴리스 외부 노출 시간 최소화 |
| same-digest | active/standby image digest 비교 | 다음 전환 때 미반영 슬롯 active 방지 |
| 실행 소유권 | `owner_instance`, `owner_epoch` lease 컬럼 존재 | inactive 슬롯의 중복 복구/완료 처리 차단 |
| 5분 모니터링 | Dashboard는 post-cutover monitor 구현, API도 release gate 요구 | "전환 완료"와 "배포 인증" 분리 필요 |

확인된 런타임 상태 기준으로 API는 `aads-server`와 `aads-server-green`이 같은 image digest이고, Dashboard도 `aads-dashboard`와 `aads-dashboard-green`이 같은 image digest이다. 즉 AADS 본체는 "전환 후 standby 자동 동기화"의 핵심 조건을 충족하고 있다.

## 3. OHVIS 적용 목표

### 3.1 운영 목표

1. 배포 중 사용자의 진행 중 채팅, SSE, WebSocket, 브라우저 세션을 가능한 한 유지한다.
2. 새 릴리스가 candidate 슬롯에서 health/QA를 통과하기 전까지 active 슬롯을 건드리지 않는다.
3. 전환 직후 문제가 보이면 nginx upstream만 즉시 되돌려 이전 active로 복구한다.
4. 문제가 없으면 이전 active 슬롯을 같은 릴리스 이미지로 동기화해 다음 배포 때 미반영 상태가 나오지 않게 한다.
5. release 완료 보고는 cutover 직후가 아니라 external health, QA, 5분 P0/P1 모니터링 이후에만 한다.

### 3.2 사용자 경험 목표

| 사용자 흐름 | 목표 상태 | 실패 복구 |
|---|---|---|
| 채팅 응답 생성 중 | SSE 연결은 기존 active 슬롯에서 drain 완료까지 유지 | 끊겨도 Redis/DB stream buffer로 이어쓰기 |
| Dashboard 사용 중 | 새 정적 번들 로딩 후 세션 상태 자동 복구 | reload 후 현재 세션/작성중 입력 복원 |
| 외부 OHVIS 채팅 | 요청 idempotency와 execution lease로 중복 답변 방지 | lease 만료 후 active 슬롯만 복구 처리 |
| 관리자 배포 | 승인된 release SHA만 배포 | 실패 시 release queue에 failed/rollback 기록 |
| PC/Browser Agent 연동 | active API를 기준으로 명령 라우팅 | inactive 슬롯은 자동 반응 금지 |

## 4. 기대효과

| 효과 | 설명 | 검증 기준 |
|---|---|---|
| 무중단성 향상 | active를 건드리지 않고 candidate 검증 후 전환 | 배포 중 외부 `/health` 실패 0초 또는 계측값 기준 허용치 이내 |
| 미반영 슬롯 제거 | 전환 후 standby를 같은 이미지 digest로 동기화 | active/standby image digest 일치 |
| 롤백 시간 단축 | 코드 revert가 아니라 nginx upstream 복원 우선 | routed health 실패 후 이전 active health 정상 |
| 다중 배포 대기 감소 | build/install 중 nginx 락을 잡지 않고 전환 구간만 직렬화 | 락 보유 시간이 cutover 단계로 한정 |
| 책임 추적 강화 | owner/session/task/release SHA 단위로 dirty와 배포 기록 연결 | release queue, dirty ledger, deploy_history 대조 가능 |
| 채팅 중단 대응 강화 | execution lease와 stream buffer로 중복 완료/빈 응답 방지 | 중단 재개 테스트에서 최종 메시지 보존 |
| 운영 보고 품질 향상 | cutover와 certification을 분리 | "전환 완료"와 "배포 인증 완료" 상태 별도 표시 |

## 5. 예상 문제점과 보강안

| 우선순위 | 문제점 | 사용자 영향 | 개선안 | 완료 기준 |
|---|---|---|---|---|
| P0 | OHVIS 서비스별 BG 대상이 불명확하면 일부만 새 버전이 될 수 있음 | 다음 전환 때 예전 코드 노출 | API, Dashboard, Worker, SSE/WS, Agent gateway를 release topology로 문서화 | 모든 upstream/서비스가 BG 대상/비대상으로 분류 |
| P0 | dirty worktree에서 이미지가 빌드되면 재현 불가 릴리스가 됨 | 롤백/감사 불가 | clean worktree 또는 `git archive HEAD` release context 강제 | 빌드 전 `git status --porcelain` 차단 또는 isolated context 증빙 |
| P0 | standby 동기화가 재빌드 방식이면 active와 digest가 달라질 수 있음 | 다음 배포 때 미반영 슬롯 active | release SHA 이미지 1회 빌드 후 `--no-build`로 양쪽 슬롯 기동 | active/standby digest 동일 |
| P0 | 전환 후 오류가 늦게 드러나면 "배포 완료" 오판 | 장애 대응 지연 | cutover complete와 release certified 상태 분리 | 5분 P0/P1 모니터 통과 전 완료 금지 |
| P1 | Dashboard 배포 시 프론트 번들 교체로 SSE 재연결 지연 | 작성중 입력/스트리밍 표시 흔들림 | 입력 draft local persistence, session restore, stream resume endpoint | 새로고침 후 작성중/진행중 상태 복구 |
| P1 | Worker/스케줄러가 양 슬롯에서 동시에 실행 | 중복 작업, 중복 알림 | DB lease owner가 active slot인지 검증 후 mutation 허용 | inactive 슬롯 mutation 0건 |
| P1 | nginx lock을 build 동안 잡으면 다른 배포가 오래 대기 | 배포 큐 지연 | lock 범위를 upstream write + nginx reload + routed health로 제한 | lock audit에 긴 build 구간 미포함 |
| P2 | 운영자가 어떤 SHA가 실제 외부 서빙 중인지 보기 어려움 | 잘못된 승인/롤백 | Admin Release 화면에 active slot, SHA, digest, last health 표시 | UI/API에서 release 상태 확인 |
| P2 | 문서와 실제 스크립트가 어긋남 | 잘못된 수동 조치 | release contract verifier를 CI/pre-deploy에 연결 | 문서/스크립트 불일치 검출 |

## 6. OHVIS 목표 아키텍처

```text
Client
  |
  v
nginx / LB
  |-- OHVIS API upstream
  |     |-- blue  : active or standby
  |     `-- green : active or standby
  |
  |-- OHVIS Dashboard upstream
  |     |-- blue  : active or standby
  |     `-- green : active or standby
  |
  |-- OHVIS SSE/WS upstream
  |     `-- same active API slot, no upstream retry migration
  |
  `-- non-BG dependencies
        |-- PostgreSQL / Redis / object storage
        |-- external model gateways
        `-- browser/PC agent relay, if stateful
```

원칙은 "상태를 가진 의존 서비스는 BG 전환 대상이 아니라 안정 의존성으로 유지하고, 애플리케이션 코드가 올라간 서비스만 BG 슬롯화"이다.

## 7. 릴리스 절차

### 7.1 사전 단계

1. release candidate 생성: 변경 파일 owner/session/task_id 확인
2. hook 검증: secret, lint, syntax, migration dry-run, dirty gate
3. clean release context 생성: `git archive HEAD`
4. release queue 등록: `queued -> approved -> deploying`

### 7.2 배포 단계

1. 현재 active 슬롯을 nginx upstream과 marker로 판정
2. candidate 슬롯 컨테이너 제거/기동 준비
3. release SHA 이미지 1회 빌드
4. candidate 슬롯 `--no-build --no-deps` 기동
5. direct health, migration compatibility, smoke test 통과
6. nginx shared lock 획득
7. upstream 전환 + marker update + nginx reload
8. routed/external health 실패 시 즉시 rollback
9. lock 해제
10. 이전 active drain 대기
11. 같은 release image로 standby 동기화
12. active/standby digest 확인
13. QA + 5분 P0/P1 monitoring
14. release certified 기록

### 7.3 롤백 단계

롤백은 우선순위를 나눈다.

| 단계 | 방식 | 사용 조건 |
|---|---|---|
| R0 | nginx upstream 이전 active 복원 | cutover 직후 health 실패 |
| R1 | 이전 certified release SHA로 양 슬롯 재기동 | 새 릴리스가 늦게 장애 발생 |
| R2 | DB migration rollback 또는 forward-fix | schema/data 변경 영향 |
| R3 | 기능 플래그 kill switch | 특정 기능만 장애 |

## 8. 자동화 범위

| 자동화 항목 | 기능 | 우선순위 |
|---|---|---|
| release contract verifier | `--no-build`, same image tag, clean context, lock release 검증 | P0 |
| dirty ownership gate | 파일별 owner/session/task_id 없는 변경 배포 차단 | P0 |
| release queue | approved SHA만 배포, 상태 이력 저장 | P0 |
| cutover lock audit | nginx lock 획득/해제와 보유 시간 기록 | P0 |
| standby sync worker | drain 후 same digest standby 동기화 | P0 |
| certification monitor | 5분 health/log/error budget 모니터링 | P0 |
| Admin Release UI | SHA, active slot, standby digest, queue 상태 노출 | P1 |
| chat resume QA | 배포 중 SSE/작성중/최종응답 보존 E2E | P1 |

## 9. OHVIS 반영 로드맵

| 단계 | 기간 | 산출물 | 완료 기준 |
|---|---:|---|---|
| 1. 현황 인벤토리 | 0.5일 | 서비스/포트/upstream/worker/DB 의존성 목록 | BG 대상/비대상 확정 |
| 2. 배포 계약 이식 | 1일 | OHVIS `AGENTS.md`, deploy contract verifier, release queue | verifier PASS |
| 3. API BG 구현 | 1~2일 | API blue/green compose, deploy script | candidate health + rollback 테스트 통과 |
| 4. Dashboard BG 구현 | 1일 | Dashboard blue/green deploy | external `/login` health 통과 |
| 5. SSE/채팅 복구 | 1~2일 | stream buffer, execution lease, resume QA | 배포 중 진행 메시지 보존 |
| 6. 관측/관리 UI | 1일 | release status API/Admin 화면 | active/standby/SHA/digest 노출 |
| 7. 운영 인증 | 0.5일 | staging drill, rollback drill, runbook | 5분 P0/P1 모니터 통과 |

## 10. 검증 시나리오

| 시나리오 | 검증 방법 | 성공 기준 |
|---|---|---|
| 정상 API 배포 | active B 상태에서 G candidate 배포 | 외부 health 정상, active G, B same digest standby |
| candidate 실패 | G health 실패 유도 | nginx는 B 유지, 배포 failed 기록 |
| cutover 후 실패 | G routed health 실패 유도 | upstream B 복원, 사용자 health 정상 |
| SSE 진행 중 배포 | 긴 응답 생성 중 BG 실행 | 기존 stream 보존 또는 resume로 최종 메시지 보존 |
| Dashboard 배포 | 프론트 변경 후 BG 실행 | `/login`, `/chat`, `/ops` 화면/API 정상 |
| 동시 배포 | API와 Dashboard 배포 동시 요청 | build 병렬 가능, nginx cutover만 순차 |
| dirty 차단 | owner 없는 파일 생성 후 배포 | deploy blocked, 파일/owner 요구 메시지 표시 |
| standby 불일치 | standby 다른 image 강제 | certification fail, 관리자 알림 |

## 11. 의사결정 포인트

1. OHVIS에서 BG 대상에 Worker를 포함할지 결정해야 한다. Worker가 stateful이면 API와 별도 leader lease가 필요하다.
2. DB migration은 backward-compatible만 BG 자동 배포에 포함하고, destructive 변경은 별도 승인 게이트로 분리해야 한다.
3. Dashboard 정적 번들 변경으로 인한 화면 reload는 완전 무중단이 아니라 "상태 복구형 무중단"으로 정의해야 한다.
4. 외부 채팅/브라우저 자동화는 inactive slot mutation 금지 규칙을 강제해야 한다.

## 12. 결론

OHVIS에 AADS Global Release Control을 반영하면 배포 실패의 영향 범위를 "새 슬롯 내부" 또는 "nginx upstream 즉시 롤백 가능 구간"으로 제한할 수 있다. 가장 큰 효과는 미반영 standby 문제 제거, dirty worktree 배포 차단, 다중 배포 대기시간 감소, 채팅/SSE 복구력 강화이다.

단, 그대로 복사하면 부족하다. OHVIS는 서비스 topology가 다를 수 있으므로 먼저 BG 대상 인벤토리와 mutation owner lease를 확정해야 한다. 이후 API/Dashboard 배포 스크립트에 단일 release SHA, `--no-build`, same digest, short lock, 5분 certification을 강제하는 방식으로 적용하는 것이 안전하다.
