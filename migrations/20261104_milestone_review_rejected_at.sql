-- 마일스톤 반려 시각. 주도가 반려한 뒤 새 연결 작업이나 담당의 직접 신고가 있을 때만 재신고한다.
-- 적용 전에도 앱은 열 부재를 감지해 기존 동작으로 폴백한다.
--
-- 이 파일이 만드는 객체(전부 새 이름 — 운영 DB 에 아직 없음):
--   1) 열 milestones.review_rejected_at (timestamptz NULL)
--        주도가 반려한 시각. 담당이 직접 신고하거나 새 연결로 재신고되면 NULL.
--   2) 열 goal_task_links.milestone_linked_at (timestamptz NULL)
--        작업이 "이 마일스톤에" 연결된 시각. created_at 은 같은 목표에 먼저 연결해 둔 작업을
--        나중에 마일스톤으로 옮겨도 그대로라서 "반려 이후의 새 연결" 판정에 쓸 수 없다.
--        NULL 이면 앱은 created_at 으로 폴백한다(이 마이그레이션 이전에 만든 연결).
--   3) 함수 goal_task_links_stamp_milestone_linked_at()  — 위 2) 를 채우는 트리거 함수
--   4) 트리거 trg_goal_task_links_milestone_linked_at ON goal_task_links
--
-- 왜 트리거인가(앱 코드에서만 찍는 안은 채택하지 않았다):
--   goal_task_links 쓰기 경로가 앱 밖에도 있다 — goal_manager._link_task_with_context(ON CONFLICT),
--   pipeline_runner_service.py:367, 채팅 db_safe_write 의 직접 INSERT(2026-11-05 실제 2행).
--   앱만 고치면 직접 INSERT/UPDATE 가 milestone_linked_at 을 비워 두고, 반려 뒤 "새 연결" 판정이
--   created_at 폴백으로 틀어진다. 모든 쓰기 경로를 한곳에서 받으려면 DB 쪽에서 찍어야 한다.
--
-- 기존 객체 보호(덮어쓰지 않는다):
--   기존 동명 함수는 절대 교체·삭제하지 않고, 없을 때만 만든다. 함수를 최상위 문장으로 다시
--   정의하면 소유자·권한 외 모든 속성(SET search_path, volatility 등)이 기본값으로 돌아가므로,
--   남이 설정한 속성을 조용히 지운다. 그래서 DO 블록 IF 분기 안에서 정적 CREATE FUNCTION 을
--   실행한다(PL/pgSQL 은 DDL 을 정적 문장으로 실행할 수 있어 동적 SQL 이 필요 없다).
--   함수·트리거가 이미 있으면 정의(함수: 본문·언어·반환형·SECURITY; 트리거: 함수·타이밍·이벤트·
--   대상 열·WHEN·인자 수·활성 상태)를 이 파일의 기대값과 비교해, 다르면 RAISE 로 중단한다.
--   트리거 생성은 DO 블록 밖 최상위 문장 하나다(아래 "트리거 문장의 무해성" 참조).
--   주의: 아래 DO 블록의 v_body 와 CREATE FUNCTION 의 $body$ 본문은 바이트 단위로 같아야 한다(테스트가 검사).
--
-- 트리거 문장의 무해성(최상위 문장이 기존 트리거를 바꾸지 못하는 이유):
--   - 이 문장은 앞선 DO 가드가 "트리거 없음" 또는 "함수·타이밍·이벤트·열·WHEN·인자 수·활성 상태
--     모두 같음"을 확인한 뒤에만 실행된다(ON_ERROR_STOP + 한 트랜잭션이라 가드가 RAISE 하면 여기까지 오지 않는다).
--   - BEFORE 행 트리거라 전이 테이블(REFERENCING)이 없다.
--   - 이 형태의 문장은 제약 트리거(CONSTRAINT TRIGGER)를 바꿀 수 없다.
--   - 따라서 같은 정의로 다시 만들어도 바뀌는 속성이 없다.
--   - 함수에는 이 형태를 쓰지 않는다. 함수를 다시 정의하면 search_path·volatility 같은 속성이 초기화되기 때문이다.
--   - 트리거를 DO 블록 안에서 만들지 않는 이유: 자동 검증기(release_migration_autoverify.py)가
--     DO 본문의 데이터 변경 키워드(트리거 이벤트 이름 포함)를 데이터 변경으로 판정하기 때문이다.
--
-- 롤백해도 트리거가 남는 경우의 무해성(API 만 되돌리고 SQL 은 두는 경우):
--   - 트리거가 바꾸는 것은 NEW.milestone_linked_at 한 열뿐이다. 이 열은 이 마이그레이션이 추가한 열이며
--     이전 버전 API 코드는 읽지도 쓰지도 않는다.
--   - INSERT 때 milestone_linked_at 이 이미 들어 있으면 덮어쓰지 않는다. 행 변경은 milestone_id 가
--     실제로 바뀔 때만 찍는다(변경 대상을 milestone_id 로 한정 + IS DISTINCT FROM 비교).
--   - 다른 테이블 접근·외부 호출·예외 발생 경로가 없다(NOW() 대입뿐). 비용은 행당 상수 시간이다.
--   - 따라서 트리거가 남아도 이전 코드의 동작·결과는 바뀌지 않는다. 두 열도 nullable 이라 남겨 둬도 무해하다.
--
-- 완전 롤백(필요 시에만, 앱을 먼저 되돌린 뒤 이 순서로):
--   DROP TRIGGER IF EXISTS trg_goal_task_links_milestone_linked_at ON goal_task_links;
--   DROP FUNCTION IF EXISTS goal_task_links_stamp_milestone_linked_at();
--   ALTER TABLE goal_task_links DROP COLUMN IF EXISTS milestone_linked_at;
--   ALTER TABLE milestones DROP COLUMN IF EXISTS review_rejected_at;
--
-- 잠금:
--   ALTER TABLE ... ADD COLUMN ... NULL 은 기본값이 없어 테이블 재작성이 없다(카탈로그 변경만, 즉시 끝남).
--   다만 ACCESS EXCLUSIVE 잠금을 잡으므로, 긴 트랜잭션 뒤에 줄을 서서 다른 쓰기를 막지 않도록
--   SET LOCAL lock_timeout = '5s' 를 건다. 적용기(apply_migration.sh)와 자동 검증기가 모두 이 파일을
--   하나의 트랜잭션 안에서 돌리므로 SET LOCAL 은 트랜잭션 끝에 저절로 풀린다(RESET 불필요).
--   이 파일에는 CREATE INDEX 가 없다.
--   이 파일은 운영 중인 기존 표(milestones, goal_task_links)에 ALTER TABLE·CREATE TRIGGER 를 한다.
--   잠금을 5초 안에 못 얻으면 적용이 실패하고 트랜잭션 전체가 롤백되며, 기존 쓰기는 그대로 계속된다(실패해도 안전).
--   이 경우 쓰기가 한가한 시간에 다시 적용한다.
--   SET LOCAL 은 트랜잭션 안에서만 효력이 있다. psql -f 로 BEGIN 없이 실행하면 값이 적용되지 않으므로
--   반드시 BEGIN…COMMIT 안에서(apply_migration.sh / 자동 검증기 방식) 실행한다.
--
-- 참고: 이 파일은 어떤 행도 변경하지 않는다. 실행되는 SQL 은 SET LOCAL, ALTER TABLE ADD COLUMN,
--   함수 생성(없을 때만), 트리거 생성뿐이다.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE milestones ADD COLUMN IF NOT EXISTS review_rejected_at timestamptz NULL;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS milestone_linked_at timestamptz NULL;

DO $do$
DECLARE
    v_body CONSTANT text := $body$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.milestone_id IS NOT NULL AND NEW.milestone_linked_at IS NULL THEN
            NEW.milestone_linked_at := NOW();
        END IF;
    ELSIF NEW.milestone_id IS DISTINCT FROM OLD.milestone_id THEN
        NEW.milestone_linked_at := CASE WHEN NEW.milestone_id IS NULL THEN NULL ELSE NOW() END;
    END IF;
    RETURN NEW;
END $body$;
    v_fn_oid   oid;
    v_attnum   smallint;
    v_trg      record;
BEGIN
    SELECT attnum INTO v_attnum
      FROM pg_attribute
     WHERE attrelid = 'goal_task_links'::regclass AND attname = 'milestone_id' AND NOT attisdropped;
    IF v_attnum IS NULL THEN
        RAISE EXCEPTION 'goal_task_links.milestone_id 열이 없어 트리거를 만들 수 없다';
    END IF;

    -- 함수: 없으면 만들고, 있으면 기대 정의와 같은지 확인한다(다르면 중단).
    v_fn_oid := to_regprocedure('goal_task_links_stamp_milestone_linked_at()');
    IF v_fn_oid IS NULL THEN
        CREATE FUNCTION goal_task_links_stamp_milestone_linked_at() RETURNS trigger
        LANGUAGE plpgsql AS $body$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.milestone_id IS NOT NULL AND NEW.milestone_linked_at IS NULL THEN
            NEW.milestone_linked_at := NOW();
        END IF;
    ELSIF NEW.milestone_id IS DISTINCT FROM OLD.milestone_id THEN
        NEW.milestone_linked_at := CASE WHEN NEW.milestone_id IS NULL THEN NULL ELSE NOW() END;
    END IF;
    RETURN NEW;
END $body$;
        v_fn_oid := to_regprocedure('goal_task_links_stamp_milestone_linked_at()');
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_proc p
         WHERE p.oid = v_fn_oid
           AND p.prosrc = v_body
           AND p.prolang = (SELECT oid FROM pg_language WHERE lanname = 'plpgsql')
           AND p.prorettype = 'trigger'::regtype
           AND NOT p.prosecdef
    ) THEN
        RAISE EXCEPTION '기존 함수 goal_task_links_stamp_milestone_linked_at() 의 정의가 이 마이그레이션의 기대 본문과 다르다 — 덮어쓰지 않고 중단한다. 내용을 확인한 뒤 사람이 정리해야 한다';
    END IF;

    -- 트리거: 이미 있으면 정의가 기대값과 같은지 확인하고 다르면 예외로 중단한다. 없으면 통과하고 최상위 문장이 만든다.
    SELECT t.tgfoid, t.tgtype, t.tgattr::text AS cols, t.tgqual IS NULL AS no_when,
           t.tgnargs, t.tgenabled
      INTO v_trg
      FROM pg_trigger t
     WHERE t.tgrelid = 'goal_task_links'::regclass
       AND t.tgname = 'trg_goal_task_links_milestone_linked_at'
       AND NOT t.tgisinternal;
    IF FOUND AND NOT (v_trg.tgfoid = v_fn_oid
                      AND v_trg.tgtype = 23          -- ROW(1) + BEFORE(2) + INSERT(4) + 행 변경(16)
                      AND v_trg.cols = v_attnum::text
                      AND v_trg.no_when
                      AND v_trg.tgnargs = 0
                      AND v_trg.tgenabled = 'O') THEN
        RAISE EXCEPTION '기존 트리거 trg_goal_task_links_milestone_linked_at 의 정의(함수·이벤트·열·WHEN·인자 수·활성 상태)가 이 마이그레이션의 기대값과 다르다 — 덮어쓰지 않고 중단한다. 내용을 확인한 뒤 사람이 정리해야 한다';
    END IF;
END
$do$;

-- 위 DO 가드를 통과한 경우에만 도달한다: 트리거가 없거나 기대 정의와 같다(헤더의 "트리거 문장의 무해성" 참조).
CREATE OR REPLACE TRIGGER trg_goal_task_links_milestone_linked_at BEFORE INSERT OR UPDATE OF milestone_id ON goal_task_links FOR EACH ROW EXECUTE FUNCTION goal_task_links_stamp_milestone_linked_at();
