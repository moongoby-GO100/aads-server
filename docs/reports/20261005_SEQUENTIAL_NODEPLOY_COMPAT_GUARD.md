# 순차 복구 승인 전 배포 금지 지시 호환 보강 (2026-10-05)

TASK_ID: AADS-SEQUENTIAL-NODEPLOY-COMPAT-GUARD-20261005 · 세션 8bf0405a-1f22-4ad9-bb09-6e0fce8c6339

> **이 보강은 5개 job 만 보호하는 임시 호환책이다. 구조화 PUSH_ONLY 실행본 반영 완료가 아니다.**
> 1단계 보고서(runner-68acf38e, 커밋 06fa604b)의 FAIL 판정은 그대로 보존한다. 비용 미측정.

## 1. 문제

실행본 `scripts/pipeline-runner.sh`(SHA256 `56080140393b00f2da644a7c05cd4148d139a1bc25f37da3cf777e1ad13de6cb`,
MainPID 4050729, 시작 2026-10-02 10:19, 파일 mtime 08:24 로 시작 이전)의 `instruction_forbids_deploy()` 는
구조화 `PUSH_ONLY` / `DEPLOY: false` 를 금지로 인식하지 못한다. 대상 5개 지시서는 변경 전 모두 **ALLOW**(배포 진행 가능)였다.
승인 시점의 `_deploy` 경로(pipeline-runner.sh:4890-4893)가 `SELECT instruction FROM pipeline_jobs` 로 DB 를 **재조회**하므로,
DB 지시서를 보강하면 실행본 수정·재시작 없이 게이트가 작동한다.

## 2. 대상(정확히 5개) — 변경 전후 동일 확인

| job | status/phase | depends_on | commit_hash | approved_at | hash 전 → 후 |
|---|---|---|---|---|---|
| runner-68acf38e | awaiting_approval | (없음) | 06fa604b89313b2a3477e7cd840bac4a5295e627 | NULL | ae6e900242029e29 → 0e4448214f70fb22 |
| runner-a0be8138 | queued | runner-68acf38e | NULL | NULL | ae83db2720960158 → bb61cae2dfea3fb8 |
| runner-4e443ba6 | queued | runner-a0be8138 | NULL | NULL | 02cf3176081d49e8 → 257faa6c29ec08c5 |
| runner-6c4be959 | queued | runner-4e443ba6 | NULL | NULL | 2fe0206840d7c061 → 305a6e19e03bfeb1 |
| runner-9a050352 | queued | runner-6c4be959 | NULL | NULL | 224199d08dac91b3 → 0c84a7af53c8bdd6 |

tenant_id `2d701a8c-9596-4757-8588-faa4f7837112`, chat_session_id 위 세션 — 변경 없음. 어떤 job 도 승인·반려·취소하지 않았다.
A~E 의존성 사슬과 승인 순서는 그대로다.

## 3. 변경 내용

- 지시서 끝에 `\n\n` + 아래 문구만 추가(멱등: 이미 있으면 건너뜀).
  `[CEO 기존 제약 호환 보강] 배포 금지. 빌드 금지. 재시작 금지. push 까지만 수행. DO NOT DEPLOY. 기존 승인 범위와 순차 의존성을 유지한다.`
- `instruction_hash` 는 시스템 정본 규칙 `sha256(f"{project}:{instruction}")[:16]` (`app/api/pipeline_runner.py:_compute_instruction_hash`)로 재계산.
  활성 job 유니크 인덱스(`uq_pipeline_jobs_active_instruction_hash_scope`) 충돌 없음을 사전 확인.
- 지원되는 지시서 갱신 API 는 없어서(`pipeline_runner.py` 에 instruction 수정 엔드포인트 없음) 단일 트랜잭션 + CAS 로 처리:
  `SELECT … FOR UPDATE` 후 `WHERE job_id AND instruction AND instruction_hash AND status AND phase AND depends_on AND commit_hash AND tenant_id AND chat_session_id AND approved_at IS NULL`
  이 5행 모두 `rowcount==1` 이어야 커밋. 하나라도 어긋나면 전체 롤백.
- `logs`(JSON 배열)는 기존 형식 `{ts,event,...}` 그대로 1건 추가: `instruction_compat_guard_added`(reason, original_approval, hash_before, hash_after, instruction_sha256_before).
  `status`, `phase`, `depends_on`, `commit_hash`, `approved_at`, `tenant_id`, `chat_session_id`, `updated_at` 는 건드리지 않았다.
- 감사: `pipeline_runner_events` 에 job 별 `event_type='instruction_compat_guard_added'` 5건. metadata 에 **원문 instruction 전체**, 전/후 hash, 롤백 SQL 템플릿 포함.

## 4. 검증 증거

1. 변경 전 fingerprint 대조: 실행본 SHA256 이 위 값과 일치, 추출 함수 본문이 실행본과 바이트 동일(함수 SHA256 `c42ca13406d86a32fc08ca6ec22b95752ac57a6c56a51af9c4a7b586a9f1ad78`).
2. 전체 스크립트를 source 하지 않고 `instruction_forbids_deploy` 함수만 추출한 stub 로 검사 — 서비스·배포·push 호출 없음.
   - 변경 전(메모리상 DB 원문): 5개 **전부 ALLOW**
   - 변경 후(메모리상, 커밋 전): 5개 **전부 FORBID**
3. 커밋 후 DB 재조회 + 동일 함수: 5개 **전부 FORBID**, status/phase/depends_on/commit_hash/approved_at 동일, 저장 hash 가 정본 규칙과 일치, 원문 + 추가문구 구조 확인.
4. 멱등 재실행: 5개 모두 "already has addendum" 로 건너뛰고 변경 0건.
5. 변경 후에도 실행본 SHA256·MainPID 동일(재시작 없음). 병행 job(runner-c026084c running, runner-a166f647 deploying 등) 상태 불변.

## 5. 롤백

원문은 `pipeline_runner_events.metadata.original_instruction` 에 있다(로컬 사본 `/tmp/aads-nodeploy-20261005/before.json`).
job 별로:

```sql
UPDATE pipeline_jobs SET instruction = <original_instruction>, instruction_hash = <hash_before>
 WHERE job_id = <job_id> AND instruction_hash = <hash_after>;
```

롤백 시 해당 job 의 구조화 PUSH_ONLY 는 다시 ALLOW 판정이 되므로, 실행본 반영 전에는 롤백하지 말 것.

## 6. 한계 / 후속

- 이 보강은 임시 호환책이다. `PUSH_ONLY`/`DEPLOY: false` 구조화 인식은 실행본 수정·반영(별도 승인)이 필요하며 이 작업 범위 밖이다.
- 이 작업 중 `apply.py` 의 멱등 재실행이 로컬 백업 `before.json` 을 빈 `{}` 로 덮어쓰는 결함이 있었다(스크립트 결함, 운영 데이터 영향 없음).
  원문은 감사 이벤트에서 복원해 `before.json` 을 재생성했다.
- handover_write: AADS/verification/`entry_key=aads-sequential-nodeploy-compat-20261005` 기록 완료(revision 5 — 동일 키가 이미 있어 갱신됨).
  공용 `HANDOVER.md`(공유 checkout, 조회 시 dirty 아님)는 지정 파일 범위 밖이라 수정하지 않았다 — 파일 동기화는 **미완료**.
- 원 세션이 이 보고서와 위 증거를 검토한 뒤 runner-68acf38e 를 승인할 수 있다. 승인은 이 작업이 대신하지 않는다.
- 서비스 재시작, 전역 자동승인 enable, 앱 빌드·배포: 0회.
