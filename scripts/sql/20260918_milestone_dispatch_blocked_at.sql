-- 발송 설명(dispatch_note)과 실제 차단 상태를 분리한다.
-- 기존 메모는 여러 운영 사유를 담으므로 차단으로 소급 변환하지 않는다.
ALTER TABLE milestones
    ADD COLUMN IF NOT EXISTS dispatch_blocked_at TIMESTAMPTZ;
