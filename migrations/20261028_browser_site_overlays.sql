-- 20261028 browser_site_overlays — 사이트별 공지 팝업 공용 목록(스마트브라우저 서버 레인).
--
-- 레시피 실행기 dismiss_overlays, '사이트 공지 팝업 닫기' 도구, 채팅 browser_click 의 가림 처리가
-- 같은 approved 행만 읽는다. candidate/rejected/disabled 는 어떤 경로에서도 누르지 않는다.
-- tenant_id NULL = 전역(모든 테넌트). evidence 에는 screenshot_url 등 참조만 둔다 — 비밀값 금지.
-- 되돌림: migrations/rollback/20261028_browser_site_overlays.down.sql (테이블은 남기고 시드만 끈다).
-- 멱등: 여러 번 적용해도 안전하다. 기존 테이블은 건드리지 않는다.
CREATE TABLE IF NOT EXISTS browser_site_overlays (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID,
    domain         TEXT NOT NULL CHECK (btrim(domain) <> ''),
    root_selector  TEXT NOT NULL CHECK (btrim(root_selector) <> ''),
    close_selector TEXT NOT NULL CHECK (btrim(close_selector) <> ''),
    label          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'candidate'
                   CHECK (status IN ('candidate', 'approved', 'rejected', 'disabled')),
    source         TEXT NOT NULL DEFAULT 'manual'
                   CHECK (source IN ('seed', 'manual', 'recorded')),
    evidence       JSONB NOT NULL DEFAULT '{}'::jsonb,
    approved_by    TEXT,
    approved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_site_overlays_scope
    ON browser_site_overlays (
        COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), domain, root_selector
    );
CREATE INDEX IF NOT EXISTS idx_browser_site_overlays_status_domain
    ON browser_site_overlays (status, domain);

-- 선행 작업(AADS-SB-SITE-POPUP-DISMISS-20261028)의 허용목록 초기값 my.a-bly.com 을 approved 시드로 옮긴다.
-- 루트는 schema.SITE_POPUP_ALLOWLIST 의 popup_selector 를 한 줄씩 나눈 것이다.
INSERT INTO browser_site_overlays
    (tenant_id, domain, root_selector, close_selector, label, status, source, evidence, approved_by, approved_at)
VALUES
    (NULL, 'my.a-bly.com', '[role="dialog"]', 'button[aria-label="close" i]',
     '에이블리 어드민 공지 모달(dialog)', 'approved', 'seed', '{}'::jsonb,
     'seed:AADS-SB-SITE-POPUP-CATALOG-20261028', now()),
    (NULL, 'my.a-bly.com', '[role="alertdialog"]', 'button[aria-label="close" i]',
     '에이블리 어드민 알림 모달(alertdialog)', 'approved', 'seed', '{}'::jsonb,
     'seed:AADS-SB-SITE-POPUP-CATALOG-20261028', now()),
    (NULL, 'my.a-bly.com', '[role="alert"]', 'button[aria-label="close" i]',
     '에이블리 어드민 알림 배너(alert)', 'approved', 'seed', '{}'::jsonb,
     'seed:AADS-SB-SITE-POPUP-CATALOG-20261028', now())
ON CONFLICT DO NOTHING;
