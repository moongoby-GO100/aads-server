-- AADS-RDOC-M5-DAILY-REGISTRATION-20261020 — R-DOC 일일 정본 등록률 계측 저장소.
-- 이 파일은 일부러 migrations/ 밖에 있다. migrations/*.sql 은 다음 릴리스에서 자동 적용되므로(scripts/apply_release_migrations.sh),
-- 운영 schema 설치를 별도 승인으로 분리하려면 승인 뒤에 `git mv` 로 migrations/ 에 옮겨 릴리스에 태운다.
-- 미적용이어도 수집기는 쓰기 없이 보고만 한다.
-- 롤백: 같은 디렉터리의 20261020_rdoc_registration_daily.down.sql (먼저 수집 행을 CSV 로 내려받을 것)
--
-- 한 행 = (tenant, project, KST 날짜, 지표). 지표 A(신규 문서 등록률)와 B(게이트 ok 비율)는 metric 으로 분리한다.
-- 수집하지 않은 날짜는 행이 없다. 분모 0 은 status=not_applicable, rate=NULL 이다(0% 아님).
BEGIN;

CREATE TABLE IF NOT EXISTS rdoc_registration_daily (
    tenant_id          uuid        NOT NULL REFERENCES tenants(id),
    project_key        text        NOT NULL CHECK (project_key ~ '^[A-Z0-9][A-Z0-9_-]{0,63}$'),
    kst_date           date        NOT NULL,
    metric             text        NOT NULL CHECK (metric IN ('A_new_doc_registration', 'B_gate_ok_ratio')),
    numerator          integer     NOT NULL CHECK (numerator >= 0),
    denominator        integer     NOT NULL CHECK (denominator >= 0),
    excluded_count     integer     NOT NULL DEFAULT 0 CHECK (excluded_count >= 0),
    excluded_reasons   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    rate               numeric(7, 6),
    status             text        NOT NULL CHECK (status IN ('measured', 'not_applicable')),
    quality            text        NOT NULL CHECK (quality IN ('on_time', 'late_backfill', 'partial_day', 'unverified_source')),
    first_collected_at timestamptz NOT NULL,
    collected_at       timestamptz NOT NULL,
    collector_version  text        NOT NULL,
    detail             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, project_key, kst_date, metric),
    CHECK (numerator <= denominator),
    CHECK ((status = 'not_applicable') = (denominator = 0)),
    CHECK ((rate IS NULL) = (denominator = 0)),
    CHECK (rate IS NULL OR rate BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS rdoc_registration_daily_date_idx
    ON rdoc_registration_daily (kst_date DESC, metric);

COMMIT;
