-- 테이블은 남긴다(DROP 금지). 코드를 되돌리면 옛 코드는 이 테이블을 읽지 않으므로 기존 동작에 영향이 없다.
-- 코드를 되돌리지 않고 자동 닫기만 멈추려면 시드를 끈다.
UPDATE browser_site_overlays SET status = 'disabled', updated_at = now() WHERE source = 'seed' AND status = 'approved';
