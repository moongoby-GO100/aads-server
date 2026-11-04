-- 롤백: 서명본 PDF 1회용 링크 테이블 제거 (링크는 5분짜리라 잃을 데이터가 없다).
BEGIN;
DROP TABLE IF EXISTS yeoljeong_contract_pdf_links;
COMMIT;
