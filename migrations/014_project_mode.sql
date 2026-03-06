-- AADS-128: projects 테이블에 mode 컬럼 추가
-- mode="full_cycle"      → full_cycle_graph (ideation + execution)
-- mode="execution_only"  → 기존 8-agent graph (하위 호환 기본값)

ALTER TABLE projects ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT 'execution_only';

-- 기존 레코드는 모두 execution_only로 유지 (하위 호환)
UPDATE projects SET mode = 'execution_only' WHERE mode IS NULL;
