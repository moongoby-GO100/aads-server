-- 롤백: 클로브 수집 계약 테이블만 제거한다. yeoljeong_* 기존 테이블은 건드리지 않는다.
-- 데이터가 들어 있으면 함께 사라진다 — 실행 전 obys_clobe_ledger_entry 건수를 확인하라.
BEGIN;
DROP TABLE IF EXISTS obys_clobe_ledger_entry;
DROP TABLE IF EXISTS obys_clobe_item;
DROP TABLE IF EXISTS obys_clobe_collection_state;
DROP TABLE IF EXISTS obys_clobe_collection_run;
DROP TABLE IF EXISTS obys_clobe_collection_lease;
DROP TABLE IF EXISTS obys_clobe_company_link;
COMMIT;
