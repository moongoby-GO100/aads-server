# AADS 채팅 연속성 통합 지시서 — 검토본

상태: 작성·검토 완료, 담당자 미전송, 러너 미제출. 생성 도구의 AADS-NEW는 임시 표기이며 정식 작업 ID가 아니다. 전달/제출 시 실제 ID를 기록한다. 자동 생성된 제목·모델 고정값은 검토 과정에서 아래 내용으로 정정했다.

>>>DIRECTIVE_START
TASK_ID: 미발급 (실제 제출 시 시스템 발급)
TITLE: AADS 러너 후속업무·추가지시·배포 중 채팅 연속성 통합 보강
PROJECT: AADS
PRIORITY: P0-CRITICAL
SIZE: L (총괄; 실행은 S/M 단위로 분할)
MODEL: 프로젝트 기본 라우팅 준수, 임의 고정 금지
담당: AADS-채팅창 관리자
담당 세션: https://aads.newtalk.kr/chat#ac5278a7-2f13-4cd7-9aa1-83d41fb23c97

DESCRIPTION:
[CEO 의도 및 완료 계약]
러너 상태가 화면에 표시되는 것에 더해, 지시한 원 채팅 AI가 결과를 인지·검증하고 기존 승인 범위의 다음 업무를 자동 진행해야 한다. CEO는 AI 응답 중에도 추가지시를 보낼 수 있고, AI는 진행 중 목적과 산출물을 보존하며 다음 안전한 처리 지점에서 이를 반영해야 한다. 응답 종료 상태에서는 새 지시에 즉시 실행을 시작해야 한다. 배포·네트워크 단절에도 지시와 결과를 잃거나 업무를 중복 실행하지 않아야 한다.
이 문서는 실행용 검토본이다. 작성·저장은 담당 채팅 전송이나 러너 제출과 다르다. 전달 후에는 CEO가 이미 요청한 본 범위 구현·검증을 중기/장기라는 명목으로 미루거나 매 단계 재승인 요청하지 말 것. 새 범위·파괴적 조치·승인되지 않은 과금만 분리 보고.

[사전 확인 근거 — 운영 정상 확정 아님]
대상 세션 최신 assistant 메시지 2bbcf741-f914-4bc7-8460-7fd60b51ae3d를 재조회하여 요구사항별 대조표 작성.
- chat_service.py: Fix 3은 runner 문구 감지 후 running/retrying 실행 ID를 반환한다. 이 분기만으로 전체 AI 반응 억제를 단정하지 말고 send_message_stream 및 producer 경로까지 추적.
- trigger_ai_reaction, chat_deferred_reactions, 메모리 큐가 이미 존재한다. 큐 만료·용량 초과 삭제, 재시작 유실, 실제 실행 성공 전 완료 처리 가능성을 확인.
- AADS_EXECUTION_RESUME_MAX_ATTEMPTS 기본값은 8이며 실제 모델 재개 호출 직전 비용을 차감하는 _claim_resume_model_attempt가 존재. 환경 override와 운영값 재확인.
- deploy.sh에 기존 drain·standby 동기화가 있다. 기본 최대대기 600초 경로를 60초 강제종료로 축소하지 말 것. drain 조회 실패를 0으로 취급하는 분기와 standby deferred에도 release certified로 취급하는 분기를 전역 AGENTS 계약에 맞게 검토.
- app/routers/chat.py, redis_stream.py, stream_worker.py 및 dashboard/src/hooks/useChatSSE.ts에 stream-resume/Last-Event-ID 구현이 있다. 기존 구현을 검증·보완하고 중복 구축 금지.
- 이전 보고의 26.9%→7.1%는 비교기간·모수부터 재조회. '완료 6, 중단 1' 표와 7.1% 표기의 분모가 불명확하므로 개선 효과 확정 금지. 0ms 다운타임·0초 지연·66% 절감도 실측 없으면 사용 금지.

[0. 착수·충돌 방지]
/root/aads/AGENTS.md 및 각 repo 규칙, preflight, git status, 활성 pipeline_jobs, workspace ledger와 동일 파일 변경을 확인.
읽기 전용 진단 → 요구사항별 정상/부분/미구현/미검증 및 재현근거 → 구체적인 변경·테스트·롤백 계획을 첫 보고.
pipeline_runner_submit_batch 또는 Runner 의존성 그래프로 분할. 독립 QA/프론트/릴레이 분석은 병렬, chat_service.py·deploy.sh 동시 수정은 worktree와 depends_on으로 직렬화. 실 job_id만 기록.

[1. P0: 러너 이벤트→담당 AI 후속업무]
1) tenant/project/origin_session/job_id/event_id/status_version으로 이벤트를 구조화. 본문 문자열만으로 CEO 지시와 시스템 알림을 구별하지 말 것. 해당 세션에 러너 알림을 인용한 CEO의 실제 지시가 필터에 삼켜지지 않게 한다.
2) 시작·진행·완료·실패·승인대기·취소를 작업패널/별도 화면에 표시. 진행 heartbeat마다 LLM을 호출하지 않고 의미 있는 상태 전이와 CEO 추가지시에 반응한다.
3) 완료·실패·승인대기 이벤트는 영속 큐에 저장하고 수신/대기/AI검토중/처리완료/오류를 추적. 완료면 결과검증 및 다음 의존작업 진행, 실패면 진단·허용 복구, 승인대기면 구체적 승인항목 보고. 러너 done이나 리뷰 통과를 push/deploy 성공으로 대체하지 않는다.
4) 중복·역순 전달·서버 재시작·슬롯 전환에도 멱등 소비. 실행 중에는 현재 응답을 끊지 않고 안전한 체크포인트에서 반영 또는 영속 후속작업으로 이어간다. 종료 상태에서는 페이지가 닫혀 있어도 자동 소비한다.
5) 승인·원 지시 범위를 이벤트가 확장할 수 없게 한다. 동일 실패 무한 재제출과 자기알림 재귀는 correlation/causation ID 및 예산으로 차단하되 승인된 다음 업무까지 일괄 금지하지 않는다.

[2. P0: CEO 추가지시]
응답 중 입력 가능 → 저장 확인 → 반영대기/반영완료 표시. 현재 목표·부분응답·완료한 도구 결과를 보존하고 다음 모델/도구 경계에서 최신 지시를 적용. 실시간 모델 호출 도중 주입이 불가능하면 체크포인트 후 이어서 실행하고 반영 지연을 표시. 전체 응답 종료까지 무조건 기다리지 말 것.
응답 종료 시 다음 폴링/cron 대기를 강제하지 않고 새 실행 시작. 취소·중지·목표 대체는 추가지시와 구분. 연속 입력·다중탭·모바일 재접속에도 누락/중복/세션 혼입 방지.
사용자 중지와 승인 철회는 최우선이며 자동반응이 다시 시작시키지 않도록 한다.

[3. P0: Blue/Green 연속성]
60초 grace는 자연 종료 기회이지 모델 추론·TCP 연결 이전 보장이 아니다.
후보 건강 확인 후 신규 요청은 새 슬롯으로 보내고 기존 SSE/producer는 이전 슬롯에서 drain. 전환 전 대기가 필요하면 새 유입과 분리하여 무한대기 방지. 60초 초과 시 강제 kill 금지; 기존 처리 유지 또는 검증된 checkpoint+lease handoff+event replay 사용.
DB owner_instance+owner_epoch로 단일 실행 소유, 모델/도구 대기 중 heartbeat, inactive 슬롯은 새 복구·자동반응을 시작하지 않되 보유 작업은 안전하게 drain.
네트워크 재연결(event replay)과 모델 실행 재개(checkpoint)를 별도로 검증. 부작용 도구는 idempotency key와 실행원장으로 재실행 방지; 외부 성공 후 내부 저장 전 crash도 테스트.
배포는 deploy.sh bluegreen: clean committed worktree, release SHA당 image 1회 build, 양 슬롯 --no-build, 후보 health 후 짧은 nginx lock, routed-health 실패 즉시 rollback, drain 완료 후 same digest standby 동기화.
standby 동기화 미완·drain 확인 실패·digest 불명은 cutover 상태와 release certified를 구분하고 완료 금지. 필수 검증 및 5분 P0/P1 관측 후 완료 보고. reload-api.sh 모듈 reload·활성 API 직접 restart를 대체 배포로 사용 금지.

[4. P1: 재개 상한 8→3 및 안전한 zombie 복구]
기본 및 실제 운영 effective resume_max_attempts를 3으로 일치시킨다. 초기 호출과 재개 3회의 관계, provider 내부 재시도·폴백 총 호출/시간 상한을 명시하고 테스트. 폴백 모델 개수만으로 3회면 충분하다고 주장하지 말 것.
스캔/lease claim/relay 슬롯대기/placeholder 경합은 재개 예산을 쓰지 않는다. 사용 가능한 폴백 모델을 실제 다음 호출에 적용하며 전체 chain 소진 시 부분응답·실패 원인·재개 방법을 보존한다. 사용자 명시 모델 정책과 인증 경로 준수.
watchdog는 heartbeat, lease, 실제 도구·모델 진행, timeout을 함께 확인. 로그가 없거나 10분 경과했다는 이유만으로 정상 작업을 zombie로 종료하지 않는다. 기존 감시와 중복 cron 금지.

[5. P1/P2: 중기·장기 항목도 동일 작업계획으로 연속 구현]
A. producer 내 모델 폴백: 첫 토큰 전 실패와 부분응답 후 실패를 분리. 이미 출력한 응답/도구 실행을 덮어쓰거나 중복하지 않는다. 지연 0초 주장 금지.
B. 운영 패널: 세션/모델별 중단·복구·이벤트 처리상태, 마지막 오류, 승인 필요, 재시도, 원채팅 이동. 최초/반복/실패복구/모바일 경로 검수. 접수→반영/완료이벤트→AI검토 지연을 계측.
C. Codex Relay: 503/timeout의 포화·queue·pool·연결수명·동시성 원인을 먼저 측정해 보강. 무조건 pool 확대 금지. 인증정보 노출·변경 금지, 기존 중앙 LLM 인증/라우팅 정책 준수.
D. SSE 복구: event_id와 execution_id/sequence, Redis 보존 및 만료 시 snapshot, 브라우저 offset 보존, 재생 중복 제거, 세션/tenant 권한을 검증.
E. 실행 FSM: 기존 상태 목록 및 허용 전이 정규화, 원자적 CAS/lease fencing, terminal 상태 되살림 방지, 진행 중 세션의 점진적 호환 migration 및 rollback.
F. durable inbox/outbox/후속실행 상태와 재시도 실패함·재처리, 추적 ID를 연결하여 채팅이 닫혀 있어도 운영자가 확인 가능하게 한다.

[검증·합격 조건]
각 조건을 요구사항 ID, 재현 명령, DB before/after, 로그, 화면 증거, pass/fail로 기록.
- 응답 진행/종료 × 러너 완료/실패/승인대기, 중복·역순 이벤트, 잘못된 세션/tenant 전달: 누락·중복실행 없음.
- 모델 응답/도구대기 중 CEO 추가지시, 종료 후 새 지시, 연속입력, 명시 중지: 저장·반영·응답 보존 확인.
- SSE 60초 초과 중 배포, cutover 실패 rollback, old worker crash, 재연결, Redis 이벤트 만료: 메시지 및 승인 지시 손실 없음, 단일 실행 소유, 도구 부작용 중복 없음.
- 첫 토큰 timeout, 429/503, provider 내부 retry, lease 경합: 실 모델호출 기준 재개 상한 3, 올바른 fallback, 비호출 대기 예산 0.
- 실제 채팅/작업패널 브라우저 캡처와 PC/모바일 검증. 실패하면 HTTP→API health→프로세스 폴백과 미검증 범위를 명시하고 화면 검증완료로 간주하지 않는다.
지연의 p50/p95, 분모·기간·표본, 배포 SHA와 기준시각을 기록. 즉시성 수치 SLO는 첫 진단에서 근거와 함께 정하고 실제 측정 전 달성 보고 금지. 5분 release 관측과 후속 6시간 효과 검증을 구분하며 후속 검증의 실제 실행장치/담당/상태를 등록.

[CEO 확인 및 최종 산출물]
첫 보고: 요구사항 진행표·실측 현황·구체적 파일/영향/rollback·실행 의존성. 다음: P0 재현 및 화면→배포 인증→중장기 통합검증 순서로 같은 원채팅에서 상태 확인 가능.
CEO가 매번 '다음 진행'을 입력하지 않아도 기존 승인 범위는 계속 수행. 새 승인 필요 항목만 승인/수정/보류 가능한 구체안으로 제시.
최종: 요구사항별 완료/진행/미검증, 변경파일, 테스트 실제 결과, GitHub commit URL, push·운영 release SHA/digest·모니터링, HANDOVER.md와 정본 기록, 남은 작업·실측 비용($ 또는 미측정).
이번 지시와 무관한 dirty 파일을 현재 작업 미완료로 귀속하거나 검증 실패를 숨기는 완료 보정문 금지.
>>>DIRECTIVE_END
