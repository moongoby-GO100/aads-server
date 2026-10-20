# R-DOC 문서 저장 규칙 — 명세 (spec)

- version 1.5.0 · 작성 2026-10-03 KST · 갱신 2026-10-20 KST · 프로젝트 AADS (1.1.0 = 6~14항 화면 개발 절차 추가, 1.2.0 = 15항 document_key 규칙·기존 키 보존 추가, 1.3.0 = 4항 게이트 현황을 2026-10-14 실측으로 교체, 1.4.0 = 16항 일일 정본 등록률 계측 정의 추가, 1.5.0 = 16.1 최초추가 판정을 전체 이력 기준으로 고정하고 16.6 tenant fail-closed 추가. 1~15항 의미 불변)
- 짝 문서: 기획서 `docs/plans/20261003_AADS_RDOC_STORAGE_RULE_PLAN.md`(1.3.0), PRD `docs/prd/20261003_AADS_RDOC_STORAGE_RULE_PRD.md`(1.3.0)
- 상태: 초안. `rdoc-storage-rule-spec` revision 1 로 등록돼 있고 승인 0건이다 [DB 조회, 2026-10-14]. 파일명 규칙의 최종 결정은 CEO 가 한다. M1 의 design 은 이 spec 이 맡는 것으로 본다 [미검증, CEO 확인 필요].
- 출처 표기: [DB 조회] [코드 확인] [지시서 인용] [미측정]

## 1. 저장 위치

신규 정본 후보 문서의 위치. 기존 파일은 옮기지 않는다(비범위).

| kind | aads-server 경로 | 근거 |
|---|---|---|
| plan | `docs/plans/` | L1 초안 + 실제 디렉터리 존재 [코드 확인] |
| prd | `docs/prd/` | 같음 |
| design, architecture | `docs/design/` | 같음 |
| report (결과 보고) | `docs/reports/` | 같음 |
| spec, tasks | `docs/specs/<슬라이스>/` | L1 초안. 이 문서가 그 첫 사례다 |
| contract | `docs/contracts/` | L1 초안에는 없다. 디렉터리가 있고 커밋 훅 후보 경로에 들어 있어 제안한다 [코드 확인] |
| reference | 미정 | L1 초안에 없다. CEO 결정 필요 |

- 저장소 루트 `reports/` 와 `uploads/chat` 에는 신규 정본 문서를 두지 않는다(L1 초안 1항) [코드 확인: migrations/20261003_l1_doc_storage_rule_draft.sql].
- 다른 저장소: `aads-dashboard/docs` 와 `aads-docs` 는 현재 평탄(flat) 구조이고 `aads-docs` 에만 `plans/`·`contracts/` 가 있다 [코드 확인: 2026-10-03 `ls`]. 같은 kind 별 디렉터리 규칙을 적용할지는 CEO 결정이다. 그 밖의 저장소(KIS·GO100·SF·NTV2·NAS 등)의 현황은 [미측정].

## 2. 파일명 규칙 — FLOW 와 L1 초안 비교

| 항목 | FLOW (`.claude/rules/flow-rules.md`) | L1 초안 (`migrations/20261003_l1_doc_storage_rule_draft.sql`, origin/main 68f92907) |
|---|---|---|
| 형식 | 단계별로 다름: `{PROJECT}-FIND-{SEQ}_{제목}.md`, `{PROJECT}-LAYOUT-{SEQ}_{제목}.md`, `{PROJECT}-{SEQ}_{제목}.md`, `{PROJECT}-WRAP-{SEQ}_{제목}.md` | 하나로 통일: `YYYYMMDD_{PROJECT}_{제목}.md` |
| 순번 | `{SEQ}` 가 있고 발급 주체가 문서에 없다 | 없음. 날짜가 정렬 키 |
| 종류(kind) 구분 | 파일명 단계 토큰(FIND·LAYOUT·WRAP)으로 일부 구분 | 파일명이 아니라 저장 디렉터리와 정본 `kind` 로 구분 |
| 저장 위치 규정 | 없음 | 있음(1항) |
| 비ASCII 제목 | 제한 없음 | 제한 없음 |
| 활성 여부 | 현재 문서에 존재. 실제 준수는 신규 17건 중 2건 [지시서 인용] | `enabled=false` 초안. 어떤 세션에도 주입되지 않는다 [코드 확인] |

권장안 1개: **`YYYYMMDD_{PROJECT}_{TITLE}.md`, TITLE 은 ASCII 대문자·숫자·밑줄만, kind 는 TITLE 끝에 `_PLAN`·`_PRD` 처럼 둔다.** 예: `20261003_AADS_RDOC_STORAGE_RULE_PLAN.md`. FLOW 의 `{SEQ}` 는 발급 주체가 없어 쓰지 않고, kind 는 디렉터리와 정본 `kind` 가 맡는다. 최종 결정은 CEO 이며, 결정 전에는 이 권장안을 현재 동작으로 쓰지 않는다.

ASCII 권장 근거(한 줄): 2026-10-03 셸 러너가 한글 파일명 때문에 신규 파일의 intent-to-add 에 실패해 산출물이 두 번 사라졌다(runner-cb6d75c2, runner-b376bc76) [지시서 인용]. 그 결함은 별도 러너에서 고치는 중이라 [지시서 인용], 수정 뒤에도 ASCII 를 요구할지는 CEO 판단이다. 기존 한글 파일명은 개명하지 않는다.

## 3. 정본 등록 절차

대상 코드: `app/api/canonical_documents.py` (`/api/v1` 아래 `/projects/{project_key}/documents`) [코드 확인].

1. `POST /api/v1/projects/{project_key}/documents` 로 revision 을 만든다. 필수 입력: `document_key`, `kind`, `title`, `version`, `expected_generation`, 그리고 `content` 또는 `source_path` [코드 확인: RevisionInput].
2. 새 head 의 `document_key` 는 소문자 kebab `{주제}-{kind}` 로 한다. 슬래시·날짜·버전을 넣지 않는다. 예: `rdoc-storage-rule-plan`. 이는 이 프로젝트의 운영 규약이다. API 의 검증 정규식은 대소문자·`.`·`:`·`_` 도 받으므로 [코드 확인: KEY], 형식 점검은 호출하는 쪽의 몫이다. 정규식·기존 키 보존(grandfather)·파일명과의 구분은 15항에서 확정한다.
3. 프로젝트는 URL 의 `project_key` 로 구분한다. `kind` 는 `plan|prd|spec|design|architecture|contract|tasks|report|reference` 중 하나를 명시한다. 같은 키에 다른 kind 를 보내면 409 `document_kind_conflict` 이다 [코드 확인].
4. 갱신은 같은 `document_key` 로 새 revision 을 올린다(`expected_generation` 필요). 새 파일을 만들지 않는다.
5. 채팅 도구는 초안 등록까지만 한다. 승인은 기존 `POST …/{document_key}/approve` 경로이고, latest revision 만 승인할 수 있다 [코드 확인].
6. 목표가 있으면 `goal_id` 를 넣거나 `POST …/{document_key}/goals` 로 연결한다 [코드 확인].
7. 완료 보고에 `document_key` 를 적는다. 등록하지 못했으면 "정본 미등록"으로 분리 보고한다.

## 4. 게이트 현황

2026-10-14 에 코드·DB 를 다시 조회해 아래로 교체했다. 2026-10-03 서술(진입점 3곳 구현, 채팅 도구 예정, 표본 1행)은 더 이상 현재가 아니다.

- 단계: **off** — DB 쿼리 0건. **shadow** — 경고·기록만 하며 판정+기록 합산 300ms 상한이고, 모든 오류에서 fail-open 이다. 환경변수 `CANONICAL_GATE_MODE` 를 비워 두면 shadow 다 [코드 확인: get_mode]. 값 `enforce` 는 파싱만 되고 shadow 와 똑같이 통과시킨다: enforce 는 미구현이며 기록에 `enforce_not_implemented` 표시만 남는다 [코드 확인]. 구현·활성화는 shadow 실측 뒤 CEO 별도 승인이다.
- 진입점은 4곳이다. 목표 문서 API(`app/routers/goals.py`), 커밋 훅(`scripts/hooks/pre-commit` → `scripts/canonical_gate_commit.py`, `|| true`), 러너 제출(`app/api/pipeline_runner.py`)은 6fd5b531 에서 들어왔고, 채팅 도구 `canonical_document_register`·`canonical_document_lookup`(`app/services/canonical_document_tools.py`)은 9c2214d2 에서 들어왔다. 채팅 도구는 초안 등록까지만 하고 approve 를 부르지 않는다 [코드 확인].
- 커밋 훅의 정본 경고 블록은 저장소본 `scripts/hooks/pre-commit` 590행에 있다(`git blame`: 6fd5b531). 설치본 `/root/aads/aads-server/.git/hooks/pre-commit` 은 2026-09-29 18:19 판이라 이 블록이 없어서, 실제 커밋에서는 경고가 동작하지 않는다. 저장소본을 설치본으로 복사하는 동기화는 별도 조치다 [코드 확인: diff].
- 커밋 훅 후보 경로는 `^docs/(prd|plans|design|contracts)/` 이다 [코드 확인]. 따라서 `docs/specs/` 와 `docs/reports/` 는 후보 경로가 아니라서 훅이 경고하지 않는다. 이 범위를 넓힐지는 별도 작업·승인 사항이다.
- shadow 실측: 최근 7일 `canonical_gate_events` 153건 중 등록 확인(`ok`) 136건(88.9%), 미등록(`missing_canonical`) 17건(11.1%). 누적 206건, 모두 mode=shadow. 미등록 17건은 전부 `runner_submit` 이고 NTV2 8건·GO100 9건이다 [DB 조회, 2026-10-14]. 이벤트 수이며 문서 수가 아니므로 합격선·기간은 여전히 [미측정] 이다.
- 목업 승인 게이트(`app/services/mockup_gate_rules.py`)도 기본값이 shadow 이고 `MOCKUP_GATE_MODE` 는 설정돼 있지 않다. `mockup_review_*` 5개 테이블은 0행이다 [코드 확인] [DB 조회].
- 이 게이트는 지금 아무것도 막지 않는다. 문서나 훅이 "막는다"고 읽히면 그것은 현재 동작이 아니다.

## 5. 이 문서의 한계

- 파일명 권장안과 reference·contract 위치는 CEO 결정 전이다.
- 위 수치 중 [지시서 인용] 은 이 작업에서 다시 측정하지 않았다.
- 정본 등록·승인은 이 작업 범위 밖이며 커밋 뒤 CEO 세션이 직접 한다.
- 6~14항(화면 개발 절차)은 설계 계약이다. 현재 API·테이블이 그 필드와 판정을 모두 지원한다는 뜻이 아니며, 지원 범위는 13항에서 구분한다.

## 6. 화면 개발 절차 — 적용 범위 (RDOC-UI-1·2)

기능 단위 plan·prd·spec 에 아래 화면 절차를 연결한다. 이 항 이후(6~14)는 2026-10-03 CEO 범위 결정("R-DOC 기획·설계·PRD에 화면 절차를 반영")에 따른 추가이며 설계 계약이다 [지시서 인용: AADS-RDOC-UI-WORKFLOW-DESIGN-REFERENCES-20261003].

| 요구사항 유형 | 화면 명세 | 기록할 것 |
|---|---|---|
| 사용자 화면을 새로 만든다 | 필요 | 아래 필수 항목 5개 |
| 기존 화면의 화면·상호작용을 바꾼다 | 필요 | 같음. 기존 승인 디자인을 재사용하면 재사용한 revision 을 적는다 |
| backend-only (화면·상호작용 변화 없음) | 불필요 | "UI 불필요" 와 그 이유 한 줄을 PRD 에 남긴다 |

- 필수 항목 5개: 대상 사용자, 첫 진입(처음 열었을 때 무엇을 보고 무엇을 하는가), 반복 사용(매일 쓰는 흐름), 실패 복구(오류·권한 없음·세션 만료에서 어떻게 돌아오는가), 범위/비범위.
- 화면 명세는 기능 단위 plan·prd·spec 안의 절로 둔다. 독립적으로 바뀌거나 별도 승인이 필요한 경우에만 `docs/design/` 에 design 문서로 분리하고, 분리해도 원 문서에 `document_key` 로 연결한다.
- 분류(UI 필요/불필요/재사용)는 PM 이 하고, 이견은 승인권자가 정한다.

## 7. 절차와 책임 (RDOC-UI-3)

| 순서 | 담당 | 하는 일 | 남기는 것 |
|---|---|---|---|
| 1 | PM | 화면 필요성 분류 | UI 필요/불필요/재사용 + 사유 |
| 2 | PM | 사용자 흐름·PRD 초안 | 6항 필수 항목 5개, 요구사항 ID |
| 3 | 디자인 담당 | 최신 근거 조사 + 시안 | 9항 참조 기록, 시안(10항 패키지) |
| 4 | 아키텍트 | API·DB·권한·AAG 대조 | 대조 결과, 13항 후속 검수 조건 충족 여부 |
| 5 | 정본관리자 | 문서·시안 revision 연결 | `document_key`/`revision_id`, 시안 asset/version |
| 6 | 승인권자 | 실행 기준 확정 | approved revision 지정 |
| 7 | 개발자 | 고정된 기준대로 구현 | 구현 SHA, 변경 파일 |
| 8 | QA | 실제 브라우저 검수 | 11항 4축 판정, 증거 URL |
| 9 | 릴리스 담당 | 별도 승인 릴리스, 사후 검증 | 12항 |

- 작은 변경은 기존 승인 디자인을 재사용한다. 신규 화면이나 중대한 시각 변경에만 시안을 비교하고 하나를 고른다.
- 이 표의 담당은 역할이다. 이 문서는 새 담당자 계정을 만들지 않고 어떤 목표의 상태도 임의로 만들거나 완료로 바꾸지 않는다.
- 단계 6 이전에는 구현이 위 기준을 "확정된 기준"이라고 부르지 않는다. 초안은 초안으로만 표시한다.

## 8. 추적 필드 (RDOC-UI-4)

화면 작업 한 건을 요구사항에서 증거까지 잇는 설계 계약 필드다. 이 표는 현재 API·테이블이 모두 지원한다는 뜻이 아니다. "근거" 칸이 [코드 확인] 인 것만 이 작업에서 파일을 열어 확인했고, 나머지는 [미확인] 이며 아키텍트 대조(7항 단계 4)에서 지원 여부를 정한다.

| 필드 | 뜻 | 근거 |
|---|---|---|
| `goal_id` | 상위 목표 | 정본 API 가 `goal_id` 를 받는다 (3항 6번) |
| `milestone_id` | 마일스톤. 확인된 경우에만 적고, 없으면 비워 둔다 | [미확인] |
| `requirement_id` | 요구사항 ID (`RDOC-UI-n` 또는 기능 요구 ID) | 문서 안의 ID |
| `screen_id` / `route` | 대상 화면 | [코드 확인: design_context_pack_service.py `_screen_from_request_row` — `id`, `route`] |
| `document_key` / `revision_id` | 근거 문서와 revision | 정본 API (3항) |
| mockup asset / version | 시안 파일과 버전 | [미확인] 저장 위치 미정 |
| design-system version | 적용한 디자인 시스템 버전 | [미확인] |
| context-pack reference | 설계 컨텍스트 팩 참조 | [코드 확인: design_modifications.py 가 `design_context_packs` 를 조회] |
| task / job | 작업·러너 job ID | [미확인] |
| implementation SHA | 구현 커밋 | [미확인] |
| test / evidence URL | 테스트·화면 증거 | [미확인] |
| reviewer / verdict | 검수자와 판정 | [미확인] |

- **approved 와 latest 를 구분한다.** 구현과 QA 는 approved revision 만 기준으로 삼는다. latest 가 approved 와 다르면 "승인 대기 초안"으로 표시하고, 최신 초안이 실행 기준을 자동으로 교체하지 않는다.
- 필드가 비어 있으면 빈칸이 아니라 "미확인" 또는 "해당 없음"과 사유를 적는다.

## 9. 디자인 조사 계약 (RDOC-UI-5·6)

기능 착수 때와 주요 리디자인 때 한다. 공식 디자인 시스템과 동종 실제 서비스 사례를 둘 다 조사한다. 참조 하나마다 아래를 기록한다.

| 항목 | 기록 방법 |
|---|---|
| 원 URL | 요약본·2차 글이 아니라 원문 주소 |
| 발행/갱신일 | 페이지에 없으면 "시점 불명" |
| 확인일 | 실제로 연 날짜(KST). 발행일과 섞지 않는다 |
| 버전 | 가이드·디자인 시스템 버전. 없으면 "버전 불명" |
| 캡처 가능 여부 | 가능/불가/로그인 필요. 불가이면 대체 증거를 적는다 |
| 적용할 요소 | 무엇을 어떻게 가져오는가 |
| 제외 이유 | 가져오지 않는 요소와 이유 |
| 라이선스·출처 | 재사용 조건. 외부 이미지·자산을 그대로 복제하지 않고 구조와 패턴만 참고한다 |

- 최신 글만 모으지 않는다. 각 참조를 기존 브랜드, 사용자의 업무, 접근성, 성능에 비추어 비교하고, 최신이라는 사실은 채택 이유가 되지 않는다.
- 조사 갱신은 새 revision 제안으로 올린다. 승인된 디자인을 자동으로 덮어쓰지 않고, 갱신용 자동 스케줄도 만들지 않는다.
- 큰 신규 화면에는 두 안을 비교하고 권장안 하나를 이유와 함께 적는다: (가) 기존 체계 유지안, (나) 선별한 트렌드 적용안. 사소한 변경마다 두 시안을 만들라고 요구하지 않는다.
- "큰 신규 화면·중대한 시각 변경" 의 경계(예: 새 route 첫 화면, 내비게이션·레이아웃 구조, 디자인 토큰, 핵심 행동 배치 변경)는 제안이며 구체 기준은 CEO 결정 사항이다 [미측정].
- 이 절의 조사는 "업계 전체에서 가장 최신" 이라는 주장을 하지 않는다. 확인한 범위와 날짜만 적는다.

## 10. 설계 패키지 (RDOC-UI-7)

화면 명세에 들어가야 하는 것이다.

- 와이어프레임, 핵심 화면 시안, 이동·상태 전이 도식.
- 색·폰트·간격·모서리·모션 토큰.
- 재사용 컴포넌트 목록(신규/기존 구분).
- 반응형(모바일 포함)과 한국어 장문 줄바꿈.
- 상태 6종: loading, empty, error, permission(권한 없음), session-expired, offline.
- 첫 화면은 핵심 업무를 먼저 보이고 설정·권한은 보조로 둔다. 한 손 조작, 터치, 세션 복구를 명세에 포함한다.
- 검증 목표(예: 접근성 기준, 성능 예산)는 "명세(목표)" 로 표시하며 실측 성과로 쓰지 않는다. 성능 목표값은 기능별로 승인권자가 정하고 이 문서는 수치를 정하지 않는다 [미측정].

## 11. QA 판정 (RDOC-UI-8)

- 기능, 시각, 접근성, 성능을 각각 따로 판정한다. 값은 통과/불합격/평가불가 셋이다. 하나라도 평가불가이면 전체 통과로 쓰지 않는다.
- 접근성은 WCAG 2.2 AA 기준으로 본다: 키보드 조작, 포커스 표시와 가림 없음, 대비, 클릭 영역, 모션 감소 설정, 모바일 가로 넘침(overflow). 클릭 영역은 [목표 크기 최소](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum) 와 예외 조건을, 포커스는 [포커스 가림 없음(최소)](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum) 를 QA 시점에 원문과 다시 대조한다 [지시서 인용].
- 시각은 승인 시안과 같은 viewport·fixture 로 Playwright 캡처를 비교하고, 사람이 판단한다. 캡처 차이 허용 임계값은 [미정] 이다.
- 주요 행동과 복구 흐름(오류·권한 없음·세션 만료)을 실제로 실행해 본다.
- 채점기 장애나 인증 실패는 평가불가이며 디자인 불합격으로 쓰지 않는다.
- API 가 200 이라는 사실로 시각 검수 성공을 선언하지 않는다. 브라우저가 실패하면 HTTP, API health, 프로세스 확인 결과를 각각 따로 기록하고 시각 검수는 미완료로 둔다.
- 현재 운영 게이트와 이 절의 제안을 구분한다. 현재 것: 러너의 화면 검증 evidence 확인 [코드 확인: pipeline_runner_service.py `_require_screen_evidence`], 디자인 QA 채점 엔드포인트 [코드 확인: design_modifications.py `/modification-requests/{request_id}/score`]. 이 절의 4축 판정·평가불가 값은 제안이고 구현은 미확인이다. 기존 게이트를 우회하는 구현은 하지 않는다.

## 12. 릴리스 분리 (RDOC-UI-9)

- 문서 승인, 디자인 승인, 코드 검수, 배포 승인은 각각 별개다. 하나의 승인이 다른 것을 대신하지 않는다.
- 신규 route 는 가능하면 프리뷰·후보 환경에서 먼저 검증한다.
- 승인된 사후 검증 예외가 있으면 그 계약과 증거를 참조만 한다. 이 문서로 그 예외를 승인한 것으로 보지 않는다.
- 빌드·배포 절차의 원본은 `/root/aads/AGENTS.md` 이며 이 문서는 대체하지 않는다. 이 문서의 작업 자체에는 빌드·배포·재시작이 없다.

## 13. 현재 기반, 미구현, 후속 검수 조건 (RDOC-UI-10)

- 재사용할 기반 [코드 확인]: `app/api/design_modifications.py`(`/admin/design` 아래 수정 요청·컨텍스트 빌드·채점), `app/services/design_context_pack_service.py`(요청의 `screen`, `allowed_scope`, `acceptance_criteria` 를 컨텍스트 팩으로 모으고, 없으면 `missing_context` 로 알린다).
- 미구현(이 문서가 만들지 않는다): 8항 추적 필드 중 확인되지 않은 것의 저장, 시안 asset·버전 저장소, 디자인 시스템 버전 대장, 조사 갱신 제안 흐름, 11항 4축 판정의 저장.
- AAG: 2026-10-03 `aag_brief` 는 stale / not_proven, coverage=0 이므로 구조 정합을 통과했다는 근거로 쓸 수 없다 [지시서 인용]. 후속 검수 조건으로 최신 SHA 에 고정한 AAG 갱신과 코드 대조를 요구한다. 이 작업은 앱 코드와 AAG 스캐너를 수정하지 않는다.
- 아키텍트(b749ff17-43d3-4b87-b67f-482b1748b82b)에게 질문을 전달했다(relay_id=2f575da0-1613-40ba-9fe3-594d4c5f4fc4). 작성 시점에 회신은 받지 못했고, 회신 미수신을 합의 완료로 쓰지 않는다 [지시서 인용].
- 범위 밖: 실제 화면 구현, 디자인 자동 변경, L1 활성화, 정본 승인, enforce 구현과 활성화(enforce 는 미구현).

## 14. 외부 1차 근거와 OHVIS 제안 (RDOC-UI-11)

2026-10-03(KST)에 확인한 1차 자료이며 이 작업에서 다시 조회하지 않았다 [지시서 인용]. 업계 전체의 최신 순위를 주장하지 않는다.

| 자료 | 이 문서가 가져오는 것 | 가져오지 않는 것 |
|---|---|---|
| [Expressive Material Design (Google Research)](https://design.google/library/expressive-material-design-google-research) | 색·형태·크기·그룹화로 핵심 행동을 강조하고, 문맥과 익숙한 사용 패턴을 보존한다는 방향 | 이 자료의 성능 개선 수치를 당사 성과로 인용하지 않는다 |
| [Materials (Apple HIG)](https://developer.apple.com/design/human-interface-guidelines/materials) | Liquid Glass 사용 시 투명도·대비의 접근성을 따져야 한다는 점 | Apple 플랫폼 가이드를 OHVIS 웹의 필수 효과로 삼지 않는다 |
| WCAG 2.2 목표 크기(최소), 포커스 가림 없음(최소) | 11항 접근성 기준 | 없음 |

OHVIS 권장 방향(제안): 정보 가독성, 작업 상태, 핵심 행동이 뚜렷한 업무 화면을 우선한다. 장식·투명 효과는 접근성·성능 검증 뒤 제한해서 적용한다. 이 방향도 승인 전에는 제안이다.

## 15. document_key 규칙 확정과 기존 키 보존 (RDOC-NAMING)

2026-10-03 CEO 승인 범위("보류건 권장안으로 진행해")의 권장안을 적는다 [지시서 인용: AADS-RDOC-NAMING-COVERAGE-20261003]. 1~14항의 의미는 바꾸지 않는다. 측정표와 근거는 `reports/20261003_rdoc_naming_registration_coverage_RESULT.md` 에 있다. 이 항의 규칙은 문서 규약이며 어떤 API·훅도 지금 이 규칙을 검사하지 않는다(현재 검사는 15.4).

### 15.1 새 키 규칙

- 형식은 `{주제}-{kind}` 이다. 소문자 `a-z`·숫자·하이픈만 쓰고 128자 이하다. 검사 정규식: `^[a-z0-9]+(?:-[a-z0-9]+)*-(plan|prd|spec|design|architecture|contract|tasks|report|reference)$`.
- 끝 토큰은 그 head 의 `kind` 와 같다. 같은 키에 다른 kind 를 보내면 409 인 것은 3항 3번 그대로다.
- 넣지 않는다: 슬래시·역슬래시, 날짜(`YYYYMM`, `YYYYMMDD`, `YYYY-MM[-DD]`), 버전 토큰(`v1` 등), 대문자, 밑줄, `.`, `:`, 해시형 접미. 날짜와 버전은 revision 의 `created_at`·`version` 이 기록한다.
- 프로젝트 접두사는 `project_key` 가 맡는다. 주제어 자체가 제품명이면(`go100-…`) 써도 되고 의무는 아니다.
- 예: `rdoc-storage-rule-spec` 는 맞다. `20261003_AADS_RDOC_STORAGE_RULE_PLAN`(날짜·대문자·밑줄), `prd:848c81d57565`(콜론·kind 접미 없음), `go100-data-engine-optimization`(kind 접미 없음)은 새 키로는 맞지 않는다.
- 갱신은 같은 키의 새 revision 이다(3항 4번). 키를 바꿔 새 head 를 만드는 것은 갱신이 아니다.
- 이 규칙은 **새 head 를 만들 때만** 적용한다.

### 15.2 기존 키는 전부 인정한다 (grandfather)

- DB 에 이미 있는 모든 `(tenant, project_key, document_key)` 는 규칙에 맞지 않아도 그대로 유효하다. 이름 변경·이전(migrate)·재키잉·삭제·재연결을 하지 않는다. 승인 포인터(`approved_revision_id`)와 과거 revision 은 불변이다.
- 이유: 키는 목표 링크·이벤트·legacy 링크·승인 포인터의 참조점이다. 바꾸면 기존 참조와 승인본이 끊긴다.
- 기존 head 에 새 revision 을 올리는 것은 15.1 검사 대상이 아니다. 형식이 맞지 않는다는 이유로 갱신이 거절되지 않아야 한다.
- 예외 목록은 외운 숫자가 아니라 그때의 SELECT 로 산출한다. 과거에 쓰인 '9개' 같은 숫자를 다시 쓰지 않는다. 읽기 전용 쿼리:

```sql
SELECT tenant_id, project_key, document_key, kind
FROM project_document_heads
WHERE document_key !~ '^[a-z0-9]+(-[a-z0-9]+)*-(plan|prd|spec|design|architecture|contract|tasks|report|reference)$'
   OR document_key !~ ('-' || kind || '$')
   OR document_key ~ '(^|[^0-9])(19|20)[0-9]{2}-?(0[1-9]|1[0-2])'
ORDER BY 1, 2, 3;
```

- 2026-10-03 KST 실측: heads 13건 중 새 규칙에 맞는 것 0건, 예외 13건이다 [DB 조회]. 사유별 목록은 RESULT 의 표에 둔다. 이 숫자는 그날의 스냅샷이며 이후 head 가 늘면 위 쿼리로 다시 낸다.

### 15.3 파일명과 `document_key` 는 다른 규칙이다

| 항목 | 파일명 (2항, CEO 결정 전 권장안) | `document_key` (15.1) |
|---|---|---|
| 날짜 | 앞에 `YYYYMMDD_` 를 둔다 | 넣지 않는다 |
| 문자 | TITLE 은 ASCII 대문자·숫자·밑줄 | 소문자·숫자·하이픈 |
| 프로젝트 | `{PROJECT}` 토큰이 들어간다 | `project_key` 가 맡는다 |
| kind | TITLE 끝 `_PLAN`·`_PRD` | 끝 `-plan`·`-prd` |
| 갱신 | 새 파일을 만들지 않고 같은 문서를 고친다 | 같은 키의 새 revision |

- 변환 예(새 head): `20261003_AADS_RDOC_STORAGE_RULE_PLAN.md` → `rdoc-storage-rule-plan`. 날짜와 `AADS_` 를 빼고 소문자·하이픈으로 바꾼다.
- 이미 head 가 있는 문서는 변환하지 않고 그 키를 쓴다. R-DOC 기획서·PRD 는 `plan:7d9f483b5dba`·`prd:848c81d57565` 의 새 revision 이다. 이 두 키는 15.2 의 예외이고 바꾸지 않는다. 이 spec 은 아직 head 가 없어 새 키 `rdoc-storage-rule-spec` 이 후보이지만, 등록 여부는 승인 뒤 정본관리자가 정한다.
- 파일명 규칙의 최종 결정은 여전히 CEO 이며(2항), 이 항은 파일명을 바꾸지 않는다.

### 15.4 채팅 등록 도구의 키 검사와 이 spec 의 불일치

- 대조 대상: 채팅 정본 등록 도구 `validate_document_key` (runner-4fabf5b5 계열, 커밋 fafa85ff, 재작업 1ccf4f78 = runner-d869f5bc, 작업 상태 awaiting_approval). origin/main 에는 아직 없다 [git조회, DB조회: 2026-10-03]. 이 작업은 앱·도구·훅 파일을 고치지 않는다.
- 불일치 1: 이 도구는 소문자 kebab·슬래시·날짜·버전만 보고 끝 kind 토큰을 보지 않는다. 15.1 은 kind 접미를 요구하므로 `go100-data-engine-optimization` 같은 키가 통과한다 [코드 확인: 정규식 재현].
- 불일치 2: 이 도구는 head 를 조회하기 전에 키 형식을 검사한다. 15.2 는 기존 head 의 갱신을 거절하지 않는다고 하므로, 현재 예외 13건 중 10건에는 이 도구로 새 revision 을 올릴 수 없다. 정본 API 로는 올릴 수 있다 [코드 확인: 정규식 재현].
- 두 불일치의 후속은 도구 검수 담당(runner-d869f5bc 검수 세션)과 PM 세션 8bf0405a 에 전달하도록 RESULT 에 적었다. 테스트를 풀어 우회하지 않는다.

### 15.5 R-DOC head 의 등록 상태

- plan head `plan:7d9f483b5dba`·prd head `prd:848c81d57565` 는 각각 revision 1(1.0.0) 하나뿐이고, latest 와 approved 가 같은 revision 이다 [DB 조회, 2026-10-03]. 승인본 content 의 sha256 은 기획서·PRD 파일의 승인본 원문 앞부분과 일치한다(`tests/unit/test_rdoc_docs.py`).
- 이 개정(1.2.0 파일)의 draft revision 은 아직 올라가 있지 않다. 이 작업은 새 head·승인·L1 활성화를 하지 않는다. 후속 draft 등록은 이 변경의 검수·승인·push 뒤에, 정상 tenant 인증이 된 정본 API 로, 위 두 head 에만 한다. 인증이 안 되면 SQL 직접 쓰기나 토큰 추출 없이 미등록 사유와 담당을 기록한다.

### 15.6 정본 등록률 측정 규칙

- 분모는 서버·저장소별로 나눈다. 7일 창은 측정 시각(KST)의 시작·끝을 적어 고정한다.
- "신규" 는 파일 수정시각이 아니라 `git log --diff-filter=A` 로 센다. git 으로 확인할 수 없는 곳은 신규 여부를 [미측정] 으로 둔다.
- "등록" 은 파일 내용 sha256 이 `project_document_revisions.content_hash` 와 같을 때만 센다. 같은 title 만으로 등록을 확정하지 않는다. `goal_documents.doc_path` 경로 일치는 내용 일치가 아니므로 따로 센다.
- 수집하지 못했거나 접근할 수 없는 서버는 분모에서 빼지 않고 [미측정] 행으로 남긴다.
- 사본은 같은 sha256 을 하나로 세는 고유 수와 파일 수를 함께 적는다.
- 2026-10-03 측정 결과: 측정한 범위(aads-server, aads-dashboard, aads-docs, contabo14 의 kis-autotrade-v4)에서 고유 문서 35건 중 내용 일치 등록 3건이다. 미측정 범위가 있어 전체 등록률이 아니다 [git조회] [DB조회] [미측정]. 표는 RESULT 에 있다.

## 16. 일일 정본 등록률 계측 (RDOC-M5-METRIC)

15.6 의 측정 규칙을 날짜별로 반복 수집하는 정의다. 구현은 `app/services/rdoc_registration_metrics.py`, 수집기는 `scripts/rdoc_registration_collect.py` 이다. 이 항은 계측 정의일 뿐 경고를 차단으로 올리는 근거가 아니다 (enforce 승격은 미구현이며 이 항의 범위 밖이다).

### 16.1 지표 A — 신규 문서 등록률 (분모 = 문서)

- 키는 (tenant_id, project_key, KST 일자, 지표) 이다. 일자는 KST 달력일이고 UTC 경계(15:00)를 쓰지 않는다.
- 분모: 그날 `docs/reports/**/*.md` 로 새로 추가된 문서. "신규" 는 `git log --diff-filter=A` 의 **저장소 전체 이력에서 그 경로의 첫 추가** 시각이다. 수집 창(`--since`)은 출력 날짜만 자를 뿐 판정 기준이 아니다: 창 이전에 이미 있던 경로를 지우고 창 안에서 다시 추가해도 `readded_path` 로 빼고, 창 이전 문서와 같은 내용(같은 git blob)의 사본은 `duplicate_content` 로 뺀다. 그래서 같은 날짜는 하루 창으로 수집하든 여러 날 창으로 수집하든 같은 분모·제외 사유를 낸다. 얕은 clone 은 최초추가를 알 수 없어 수집을 실패시킨다. 이름만 바뀐 파일, 같은 경로의 재추가, 같은 sha256 사본, 빈 파일, md 가 아닌 파일은 분모에서 빼고 제외 사유별 개수를 행에 남긴다.
- 분자: 같은 tenant·project 의 `project_document_revisions` 에 `source_path` 와 `content_hash`(sha256) 가 모두 같은 문서. 경로만 같고 내용이 다른 문서는 분자에 넣지 않고 `path_only` 로 따로 센다. 다른 tenant 나 다른 project 의 등록은 세지 않는다.
- 프로젝트는 파일명의 두 번째 토큰이 알려진 project_key 일 때만 그 프로젝트로 귀속하고, 아니면 저장소 기본 프로젝트(AADS)로 본다.

### 16.2 지표 B — 게이트 ok 비율 (A 와 섞지 않는다)

- `canonical_gate_events` 의 verdict 가 ok 인 비율이다. 분모는 ok·missing_canonical·unknown 전부이다 (unknown 을 빼서 비율을 올리지 않는다).
- 이 테이블에는 tenant 열이 없어 project 단위로만 나뉜다. tenant 격리가 되지 않는다는 한계를 행의 detail(`tenant_scope`)에 적는다. B 는 내부 tenant 행에 저장되지만 그 tenant 의 개인 지표가 아니라 project 전체 게이트 이벤트의 비율이다. 다른 tenant 의 지표처럼 보고하지 않는다.
- commit 진입점은 DB 에 닿지 않는 hook 이라 판정이 항상 unknown 이다. 기록은 로컬 로그(`<git-common-dir>/canonical_gate.log`)에만 남으므로 이 테이블의 commit 행은 0건이 정상이다.
- B 는 A 를 대신하지 못하고 A 도 B 를 대신하지 못한다. 두 지표는 별도 행으로 저장한다.

### 16.3 분모 0, 누락 일자, 수집 품질

- 분모가 0 이면 비율은 `N/A`(저장값 NULL, status=not_applicable) 이다. 0% 도 100% 도 아니다.
- 수집기가 돌지 않아 행이 없는 날짜는 `not_collected` 이다. 0% 로도 성공으로도 채우지 않는다. 서비스가 꺼져 있었다는 사실이 "그날 신규 문서 0건" 으로 읽히면 안 된다.
- 각 행의 quality: `on_time`(대상일 종료 후 다음날 24시 안에 origin 을 fetch 해 확인하고 수집), `late_backfill`(그 뒤 수집), `partial_day`(대상일이 끝나기 전 수집), `unverified_source`(origin fetch 를 확인하지 못함). `on_time` 행은 늦은 재수집으로 덮어쓰지 않는다.
- 7일 연속은 `on_time` 행만 센다. 사이에 `not_collected`·`late_backfill`·`partial_day`·`unverified_source` 가 끼면 연속이 끊긴다.

### 16.4 M5 완료 조건과 현재 상태

- M5 는 실제 수집된 7일 연속 `on_time` 측정이 쌓여야 완료로 본다. 이 조건 없이 구현·테스트 통과만으로 완료를 주장하지 않는다.
- 구분 1 — 계측 미실행/첫 수집 준비: 코드·수집기·롤백 문서와 단위 테스트가 있고, 운영 테이블(`rdoc_registration_daily`)과 타이머가 설치되지 않은 상태. 저장된 측정 행은 0건이다.
- 구분 2 — 운영 수집 실행: 승인 후 테이블과 타이머를 설치하고 `collect --fetch --apply` 가 실제로 일자 행을 쌓은 상태. 이때부터 `report` 로 연속일수를 센다.
- 2026-10-20 현재는 구분 1 이다(재작업 후에도 같다: 운영 수집·테이블·타이머 미설치, 저장 행 0건, M5 미완료). 테이블 DDL 과 롤백은 `scripts/pending_migrations/`, 타이머 단위는 `scripts/pending_units/` 에 두었고 `migrations/` 자동 적용 경로에 넣지 않았다. 설치는 별도 승인 사항이다. 타이머는 `Persistent=false` 라 꺼져 있던 날을 몰아서 수집해 on_time 으로 위장하지 않는다.
- 롤백: 수집기를 멈추고(타이머 미설치면 해당 없음) `.down.sql` 로 인덱스·테이블을 제거한다. 코드 변경은 커밋 되돌리기로 끝나며 기존 정본 테이블은 건드리지 않는다.

### 16.5 pre-commit hook 경고 (M3 연결)

- hook 은 `docs/(prd|plans|design|contracts)/**` 와 `docs/reports/**/*.md` 를 스테이징해 커밋하면 경고하고 공용 git 디렉터리의 로그에 남긴다. 미등록 문서 때문에 커밋을 막지 않는다.
- 스크립트가 없거나 실행에 실패하면 말없이 넘기지 않고 노란 경고를 낸다. 그래도 커밋은 막지 않는다. `CANONICAL_GATE_MODE=off` 는 경고 없이 끈다.
- 검증은 임시 저장소의 실제 `git commit`(스테이징 fixture)이며 `tests/unit/test_canonical_gate_hook_staged.py` 가 맡는다. 운영 체크아웃의 `.git/hooks` 설치본 동기화는 별도 작업이다.

### 16.6 tenant fail-closed (수집·조회 공통)

- `collect` 와 `report` 는 DB 에서 신뢰 tenant(`public.aads_internal_tenant_id()`)를 읽고, 그 값이 있고 UUID 이며 `--tenant-id`(있다면)와 같을 때만 그 tenant 로 지표 SELECT·upsert 를 한다. `--tenant-id` 가 없으면 신뢰 tenant 를 쓴다.
- 신뢰 tenant 없음, 형식 오류, 요청과 불일치는 전부 실패(종료코드 1)이며 지표 SELECT 와 쓰기를 한 번도 하지 않는다. `--tenant-id` 의 UUID 형식 오류는 DB 연결 전에 거부한다.
- DB 연결의 읽기 전용 모드(`default_transaction_read_only`)는 어느 tenant 를 읽는지 막지 못한다. 읽기 전용은 쓰기 방지일 뿐 tenant 검증을 대신하지 않는다.
- 다른 tenant 의 수집·조회는 이 수집기의 범위 밖이다.
