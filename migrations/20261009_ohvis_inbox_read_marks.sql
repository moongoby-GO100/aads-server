-- 오비스 알림 모아보기: 러너 카드·승인 대기 항목의 사용자별 확인 표시. 롤백: 표를 지우지 않는다(DROP 금지) — 코드를 되돌리면 이 표는 읽히지 않는 빈 표로 남는다
-- 기존 알림 테이블은 건드리지 않는다. 멱등(CREATE TABLE IF NOT EXISTS), DROP 없음.
CREATE TABLE IF NOT EXISTS ohvis_inbox_read_marks (
    tenant_id uuid        NOT NULL,
    user_id   text        NOT NULL,
    source    text        NOT NULL,
    source_id text        NOT NULL,
    read_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, source, source_id)
);
