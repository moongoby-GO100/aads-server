-- 러너 작업 체크포인트: 러너 종료로 대기열에 돌아간 작업의 진행분(diff)을 DB에 저장해 다른 서버도 이어받게 한다.
-- 기준: docs/design/20261104_AADS_러너 재시작 시 작업 이어가기 기획.md 3.1·3.5
--
-- ■ 목적
--   러너가 종료되며 작업을 재대기시킬 때만 그때까지의 patch 를 저장하고, 이어받는 러너가 복원한다.
--   저장은 1건 CHECKPOINT_MAX_BYTES=2MiB 이하로 제한된다(러너 코드 쪽 상한).
--
-- ■ 만드는 객체 (모두 새 객체, 멱등)
--   표      pipeline_job_checkpoints
--   색인    idx_pjc_job_created (job_id, created_at DESC)
--   함수    aads_pipeline_job_checkpoints_prune(interval)  — 보존 기간 지난 행 삭제, 기본 7일
--   함수    aads_pipeline_job_checkpoints_purge_terminal() — 종료 작업의 행 삭제
--
-- ■ 기존 객체 영향과 잠금
--   · 모든 CREATE 는 새 표 pipeline_job_checkpoints 에만 닿는다. 기존 표에는 ALTER·색인·외래키·트리거를
--     만들지 않으므로 기존 표에 잠금을 걸지 않는다. 정리 함수는 정의만 만들 뿐 적용 시점에 실행되지 않는다.
--   · CREATE INDEX 는 방금 만든 빈 표에 대한 것이라 오래 걸리지 않는다. CONCURRENTLY 는 트랜잭션 시험
--     (BEGIN … ROLLBACK)이 불가능해 쓰지 않는다.
--   · 맨 위 SET LOCAL lock_timeout/statement_timeout 은 트랜잭션 안에서만 효과가 있다. 적용 스크립트
--     (scripts/apply_migration.sh)는 BEGIN 으로 감싸 실행하고, 자동 검증 시험도 BEGIN 안에서 돈다.
--     새 객체 생성이 5초 넘게 잠금을 기다리면 실패하고 아무것도 남기지 않는다(트랜잭션 롤백).
--   · 동명 객체 보호: 같은 이름의 표·색인·함수가 이미 있는데 이 파일이 만들려는 모양과 다르면 아래 DO 블록이
--     RAISE EXCEPTION 으로 멈춘다. 같으면 재실행(멱등)을 허용한다. CREATE OR REPLACE FUNCTION 은 본문뿐
--     아니라 SECURITY DEFINER·SET search_path 같은 속성도 기본값으로 되돌리므로, 본문(md5)과 속성을 함께
--     비교해 하나라도 다르면 교체하지 않고 멈춘다.
--   · 2026-11-05 12:54 KST 운영 DB 실측: pipeline_job_checkpoints 표 없음, 두 정리 함수 0건.
--     따라서 첫 적용은 순수 신규 생성이다.
--
-- ■ API 롤백 시 무해성
--   · app/ 코드는 이 표·함수를 참조하지 않는다. 쓰는 곳은 scripts/pipeline-runner.sh(.local) 뿐이고,
--     러너는 매번 to_regclass('public.pipeline_job_checkpoints') 로 표 존재를 확인해 없으면 저장·복원을
--     건너뛴다. 표에 트리거가 없고, 두 함수는 이 표만 삭제한다.
--   · 그러므로 API 를 이전 버전으로 되돌려도 남는 표·함수는 아무 경로에서도 자동 실행되지 않고 데이터만
--     그대로 남는다. 객체를 지우려면 아래 롤백 SQL 을 수동 실행한다.
--
-- ■ 저장 내용의 시크릿·개인정보 처리와 접근 범위
--   · patch 에는 시크릿·개인정보가 섞일 수 있다. 저장 전 시크릿 검사는 러너 코드(runner-ac50dffb, 진행 중)가
--     맡는다: 걸리면 값을 가리지 않고 아예 저장하지 않는다(가린 patch 는 적용할 수 없어 이어받기가 깨진다).
--     바이너리 덩어리처럼 검사할 수 없는 내용도 저장하지 않는다. 이 SQL 은 내용을 검사하지 않는다.
--   · 개인정보는 기계로 판별할 수 없다. 그래서 노출 기간을 줄인다: 보존 7일 + 작업 종료 즉시 삭제.
--   · 접근 범위(한계 포함): DB 로그인 역할은 superuser aads 1개뿐이다(aads_w12_runtime_test 는 NOLOGIN).
--     새 표에는 PUBLIC 권한을 주지 않는다(GRANT 없음). 그러나 채팅 SELECT 도구도 aads 로 접속하므로
--     이 표를 읽을 수 있다 — 이 한계는 해소되지 않았다. 채팅 전용 열 단위 권한 역할(patch 열 제외)은
--     후속 과제이며 이 마이그레이션 범위 밖이다.
--
-- ■ 정리 함수의 호출자·주기·범위
--   · 호출자와 주기: 러너가 체크포인트를 저장할 때마다 같은 예산 안에서 두 정리 함수를 호출한다(별도 작업
--     runner 코드 반영). 여기에 더해 scripts/prune_pipeline_job_checkpoints.sh 를 cron 으로 매시 17분에
--     돌린다(적용 후 설치). 저장이 없는 기간에도 cron 이 보존 기간을 지킨다.
--   · 범위: 함수 정의 그대로다. pipeline_job_checkpoints 표만 삭제한다(다른 표는 읽기만 한다).
--     삭제 조건은 (1) prune: created_at 이 보존 기간(기본 7일)보다 오래된 행, (2) purge_terminal:
--     pipeline_jobs 상태가 done/error/cancelled/rejected/rejected_done 인 작업의 행(종료 작업이 아카이브로
--     옮겨져 pipeline_jobs 에서 사라진 경우는 pipeline_jobs_archive 상태로 판정).
--     진행 중·대기 작업의 행은 남는다.
--
-- ■ 롤백 SQL (앱이 체크포인트를 쓰지 않는 상태에서만. 저장된 체크포인트가 사라진다)
--   DROP FUNCTION IF EXISTS aads_pipeline_job_checkpoints_purge_terminal();
--   DROP FUNCTION IF EXISTS aads_pipeline_job_checkpoints_prune(interval);
--   DROP TABLE IF EXISTS pipeline_job_checkpoints;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 동명 표·색인 보호: 이미 있는데 열 구성(이름·타입·NOT NULL·순서)이나 색인 정의가 다르면 멈춘다.
DO $guard_table$
DECLARE
    got text;
BEGIN
    SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod) || ':' || a.attnotnull::text,
                      ',' ORDER BY a.attnum)
      INTO got
      FROM pg_class c
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
     WHERE c.relnamespace = to_regnamespace(current_schema())
       AND c.relname = 'pipeline_job_checkpoints'
       AND c.relkind IN ('r', 'p');
    IF got IS NOT NULL AND got <> 'id:uuid:true,job_id:text:true,base_sha:text:true,patch:bytea:true,'
        'patch_sha256:text:true,files:jsonb:true,bytes:integer:true,model:text:false,attempt:integer:false,'
        'host:text:true,instruction_hash:text:true,created_at:timestamp with time zone:true' THEN
        RAISE EXCEPTION 'pipeline_job_checkpoints 가 이미 있고 열 구성이 다릅니다: %', got;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class c
                WHERE c.relnamespace = to_regnamespace(current_schema())
                  AND c.relname = 'pipeline_job_checkpoints'
                  AND c.relkind NOT IN ('r', 'p')) THEN
        RAISE EXCEPTION 'pipeline_job_checkpoints 이름을 표가 아닌 객체가 쓰고 있습니다';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND indexname = 'idx_pjc_job_created'
                  AND (tablename <> 'pipeline_job_checkpoints'
                       OR indexdef NOT LIKE '% USING btree (job_id, created_at DESC)')) THEN
        RAISE EXCEPTION 'idx_pjc_job_created 가 이미 있고 정의가 다릅니다';
    END IF;
END
$guard_table$;

CREATE TABLE IF NOT EXISTS pipeline_job_checkpoints (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           text NOT NULL,
    base_sha         text NOT NULL,
    patch            bytea NOT NULL,
    patch_sha256     text NOT NULL,
    files            jsonb NOT NULL DEFAULT '[]'::jsonb,
    bytes            int NOT NULL,
    model            text,
    attempt          int,
    host             text NOT NULL,
    instruction_hash text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pjc_job_created
    ON pipeline_job_checkpoints (job_id, created_at DESC);

COMMENT ON TABLE pipeline_job_checkpoints IS
    '러너 재시작 시 작업 이어가기용 진행분(patch) 저장. 보존 7일, 작업 종료 시 삭제. patch 에 민감 정보가 섞일 수 있어 접근 주의';

-- 동명 함수 보호: 같은 이름·같은 스키마의 함수가 이미 있으면 본문(md5)과 속성이 이 파일이 만들 함수와
-- 같아야 한다. 본문이 같아도 SECURITY DEFINER·SET(search_path 등)·변동성·인자 등이 다르면 뒤의
-- CREATE OR REPLACE 가 그 속성을 기본값으로 바꾸므로 멈춘다. 다른 인자 목록의 동명 함수(오버로드)도 멈춘다.
-- md5 상수는 아래 두 함수 본문($$ 사이 문자열)의 md5 이며 tests/unit/test_pipeline_job_checkpoints_migration.py 가
-- 일치를 검사한다. 본문을 고치면 상수도 같이 고쳐야 한다.
DO $guard_func$
DECLARE
    r record;
    p record;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('aads_pipeline_job_checkpoints_prune', '5fdac043461f3bd83ed0f1aa7e0345f9', 'retention interval',
             '''7 days''::interval'),
            ('aads_pipeline_job_checkpoints_purge_terminal', '1b7dd277eef9e0cceae1c5b95b6f89fe', '', '')
        ) AS v(fname, body_md5, ident_args, default_expr)
    LOOP
        FOR p IN
            SELECT pr.*,
                   pg_get_function_identity_arguments(pr.oid) AS ident_args,
                   coalesce(pg_get_expr(pr.proargdefaults, 0), '') AS default_expr
              FROM pg_proc pr
             WHERE pr.pronamespace = to_regnamespace(current_schema())
               AND pr.proname = r.fname
        LOOP
            IF p.ident_args <> r.ident_args
               OR p.default_expr <> r.default_expr
               OR md5(p.prosrc) <> r.body_md5
               OR p.prolang <> (SELECT l.oid FROM pg_language l WHERE l.lanname = 'plpgsql')
               OR p.prorettype <> 'bigint'::regtype
               OR p.prokind <> 'f'
               OR p.proretset
               OR p.prosecdef
               OR p.proconfig IS NOT NULL
               OR p.provolatile <> 'v'
               OR p.proisstrict
               OR p.proleakproof
               OR p.proparallel <> 'u'
               OR p.procost <> 100 THEN
                RAISE EXCEPTION '함수 %(%) 가 이미 있고 본문 또는 속성이 이 마이그레이션과 다릅니다. 교체하지 않고 멈춥니다',
                    r.fname, p.ident_args;
            END IF;
        END LOOP;
    END LOOP;
END
$guard_func$;

-- 보존 기간이 지난 체크포인트 삭제. 삭제한 행 수를 돌려준다. 기본 7일.
CREATE OR REPLACE FUNCTION aads_pipeline_job_checkpoints_prune(retention interval DEFAULT interval '7 days')
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    n bigint;
BEGIN
    DELETE FROM pipeline_job_checkpoints
     WHERE created_at < now() - retention;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

-- 종료(done/error/cancelled/rejected/rejected_done)된 작업의 체크포인트 삭제. 삭제한 행 수를 돌려준다.
-- 종료 집합은 app/services/pipeline_cleanup.py 의 _TERMINAL_STATUSES 와 같아야 한다(하나라도 빠지면 샌다).
-- 진행 중·대기 작업은 pipeline_jobs 에서 종료 상태가 아니므로 남는다.
-- 종료 작업이 아카이브로 옮겨져 pipeline_jobs 에서 사라진 경우도 아카이브 상태로 판정한다.
-- (pipeline_jobs 에 있는 작업은 아카이브 상태가 아니라 pipeline_jobs 상태가 우선한다.)
CREATE OR REPLACE FUNCTION aads_pipeline_job_checkpoints_purge_terminal()
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    n bigint;
BEGIN
    DELETE FROM pipeline_job_checkpoints c
     WHERE EXISTS (
               SELECT 1 FROM pipeline_jobs j
                WHERE j.job_id = c.job_id
                  AND j.status IN ('done', 'error', 'cancelled', 'rejected', 'rejected_done')
           )
        OR (
               NOT EXISTS (SELECT 1 FROM pipeline_jobs j WHERE j.job_id = c.job_id)
               AND EXISTS (
                   SELECT 1 FROM pipeline_jobs_archive a
                    WHERE a.job_id = c.job_id
                      AND a.status IN ('done', 'error', 'cancelled', 'rejected', 'rejected_done')
               )
           );
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

COMMENT ON FUNCTION aads_pipeline_job_checkpoints_prune(interval) IS
    '보존 기간(기본 7일) 지난 pipeline_job_checkpoints 행 삭제. 이 표만 지운다';
COMMENT ON FUNCTION aads_pipeline_job_checkpoints_purge_terminal() IS
    '종료 상태(done/error/cancelled/rejected/rejected_done) 작업의 pipeline_job_checkpoints 행 삭제. 이 표만 지운다';
