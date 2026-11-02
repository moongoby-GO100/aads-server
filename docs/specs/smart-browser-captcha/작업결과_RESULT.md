# 스마트브라우저 캡챠 문서 검수 보완 작업 결과

- document_key: smart-browser-captcha-result
- source_task_id: runner-6fbe30cd
- 결과: 허용된 기존 7개 경로·key를 보존하여 로컬 보완 및 정적 검증 완료. 실제 정본·계층·레시피 등록은 원세션 인계 미완료다.
- 정본 미등록(미완료). 열람 링크 미제공. DB/사이트 성공으로 정적 통과를 승격하지 않는다.

## STEP 0 조사 및 분류

AAG의 mount·테이블 접점을 전제로 대상 구현을 먼저 확인했다. /root/aads/AGENTS.md와 저장소 AGENTS.md를 읽었다. 시작 git status는 clean, 허용 7개 경로는 현재 base에 없고 제공 original에 보존돼 있었다. 전체 cherry-pick·worktree 명령 없이 기존 격리 작업공간에 7개만 복원하여 개선했다. 관련 없는 dirty 덮어쓰기 없음. 로컬 HEAD와 origin/main은 d6152b7cf758a459772282ffd70868ac31484587로 일치했으나 fetch·live 최신 상태를 확인한 뜻은 아니다.

| 분류 | 대상 및 기존 접점 | 처리 |
|---|---|---|
| 유지 | app/routers/goals.py 모델 MilestoneCreateRequest/WorkItemCreateRequest/LinkTaskRequest, create_goal_work_item/get_goal_work_tree/add_milestone/link_task 및 목록/status/governance/board/목표상태 라우트 | 기존 /api/v1 mount·tenant/actor/feature gate·transaction 유지. goals/milestones/work_items/goal_task_links·정본 접점 읽기 참조, 호출·수정 없음 |
| 유지 | app/api/canonical_documents.py RevisionInput/provenance_problem/create_revision_in_tx/create_revision/_head 및 list/content/goal-link/승인 계열 | 현재 DTO·CAS·출처·기존 head/revisions/goal_links/events/grants/legacy_links 접점 유지. 승인·등록 호출 없음 |
| 유지 | work_recipe executor BrowserRecipeExecutor/execute_step/smart_browser_route, player RecipePlayer/play_recipe, login_contract normalize_login_contract/contract_route/adapt_browser_recipe | 현재 레인·재생·후조건 계약 조사. 자동 fallback을 새 문서의 고정 레인 로그인 성공으로 승격하지 않음 |
| 유지 | registration normalize_verify_steps/normalize_flow_metadata/normalize_piece_metadata/request_registration/decide_registration, dom_evidence issue/verify/binding_hash, credential_scope assert_credential_allowed, human_login LoginPolicyStore/HumanLoginExecutor, schema RecipeInput/RecipeStep/WorkRecipe | 기존 승인본 탐색·후보등록·사람승인·관측·scope·schema 재사용 경계. 큐/실행/정본·레시피 DB 계약 참조만; 실제 DB 조회 아님 |
| 수정 | 기존 3종 CAPTCHA 문서 | 같은 파일명·document_key 유지, rev2 출처·서버우선·조각 계약·PM-C1~C3·등록인계 보완 |
| 수정 | 업무계층_등록계획.json | 기존 C 5/10/20 ref/멱등키 보존, 구체 시험과 원래 M 13/13/13 계획, 실제 ID 보존, 정상 DTO payload 준비 |
| 수정 | validate_manifest.py check/main | 기존 19종 부정시험 보존. check_supplement/supplemental_mutations 추가, 합계 90종. 정적 검증 코드 변경 |
| 수정 | 등록실행_결과.json·작업결과_RESULT.md | 현재 작업 신원·실행 결과·한계로 갱신, 이전 transport 시도는 역사로 구분 |
| 신규 | 위 검증기의 보완 함수와 manifest 계약 필드 | 허용된 기존 파일 안에서만 추가. 제품 API/스케줄러/schema/운영코드 신규 없음 |
| 삭제 | 없음 | 삭제 대상·호출처 영향·삭제 롤백 없음. 보완 롤백은 원본7개와 차이를 검토해 이번 변경만 되돌리는 정상 후속 작업이며 운영 DB를 지우지 않음 |

REFERENCE_FILES의 app/services/smart_browser/는 현재 base에 없어 실제 app/services/work_recipe/를 확인했다. 책임 시험파일 중 현재 없는 것은 신규 예정으로 표시했다. 기존 work-engine 문서와 앱/pc_agent/HANDOVER 파일은 수정하지 않았다. 지정 외 파일 변경 사유: 해당 없음. 새 날짜 사본·새 문서 key 없음. 기존 7개만 변경했다.

## 변경 파일

다음은 파일명이며 정본 열람 참조가 아니다.

- docs/plans/20261102_AADS_스마트브라우저_CAPTCHA개선_기획서.md
- docs/prd/20261102_AADS_스마트브라우저_CAPTCHA개선_PRD.md
- docs/design/20261102_AADS_스마트브라우저_CAPTCHA개선_설계서.md
- docs/specs/smart-browser-captcha/업무계층_등록계획.json
- docs/specs/smart-browser-captcha/validate_manifest.py
- docs/specs/smart-browser-captcha/등록실행_결과.json
- docs/specs/smart-browser-captcha/작업결과_RESULT.md

## 검수 차단점 보완

상위 work-engine plan/prd/design/contract rev2 4종의 제공 원문 UTF-8 SHA256가 전부 일치했다. source-context 전체 SHA256=f2554430e5f670714b50b9892c64806b3f441700a8d2fb565dec24e87306aa34. 각 문서 ID/hash/revision/generation/status/approved=false를 3종 문서와 manifest에 반영했다. plan/prd/design은 review, contract는 draft이며 승인본이 아니다. 과거 rev1/None 참조는 historical_source_references에만 보존했다. contract 본문 앞부분의 역사와 끝부분 rev2 PM 후속 계약을 구별했다. v4 핸드오버 제공 원문도 읽었으며 실측 없는 1초·무중단·실제PG 완료는 주장하지 않는다.

일반 사이트 서버 Playwright 기본, PC는 실제 로컬세션/인증서/보안프로그램 필요성 확인시에만 선택한다. 같은 work_key의 lane/page/account/device/epoch 결속·실행중 자동전환 금지·새 장치 재인증을 명시했다. 서버/PC 증거와 성공/미실행은 각각 기록한다. 실제 사이트 계정일치·업무화면·CAPTCHA/오류/AccessDenied 부재가 필수이며 HTTP200과 HTTP/API/process fallback이 로그인 성공을 대신하지 못한다.

승인본 list 탐색 후 로그인진입/Vault입력/제출검증/조회/다운로드/집계 여섯 조각의 exact origin/domain, lane/work_key/page/account, 시작/성공, verify assertion, 선행 name/version/hash, screenshot/artifact/run, 복귀점, scope/secret변수, 등록대기/승인/재생 상태 계약을 준비했다. 실제 profile·증거 미확정 null은 제출금지다. password filled/empty만, 인증유지 재제출0, OTP/지원불가 사람해결후 동일작업 재개, 오류 실행 성공등록 금지를 명시했다.

C0~C4 각1 epic/2 story/4 task의 기존 key를 보존했다. 20 leaf 각각에 구체 입력·행동·기대결과·책임파일·신규예정 시험명·증거·복구를 적고 부모 기준에 집계했다. M1~M13은 실제 ID와 원래 description/criteria/status/owner/evidence/version을 그대로 보존해 각각 최소1 epic/story/task를 계획했다. completed인 M1/M6은 planned 근거재검증이며 기존 완료를 다시 주장하지 않는다. M8 역사 원문의 HANDOVER 파일 요구는 보존하되 실제 이행은 DB만 따른다.

공통 실행원장·fenced owner와 PM-C1 소유권, PM-C2 타입/복합키/기존records 대 신규feed, PM-C3 24h cache 대 비민감 tombstone·권한/충돌/unknown/결과소실 순서를 재사용한다. PM 시험6종은 개별 추적, 모두 not_run. CAPTCHA 자동대행 제외의 기존 경계 중 최신 CEO 제한 단순 화면조작만 추가 범위로 명시했다. 구현/검수/승인/운영 적용은 별개다.

## 실제 검증과 검수 식별자

검수 base SHA=d6152b7cf758a459772282ffd70868ac31484587. 아직 새 commit SHA는 없다. 보완 문서3종+manifest+검증기 5개 파일의 정렬 경로→SHA256 JSON에 대한 검수 bundle SHA256=6fedf15b210f479319d94634d76d70e2e7d1a1dd53f587a36263ea61d8f0d76b. 각 파일 해시는 등록실행_결과.json에 있다. 자체참조를 피하려고 실행결과/RESULT는 이 bundle에서 제외했다.

| 명령·검사 | 실제 결과 | 한계 |
|---|---|---|
| 제공 원문4종 SHA256 재계산 | 4/4 일치 | 원세션 조회 당시 snapshot, 현재 승인/운영상태 아님 |
| python3 docs/specs/smart-browser-captcha/validate_manifest.py | PASS; 정상 manifest 및 부정 fixture 90/90 거부 | 기존19종+이번71종, DB/API/사이트 실제시험 아님 |
| python3 -m compileall -q docs/specs/smart-browser-captcha/validate_manifest.py | exit 0 | 문법 검증 |
| git diff --check | exit 0 | 7개 파일은 이 base에서 untracked라 별도 전파일 공백검사 병행 |
| 원본7개 대비 git diff --no-index --check | 공백오류 출력 없음, 변경파일은 exit1/동일파일은 exit0 | no-index의 변경존재 상태이며 exit1을 일반 시험 통과로 쓰지 않음 |
| 7개 파일 trailing whitespace/EOF 별도 Python 검사 | PASS, 7개 모두 공백오류0·정상 EOF | untracked 파일도 모두 포함 |
| 시작/완료 git status 및 변경경로 대조 | 기존7개 이외 tracked 수정 없음 | live pipeline/ledger와 동일하지 않음 |

실제 로그인·레시피 승인/재생·서버/PC E2E·실제PG/GC/경쟁/멱등 재실행·정본 readback은 모두 not_run이다. 초기 정적 검사에서 PM 시험 ID의 축약 표기를 검출해 전체 ID로 고친 뒤 다시 통과했다. 추가 유료 도구 호출0, 비용은 미계측(null)이며 $0이라고 쓰지 않는다. 금지된 commit/push/build/deploy/restart/프로세스종료 명령은 실행하지 않았다.

## 운영 등록 인계 및 미완료

이번 작업의 정상 MCP/DB/ledger 도구 검색 결과 해당 도구 미제공이다. source snapshot으로 허용 로컬 보완을 끝냈고 네트워크·자격변수 없는 러너에서 운영 요청을 다시 시도하지 않았다. 이전 DNS/EPERM은 transport 실패이며 권한거절로 단정하지 않는다. 외부 unauth401을 통과로 적지 않았다. 실제 preflight/활성 job/ledger/동일파일 점유/원격 최신 git 재확인은 원세션 미완료다. 같은 파일 충돌 발견시 등록·통합 중단한다.

원세션 65f926cb-75c0-47e5-bc2d-b22310dbcd02이 보완판 검수 직후 현재 상태를 다시 읽고 정상 canonical_document_register로 3종 초안을 등록한다. payload에 실제 source_task_id=runner-6fbe30cd·10자 이상 change_summary·goal_id·content/hash가 있다. expected_generation은 live 조회 후 치환하며 null 템플릿 그대로 제출하지 않는다. 응답 revision/hash/goal link/content와 view/report_line을 재조회해야 정본 등록 완료다. 이번 쓰기0·실제 등록0, 전후 DB count·재실행 증가량 null이다.

M 실제 ID는 그대로, C/parent 참조는 검증된 API 응답 실제 ID로 치환한다. 임의 UUID 생성·blocked goal에 무관한 M 연결 금지. milestone API 멱등/원자 보장 없음 구간은 승인된 정상 중복방지 절차 확인 전 STOP, work-item은 개별 transaction/멱등키만 보장, 전체 배치/링크 원자성을 주장하지 않는다. DTO에 없는 completion/depends/책임·시험·복구는 description/criteria에 보존한다. feature flag·새 API·migration·SQL 쓰기로 우회하지 않는다.

D=runner-6fbe30cd, S=runner-83015ff6, I=runner-144faf81, E=runner-66611fed, batch=batch-9ce10925. 반려 D=runner-94f7eafb는 성공으로 연결하지 않는다. C0/D·C1/S·C2와C3/I·C4/E의 현재 job 상태·실제 C ID를 읽은 뒤 명시 link-task하고 재조회한다. 링크는 출처이지 성공 증거가 아니다. S/I/E 러너 중단·지시변경·자동 재작업 제출 없음.

핸드오버 DB 미기록. 정상 handover_write/POST 도구가 없어 entry_key=runner:runner-6fbe30cd의 DB 기록·재조회는 원세션 인계 미완료다. handover_db_write_failed는 로컬 결과에만 기록, 서버 이벤트 저장 주장은 없다. HANDOVER.md는 수정하지 않았다. 정본 미등록(미완료), 열람 링크 미제공.
