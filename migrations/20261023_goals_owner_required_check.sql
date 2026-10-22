-- 20261023 goals 주도담당 필수 CHECK (NOT VALID) — 파일만 둔다. 운영 적용은 별도 승인 후.
--
-- NOT VALID 는 기존 행을 훑지 않을 뿐, **누락 행을 UPDATE 하면 그 행도 검사한다.**
-- 즉 누락 행이 남은 채 적용하면 그 목표의 진행률·상태 갱신 UPDATE 가 23514 로 실패한다.
-- 적용 순서: ① scripts/goal_owner_backfill.py --apply 로 확정 건 보정 ② 모호 건은 사람이 담당 지정
-- ③ scripts/goal_owner_backfill.py (dry-run) 이 0건인 것을 확인한 뒤에만 이 파일을 적용 ④ VALIDATE.
-- 종결 상태(completed/cancelled/archived 등)는 소급하지 않으므로 CHECK 대상에서 뺀다.
-- 되돌림: migrations/rollback/20261023_goals_owner_required_check.down.sql
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_goals_owner_required'
                   AND conrelid = 'goals'::regclass) THEN
        ALTER TABLE goals ADD CONSTRAINT ck_goals_owner_required
            CHECK (status NOT IN ('active', 'draft')
                   OR (btrim(COALESCE(owner_role_key, '')) <> '' AND owner_session_id IS NOT NULL))
            NOT VALID;
    END IF;
END $$;

-- 보정 완료 후 별도 승인 단계에서:
--   ALTER TABLE goals VALIDATE CONSTRAINT ck_goals_owner_required;
