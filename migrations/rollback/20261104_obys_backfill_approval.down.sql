-- 롤백: 백필 승인 표만 제거한다. 승인 기록과 감사 기록이 함께 사라지므로 실행 전 건수를 확인하라.
-- 롤백 후에는 승인 조회가 실패하므로 백필 진입점이 모두 막힌다(정기 증분 수집은 영향 없음).
BEGIN;
DROP TABLE IF EXISTS public.obys_backfill_approval_audit;
DROP TABLE IF EXISTS public.obys_backfill_approval;
COMMIT;
