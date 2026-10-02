# OHVIS 문서 정본관리 게이트 — 운영 기획서

- 작성 2026-09-22 KST · 작성자: AADS CTO AI (문서 정본관리 게이트 담당)
- 관련 계약: [PRD](../prd/20260914_OHVIS_DOC_SYSTEM_PRD.md)(S1~S5), [PRD(운영판)](../prd/20260922_OHVIS_DOC_GOVERNANCE_OPS_PRD.md)

## 왜 이 문서가 지금 생기는가

CEO 지시("기획 설계 PRD 작성을 안 하지?")로 드러난 공백을 메운다. 이번 대화에서
정본 중복 3건 백필, git 발산 정리, index_docs.py 정책 결정, 문서표류 27건 러너
제출까지 **실행(Operate)만 하고 설계(Layout) 문서를 남기지 않았다** — 담당
역할이 "문서 정본관리 게이트"인데 그 게이트 자신의 작업 이력이 게이트에
등록되지 않은 자기지시적 공백이었다.

## 목표

1. 오비스 문서 정본 게이트(담당: 이 세션)의 운영 범위·권한·완료기준을 문서로
   고정하고 `goal_documents`에 정본으로 등록한다.
2. 이후 이 게이트를 다루는 모든 조치(백필·게이트 승격·정책 결정)는 이 목표
   아래 `goal_documents`에 변경요약(change_summary)으로 남긴다.

## 범위

- 정본 대장 무결성(document_key 충돌·중복 is_latest 방지)
- 문서 표류 게이트(S4) 운영 — 경고→차단 전환과 재발 방지
- Auto-RAG 색인 정책(S1-2) 예외 관리
- 이 세션이 CEO 승인 없이 결정할 수 있는 범위: 데이터 무결성 백필(가역적,
  트랜잭션 검증), 명백한 오류 diff 반려. CEO 결정이 필요한 범위: 게이트
  차단 승격의 최종 스위치, 정책(S1~S5) 조항 자체의 변경.

## 오늘 처리한 것 (근거)

| 조치 | 상태 | 근거 |
|---|---|---|
| document_key 충돌 3건(plan/prd/design.md) 백필 | 완료 | `db_safe_write` 6건, `is_latest` 중복 0행 재확인 |
| AADS git 발산(ahead1/behind3) 정리 | 완료 | `git stash`→`reset --hard origin/main`→`stash pop`, 다른 세션 WIP 보존 |
| index_docs.py `.html` 삭제 시도 반려 | 완료(결정) | PRESERVATION_HARD_GATE, PRD S1-2에 예외 명시로 정정(`eb7d16d5`) |
| 문서표류 27건 정정 + 게이트 승격 | 진행중 | `runner-f4cfbf95` |

## 리스크

- 게이트를 차단으로 올리기 전 표류 0건을 실측으로 확인하지 못하면 "거짓
  0건"이 커밋을 막는 상태로 고정된다(runner-afd8458d 반려 사유).
- 이 문서 자신도 `goal_documents`에 등록하지 않으면 같은 공백이 반복된다.

## R-DOC 문서 저장 규칙

> 개정 근거: 대상 head `plan:7d9f483b5dba` 는 "OHVIS 문서 정본관리 게이트 — 운영 기획서"(AADS)이고, 짝이 되는 PRD head `prd:848c81d57565` 의 담당 범위에 `goal_documents` 대장·문서 표류 게이트가 있다. R-DOC 는 그 직접 연장이므로 새 head 를 만들지 않고 같은 head 의 새 revision 으로 등록한다 [DB 조회: project_document_heads 679ce7d2…·2c4e95fb…, 2026-10-03].
> version 1.1.0 (직전 승인본 1.0.0 = revision 1). 위쪽 승인본 원문은 글자 그대로이며 sha256 `b0a54223627600eb18b549dd18cbf9627e4e8f31fca098f6fb4c7785bef750df` 이다 [DB 조회: project_document_revisions 6dfc2221… 의 content_hash].

### 문제

문서는 계속 만들어지는데 정본 대장에 오르지 않는다. 2026-10-03 07:31 KST 실측:

| 항목 | 값 | 출처 |
|---|---|---|
| 최근 7일 `docs`·`reports` 신규 `.md` | 17건 | [지시서 인용] |
| 그중 `goal_documents` 등록 | 1건 | [지시서 인용] |
| 그중 `project_document_heads` 등록 | 0건 | [지시서 인용] |
| 그중 FLOW 파일명 규칙 준수 | 2건 | [지시서 인용] |
| `canonical_gate_events` 누적 행 수(shadow 실측 표본) | 1행 | [DB 조회, 2026-10-03 작업 시점] |

- 정본 등록 경로(`POST /api/v1/projects/{project_key}/documents`)는 이미 있다 [코드 확인: app/api/canonical_documents.py:150]. 문서를 만드는 쪽(채팅 세션·러너·터미널·서브에이전트)이 그 경로를 부를 도구와 규칙이 없다는 것이 현재 가설이며, 원인 비율은 [미측정] 이다.
- 저장 위치와 파일명 규칙이 둘이다: FLOW(`.claude/rules/flow-rules.md`)는 `{PROJECT}-LAYOUT-{SEQ}_{제목}.md` 계열이고, 실제 파일은 `YYYYMMDD_…` 계열이 다수다 [코드 확인: docs/plans, docs/design 목록]. 비율은 위 표의 2건/17건 외에는 [미측정].

### 목표

1. 정본 후보 문서(plan·prd·spec·design·architecture·contract·tasks·report·reference)가 만들어지면 정본 head 에 초안으로 등록된다.
2. 저장 위치와 파일명 규칙을 하나로 정한다. 최종 결정은 CEO 이고, 권장안은 spec 에 둔다.
3. 갱신은 새 파일이 아니라 같은 `document_key` 의 새 revision 으로 올린다.
4. 등록률·이중 정본 수치는 PRD 의 수용 기준으로 측정한다. 목표값은 baseline 측정 뒤 CEO 가 정한다 [미측정].

### 대상

모든 채팅 세션, 러너(Pipeline-Runner), 터미널 작업, 서브에이전트. 전 서버에 같은 규칙을 적용하며 서버별 사본 규칙을 만들지 않는다. 서버 목록과 서버별 저장소 구성은 [미측정].

### 범위·비범위

| 구분 | 내용 |
|---|---|
| 범위 | 저장 위치·파일명 규칙, 정본 등록 절차, shadow 게이트 실측, L1 R-DOC 프롬프트 자산 활성화 판단 |
| 비범위 | 기존 파일의 일괄 이관·개명, 핸드오버(R-HANDOVER-DB 가 원본), 레거시 `goal_documents` 대량 백필, 정본 API 변경 |
| 비범위 | enforce 모드 — 미구현 · shadow 실측 뒤 CEO 별도 승인 |

### 로드맵

| 단계 | 내용 | 현재 상태 | 출처 |
|---|---|---|---|
| 1. 정본 등록 | 이 기획서(1.1.0)·PRD(1.1.0)·spec(1.0.0)을 커밋한 뒤 CEO 세션이 정본 API 로 등록한다. 기획서·PRD 는 위 head 의 새 revision | 이 작업은 파일까지. 등록은 미수행 | [지시서 인용] |
| 2. 채팅 등록 도구 | 채팅에서 초안 등록까지만 하는 도구. 승인은 기존 `approve` 경로 | runner-0d1b55b7 실행 중(선행 runner-a19f820e 는 error). origin/main 미반영 | [DB 조회: pipeline_jobs, 2026-10-03] |
| 3. shadow 실측 | 진입점 3곳이 경고·기록만 한다. 기간·합격선을 정한다 | 3곳 구현은 origin/main 커밋 6fd5b531 에 반영. 표본 1행이라 판단 근거로는 부족 | [코드 확인] [DB 조회] |
| 4. L1 R-DOC 활성화 | 프롬프트 자산 초안(`enabled=false`)을 활성화하고 `flow-rules.md` 파일명 절을 정리한다 | 초안만 있음(origin/main 68f92907). 활성화는 CEO 결정이고 spec 의 파일명 결정이 선행 | [코드 확인] |

enforce 는 로드맵 단계가 아니다: 미구현 · shadow 실측 뒤 CEO 별도 승인으로만 다룬다.

### CEO 결정이 필요한 것

- 파일명 규칙 최종안(spec 의 비교표 참조).
- L1 R-DOC 활성화 시점(4단계). 활성화하지 않으면 세션에 주입되지 않는다.
- shadow 실측 기간과 합격선 [미측정].
