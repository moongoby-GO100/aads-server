# R-DOC 문서 저장 규칙 — 명세 (spec)

- version 1.0.0 · 작성 2026-10-03 KST · 프로젝트 AADS
- 짝 문서: 기획서 `docs/plans/20261003_AADS_RDOC_STORAGE_RULE_PLAN.md`(1.1.0), PRD `docs/prd/20261003_AADS_RDOC_STORAGE_RULE_PRD.md`(1.1.0)
- 상태: 초안. 정본 등록 전이며, 파일명 규칙의 최종 결정은 CEO 가 한다.
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
2. `document_key` 는 소문자 kebab `{주제}-{kind}` 로 한다. 슬래시·날짜·버전을 넣지 않는다. 예: `rdoc-storage-rule-plan`. 이는 이 프로젝트의 운영 규약이다. API 의 검증 정규식은 대소문자·`.`·`:`·`_` 도 받으므로 [코드 확인: KEY], 형식 점검은 호출하는 쪽의 몫이다.
3. 프로젝트는 URL 의 `project_key` 로 구분한다. `kind` 는 `plan|prd|spec|design|architecture|contract|tasks|report|reference` 중 하나를 명시한다. 같은 키에 다른 kind 를 보내면 409 `document_kind_conflict` 이다 [코드 확인].
4. 갱신은 같은 `document_key` 로 새 revision 을 올린다(`expected_generation` 필요). 새 파일을 만들지 않는다.
5. 채팅 도구는 초안 등록까지만 한다. 승인은 기존 `POST …/{document_key}/approve` 경로이고, latest revision 만 승인할 수 있다 [코드 확인].
6. 목표가 있으면 `goal_id` 를 넣거나 `POST …/{document_key}/goals` 로 연결한다 [코드 확인].
7. 완료 보고에 `document_key` 를 적는다. 등록하지 못했으면 "정본 미등록"으로 분리 보고한다.

## 4. 게이트 현황

작업 시점(2026-10-03)에 `git ls-tree origin/main app/services/canonical_gate.py` 로 재확인했다: 파일이 origin/main 에 있다(blob fa65c05c…, origin/main eced41c2) [코드 확인].

- 단계: **off** — DB 쿼리 0건. **shadow** — 경고·기록만 하며 판정+기록 합산 300ms 상한이고, 모든 오류에서 fail-open 이다. 환경변수 `CANONICAL_GATE_MODE` 를 비워 두면 shadow 다 [코드 확인: get_mode]. 값 `enforce` 는 파싱만 되고 shadow 와 똑같이 통과시킨다: enforce 는 미구현이며 기록에 `enforce_not_implemented` 표시만 남는다 [코드 확인]. 구현·활성화는 shadow 실측 뒤 CEO 별도 승인이다.
- 진입점 3곳은 구현돼 origin/main 에 반영돼 있다: 목표 문서 API(`app/routers/goals.py`), 커밋 훅(`scripts/hooks/pre-commit` → `scripts/canonical_gate_commit.py`, `|| true`), 러너 제출(`app/api/pipeline_runner.py`). 커밋은 6fd5b531(runner-1816e2c0, 작업 상태 done) [코드 확인] [DB 조회]. 지시서의 97ac03c1 은 같은 러너 커밋의 rebase 전 SHA 이고 origin/main 의 조상이 아니다 [코드 확인: `git merge-base --is-ancestor`]. 지시서의 "origin/main 미반영" 서술은 작업 시점 실측과 달라 SHA 를 6fd5b531 로 갱신했다.
- 4번째 진입점(채팅 정본 등록 도구)은 예정이다. runner-a19f820e 는 error 로 끝났고 runner-0d1b55b7 로 재제출돼 실행 중이다. origin/main 에는 아직 없다 [DB 조회: pipeline_jobs, 2026-10-03].
- 커밋 훅 후보 경로는 `^docs/(prd|plans|design|contracts)/` 이다 [코드 확인]. 따라서 `docs/specs/` 와 `docs/reports/` 는 후보 경로가 아니라서 훅이 경고하지 않는다. 이 범위를 넓힐지는 별도 작업·승인 사항이다.
- shadow 실측 표본: `canonical_gate_events` 1행 [DB 조회, 2026-10-03]. 합격선·기간은 [미측정].
- 이 게이트는 지금 아무것도 막지 않는다. 문서나 훅이 "막는다"고 읽히면 그것은 현재 동작이 아니다.

## 5. 이 문서의 한계

- 파일명 권장안과 reference·contract 위치는 CEO 결정 전이다.
- 위 수치 중 [지시서 인용] 은 이 작업에서 다시 측정하지 않았다.
- 정본 등록·승인은 이 작업 범위 밖이며 커밋 뒤 CEO 세션이 직접 한다.
