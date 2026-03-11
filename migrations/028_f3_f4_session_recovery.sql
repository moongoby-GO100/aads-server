-- Migration 028: F3 session_notes FK + F4 recovery_logs rename
-- Date: 2026-03-12

-- F3: session_notes.session_id varchar(100) → UUID + FK to chat_sessions
ALTER TABLE session_notes ALTER COLUMN session_id TYPE UUID USING session_id::uuid;
ALTER TABLE session_notes ADD CONSTRAINT fk_session_notes_session
  FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE;

-- F4: recovery_logs → escalation_recovery (이름 혼동 방지)
-- recovery_log: 복구 명령/레시피 (unified_healer, watchdog) - 1347건
-- recovery_logs: 에스컬레이션 복구 이벤트 (escalation_engine, recovery_graph) - 4건
-- 스키마가 완전히 다른 별개 시스템이므로 이름만 정리
ALTER TABLE recovery_logs RENAME TO escalation_recovery;
ALTER INDEX recovery_logs_pkey RENAME TO escalation_recovery_pkey;
ALTER INDEX idx_recovery_logs_created RENAME TO idx_escalation_recovery_created;
ALTER INDEX idx_recovery_logs_result RENAME TO idx_escalation_recovery_result;
ALTER INDEX idx_recovery_logs_server RENAME TO idx_escalation_recovery_server;
ALTER INDEX idx_recovery_logs_type RENAME TO idx_escalation_recovery_type;
ALTER SEQUENCE recovery_logs_id_seq RENAME TO escalation_recovery_id_seq;
