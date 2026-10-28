-- 20261028 canonical_review_digest_log — 검토 대기 정본 문서 일일 요약의 하루 1회 발송 기록.
-- (KST 날짜, 테넌트) 당 한 행. 블루그린 두 슬롯이 같은 잡을 돌려도 PK 가 중복 발송을 막는다.
-- 되돌림: migrations/rollback/20261028_canonical_review_digest_log.down.sql
CREATE TABLE IF NOT EXISTS canonical_review_digest_log (
    digest_date date NOT NULL,
    tenant_id uuid NOT NULL,
    pending_count integer NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (digest_date, tenant_id)
);
