# OHVIS Research Dataset v1 — Pilot Dataset Card

- **dataset_version**: `ohvis-pilot-v1`
- **생성 시각**: 2026-09-12 23:10 KST
- **생성 도구**: `research/ohvis_dataset_v1/extract_pilot.py` (SELECT 전용)
- **산출물**: `research/ohvis_dataset_v1/out/pilot_v1.jsonl`, `out/pii_scan_report.json`
- **상태**: 파일럿(30건). 본 데이터셋은 **연구 결과가 아니라 분석 대상 표본**이다.

## 1. 목적

AADS/OHVIS에서 실제로 수행된 **자율 AI 개발 사이클 1건**(CEO 지시 → 러너 실행 → AI 검수 →
승인/반려 → 커밋/배포)을 연구 분석 단위로 고정하고, 논문에서 사용할 변수·라벨·비식별 절차가
실제로 성립하는지 30건으로 선검증한다.

## 2. 모집단과 표본

| 항목 | 값 | 출처 |
|---|---|---|
| 원천 테이블 | `pipeline_jobs` | [DB 조회] |
| 전체 행 | 820 | [DB 조회 2026-09-12 23:02 KST] |
| 모집단(최근 120일) | 417 | [추출기 리포트] |
| 파일럿 표본 | 30 | [추출기 리포트] |
| 층화 기준 | `project` × `outcome` | 코드 `stratify()` |
| 추출 방식 | 층별 라운드로빈 + `sha256(job_id)` 정렬 → **결정론적 재현 가능** | 코드 |

### 층 분포(실측)

| 층 | 건수 |
|---|---|
| GO100/terminal_after_rejection | 4 |
| AADS/terminal_after_rejection | 4 |
| NTV2/terminal_after_rejection | 4 |
| KIS/terminal_after_rejection | 1 |
| GO100/failed | 4 |
| AADS/failed | 4 |
| AADS/in_flight | 3 |
| GO100/in_flight | 3 |
| GO100/accepted | 3 |

> SF 프로젝트는 120일 내 러너 작업이 2건뿐이라 층에 포함되지 않았다. v2에서 기간을 늘리거나
> SF를 별도 소표본으로 다뤄야 한다.

## 3. 레코드 스키마 (23 필드 그룹)

| 그룹 | 필드 | 설명 |
|---|---|---|
| 식별 | `case_id`, `session_pseudo`, `commit_pseudo`, `parallel_group` | salt+SHA-256 의사식별자. 원본 복원 불가(salt 미배포) |
| 맥락 | `project`, `declared_task_id`, `declared_priority`, `declared_size` | 지시서 선언값 |
| 모델 | `orchestrator_model`, `worker_model` | 라우팅 연구용 독립변수 |
| 검수 | `review_score`, `review_flag_category`, `review_needs_retry`, `max_cycles` | AI 자동검수 결과 |
| 지표 | `metrics.*` (instruction_len, git_diff_len, logs_len, changed_files_n, queue_wait_sec, work_sec, approval_wait_sec, total_lead_sec) | 종속변수 후보 |
| 시각 | `timestamps_kst.*` | created/started/completed/deployed |
| 라벨 | `labels.*` | 자동라벨 + 사람라벨 공란 |
| 텍스트 | `text.instruction_redacted`(≤1,200자), `text.review_feedback_redacted`(≤600자), `text.error_detail_redacted`(≤200자) | 비식별·절단 |
| 출처 | `provenance.*` | 원천 테이블, 추출기 경로, 추출 시각, 접근 모드 |

## 4. 라벨 정의

`labels.outcome` 4종 + 기타:

| 값 | 정의 | 파일럿 건수 |
|---|---|---|
| `accepted` | status ∈ {done, approved} | 3 |
| `terminal_after_rejection` | status = `rejected_done` | 13 |
| `failed` | status ∈ {error, cancelled} | 8 |
| `in_flight` | status ∈ {queued, running, awaiting_approval, review_hold} | 6 |

`labels.failure_mode`: none / timeout / auth_or_quota / vcs_conflict / worker_crash / build_or_test / other.

### ⚠️ 구성타당도 경고 — `rejected_done`

`rejected_done`은 코드에서 done·approved와 같은 **종료군**으로 집계되지만(`app/api/admin.py:51`,
`app/api/pipeline_runner.py:821`), 이름은 "반려 후 종료"를 뜻한다. 전체 820건 중 683건(83.3%)이
이 상태이므로, 이 한 라벨의 해석이 논문 결론 전체를 좌우한다.
따라서 자동 라벨로 확정하지 않고 해당 레코드에 `labels.needs_human_adjudication = true`를 부여해
**rater A/B 이중 판정 대상**으로 넘긴다. 이 판정 전에는 "성공률"류 수치를 산출하지 않는다.

## 5. 비식별(de-identification) 절차

18종 정규식 + 도메인 일반화를 적용한다: Anthropic OAuth/API 키, OpenAI 키, Google 키,
GitHub/Slack/Telegram 토큰, JWT, PRIVATE KEY 헤더, Bearer 헤더, URL userinfo, 이메일,
주민등록번호, 휴대폰, 계좌번호, 카드번호, IPv4, 홈 경로, Windows 사용자 경로, 조직 도메인/계정명.

식별자는 `salt|kind|value`의 SHA-256 앞 16자로 치환한다. **salt는 컨테이너 `/root/.ohvis_research_salt`
(0600)에 보관하며 데이터셋 디렉터리·git에 포함하지 않는다.** salt 없이는 재식별이 불가능하다.

### 파일럿 실측 결과

| 항목 | 값 |
|---|---|
| redaction 적중 | 도메인/계정 일반화 8건 (`aads.newtalk.kr` 2, `newtalk.kr` 3, `moongoby` 3) |
| 키·토큰·PII 적중 | 0건 |
| **출력 재스캔 잔존 유출** | **0건 (`leak_clean: true`)** |

재스캔은 추출기 내부에서 자동 수행되며, 1건이라도 탐지되면 프로세스가 `exit 2`로 실패해
산출물 배포를 차단한다.

## 6. 안전성

- DB 접근은 `SELECT` 1개 쿼리뿐이다. INSERT/UPDATE/DELETE/DDL 없음. 연결 직후
  `SET TRANSACTION READ ONLY`를 시도한다.
- 원문 `instruction`·`logs`·`git_diff` 전문은 반출하지 않는다. 길이(len)만 지표로 기록한다.
- 운영 서비스(API/Docker/nginx)를 변경하지 않는다.

## 7. 알려진 한계 (v1)

1. `rejected_done` 의미 미확정 → 성공률·품질 향상률 산출 불가 (§4 참조).
2. 사람 라벨 0건, Cohen's κ **미측정**.
3. 단일 조직·단일 CEO 사례 → 외적 타당도 제한. 일반화 주장 금지.
4. 모델 라우팅 비교는 관찰 데이터이므로 **인과 추론 불가**. A/B 또는 사전등록 실험 필요.
5. SF/KIS 표본 부족.
6. 커밋 도달 5/30, 배포 도달 0/30 → 배포 단계 종속변수는 v2에서 기간 확장 필요.

## 8. 재현 절차

```bash
docker exec aads-server python3 /app/research/ohvis_dataset_v1/extract_pilot.py \
    --n 30 --outdir /app/research/ohvis_dataset_v1/out
docker cp aads-server:/app/research/ohvis_dataset_v1/out/. \
    /root/aads/aads-server/research/ohvis_dataset_v1/out/
```

동일 salt·동일 DB 스냅샷이면 `case_id`와 표본 구성이 동일하게 재생성된다.

## 9. 배포 정책

- 현재 등급: **내부 전용(Internal)**. 외부 공개·논문 부록 첨부는 CEO 승인 + 윤리 검토 후.
- 외부 공개 시 추가 요구: 사람 라벨 완료, κ 보고, `rejected_done` 판정 확정, 표본 확대(n≥200),
  텍스트 필드 재검토(2차 수동 검수).
