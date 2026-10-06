-- L1 문서 저장 규칙(R-DOC) 활성화 (AADS-RDOC-UNIFIED-STORAGE-20261006).
-- 대상: prompt_assets.slug='l1-doc-storage-rule' (20261003 초안, enabled=false).
-- 하는 일: 원본 행 백업 -> 본문 갱신(보고 링크 항목 추가, 파일명 규칙 정리) -> enabled=true.
-- 안전장치
--   * 원본(content md5)이 기대값과 같을 때만 적용한다. 다르면 누군가 먼저 고친 것이므로 중단(EXCEPTION).
--   * 같은 트랜잭션에서 행을 FOR UPDATE 로 잠근다(동시 변경 차단).
--   * 이미 적용된 뒤 다시 실행하면 아무것도 바꾸지 않는다(멱등).
--   * 백업: prompt_rdoc_activation_backup(원본 행 전체 jsonb) + prompt_asset_versions(원본 본문).
--   * 롤백: migrations/rollback/20261006_l1_doc_storage_rule_activate.down.sql
-- 이 파일은 저장소에만 있다. 운영 DB 적용은 별도 승인된 창구에서 사람이 실행한다.
BEGIN;

CREATE TABLE IF NOT EXISTS prompt_rdoc_activation_backup (
    id            bigserial PRIMARY KEY,
    slug          varchar(150) NOT NULL,
    original_row  jsonb        NOT NULL,
    original_md5  text         NOT NULL,
    applied_md5   text         NOT NULL,
    applied_at    timestamptz  NOT NULL DEFAULT now(),
    rolled_back_at timestamptz,
    UNIQUE (slug, original_md5)
);

DO $migrate$
DECLARE
    expected_md5 CONSTANT text := 'e2fcbb594daa8089004b2f9d6b025d73';
    new_content  CONSTANT text := $rule$## L1 Global / 문서 저장 규칙 (R-DOC)
세션·러너·서브에이전트가 남기는 계획·PRD·설계·보고·결과 문서에 적용한다. 모든 프로젝트·모델에 같다. 핸드오버는 R-HANDOVER-DB 가 원본이므로 여기서 다루지 않는다.

1. 저장 위치(프로젝트 저장소 기준): plan→docs/plans, prd→docs/prd, design·architecture→docs/design, report·결과→docs/reports, spec·tasks→docs/specs/<슬라이스>/. 저장소 루트 reports/ 와 uploads/chat 에 신규 정본 문서를 두지 않는다. 기존 문서의 경로·document_key 는 바꾸지 않는다.
2. 신규 문서 파일명은 YYYYMMDD_{PROJECT}_{한글 제목}.md 로 쓴다. 화면 제목과 본문 첫 제목(H1)도 한글로 쓰고, 영문 소문자 kebab-case document_key 를 따로 붙인다. FLOW 접두 파일명은 기존 문서의 호환 표기로만 남긴다.
3. 정본 등록 의무: 문서를 만들면 등록 도구가 있으면 도구로, 없으면 POST /api/v1/projects/{PROJECT}/documents 로 초안 등록한다(승인은 별도 승인 경로만). kind 는 plan|prd|spec|design|architecture|contract|tasks|report|reference 중 하나를 지정하고, 목표가 있으면 goal_id 를 넣는다. 완료 보고에 document_key 를 적는다. 등록하지 않았으면 "정본 미등록(미완료)"으로 분리 보고한다.
4. 갱신은 새 파일이 아니라 같은 document_key 의 새 판본으로 올린다.
5. 보고 링크: 파일 경로를 링크처럼 적지 않는다. 등록 도구가 돌려준 열람 참조(view / report_line)를 그대로 쓴다. 열람 참조를 받지 못했으면 document_key 와 한글 제목을 적고 "열람 링크 미제공"이라고 쓴다. 열리지 않는 링크를 만들어 보고하지 않는다.
6. 금지: 날짜만 바꾼 사본 파일 만들기. 채팅 본문에만 남기고 "문서 저장 완료"라고 보고하기.

왜: 최근 7일 신규 문서 17건 중 정본 등록 1건, FLOW 파일명 준수 2건뿐이었고, 보고된 문서 링크가 열리지 않는 사례가 반복됐다.$rule$;
    cur prompt_assets%ROWTYPE;
    cur_md5 text;
    new_md5 text := md5(new_content);
BEGIN
    SELECT * INTO cur FROM prompt_assets WHERE slug = 'l1-doc-storage-rule' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'l1-doc-storage-rule 행이 없다 — 20261003 초안 마이그레이션이 먼저 필요하다';
    END IF;
    cur_md5 := md5(cur.content);

    IF cur_md5 = new_md5 AND cur.enabled THEN
        RAISE NOTICE 'R-DOC 규칙은 이미 활성화되어 있다 — 변경 없음';
        RETURN;
    END IF;
    IF cur_md5 <> expected_md5 THEN
        RAISE EXCEPTION 'l1-doc-storage-rule 본문이 기대값과 다르다(현재 md5=%). 동시 변경 의심 — 중단', cur_md5;
    END IF;
    IF cur.enabled THEN
        RAISE EXCEPTION 'l1-doc-storage-rule 이 이미 활성(enabled=true) 상태에서 본문이 다르다 — 중단';
    END IF;

    INSERT INTO prompt_rdoc_activation_backup (slug, original_row, original_md5, applied_md5)
    VALUES (cur.slug, to_jsonb(cur), cur_md5, new_md5)
    ON CONFLICT (slug, original_md5) DO UPDATE SET applied_md5 = EXCLUDED.applied_md5, applied_at = now(), rolled_back_at = NULL;

    INSERT INTO prompt_asset_versions (prompt_asset_id, version_no, content, model_variants, changed_by, change_note)
    VALUES (
        cur.id,
        COALESCE((SELECT max(version_no) FROM prompt_asset_versions WHERE prompt_asset_id = cur.id), 0) + 1,
        cur.content, cur.model_variants, 'runner_20261006_rdoc_unified_storage',
        'R-DOC 활성화 전 원본(enabled=false, md5=' || cur_md5 || ')'
    );

    UPDATE prompt_assets
       SET content = new_content,
           title = 'L1 Global - Document Storage Rule / 문서 저장 규칙 (R-DOC)',
           enabled = true,
           updated_at = now()
     WHERE id = cur.id;
END
$migrate$;

COMMIT;
